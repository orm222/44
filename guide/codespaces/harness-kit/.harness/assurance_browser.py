"""Optional browser assurance with isolated server ownership and durable evidence.

Importing this module neither discovers nor launches a browser. The controller
calls it only for an explicitly approved web project. Playwright is optional and
is imported solely by browser_probe.py in a separate bounded process.
"""
from datetime import datetime, timezone
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


BASE = Path(__file__).resolve().parent
PROBE = BASE / 'browser_probe.py'


def _load(name):
    loader = importlib.util.spec_from_file_location('browser_assurance_' + name, BASE / (name + '.py'))
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    return module


runtime = _load('development_runtime')
processes = runtime.processes
clock = runtime.problem_time


def _object(**properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


STRING, INTEGER = {'type': 'string'}, {'type': 'integer'}
BROWSER_SCHEMA = _object(
    browser={'type': 'string', 'enum': ['chromium', 'firefox', 'webkit']},
    server=_object(argv={'type': 'array', 'items': STRING}, cwd=STRING, ready_url=STRING, timeout_seconds=INTEGER),
    cases={'type': 'array', 'items': _object(id=STRING, path=STRING, width=INTEGER, height=INTEGER,
        steps={'type': 'array', 'items': _object(action={'type': 'string', 'enum': ['fill', 'click', 'select', 'press']},
                                               selector=STRING, value=STRING)},
        expect={'type': 'array', 'items': _object(selector=STRING, text=STRING)})})


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(path):
    return hashlib.sha256(runtime._read_bytes(path)).hexdigest()


def origin(url):
    parsed = urlsplit(url)
    _need(parsed.scheme in ('http', 'https') and parsed.hostname is not None
          and parsed.username is None and parsed.password is None, 'Browser URL must be a credential-free HTTP loopback URL')
    try:
        address = ipaddress.ip_address(parsed.hostname)
        port = parsed.port
    except ValueError as exc:
        raise ValueError('Use a literal loopback address, such as 127.0.0.1, for browser assurance') from exc
    _need(address.is_loopback and port is not None and 1 <= port <= 65535, 'Browser URL requires an explicit loopback port')
    host = '[' + parsed.hostname + ']' if address.version == 6 else parsed.hostname
    return parsed.scheme + '://' + host + ':' + str(port)


def validate_browser(config):
    runtime.validate_schema(config, BROWSER_SCHEMA)
    server = config['server']
    _need(server['argv'] and all(value and '\x00' not in value for value in server['argv']), 'Browser server argv is empty or invalid')
    _need(type(server['timeout_seconds']) is int and 1 <= server['timeout_seconds'] <= 180,
          'Browser readiness timeout must be 1..180 seconds')
    if server['cwd'] != '.':
        runtime._relative(server['cwd'])
        _need(not server['cwd'].startswith('.harness'), 'Browser server cannot run inside harness state')
    base = origin(server['ready_url'])
    _need(not urlsplit(server['ready_url']).fragment, 'Browser readiness URL cannot have a fragment')
    _need(1 <= len(config['cases']) <= 12, 'Browser assurance needs 1..12 approved cases')
    names = set()
    for case in config['cases']:
        _need(re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', case['id']) is not None and case['id'] not in names,
              'Browser case IDs must be unique portable identifiers')
        names.add(case['id'])
        _need(case['path'].startswith('/') and not case['path'].startswith('//') and '\\' not in case['path']
              and '\x00' not in case['path'] and origin(base + case['path']) == base,
              'Browser cases must remain on the approved loopback origin')
        _need(type(case['width']) is int and 320 <= case['width'] <= 3840
              and type(case['height']) is int and 200 <= case['height'] <= 2160, 'Browser viewport is outside supported bounds')
        _need(len(case['steps']) <= 30 and 1 <= len(case['expect']) <= 20, 'Browser cases need bounded steps and explicit DOM expectations')
        for item in case['steps'] + case['expect']:
            _need(item['selector'].strip() and len(item['selector']) <= 1024 and '\x00' not in item['selector'],
                  'Browser selector is empty or invalid')
        for item in case['expect']:
            _need(item['text'].strip(), 'Browser DOM expectation text cannot be empty')
    return json.loads(json.dumps(config, ensure_ascii=False, allow_nan=False))


def _protected(app, problem, directory):
    return {'app': runtime.snapshot(app, exclude=(directory.relative_to(app).as_posix(), '.harness/work/operation.lock')),
            'problem': runtime.snapshot(problem, exclude=('.harness/work/operation.lock',))}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _port_available(url):
    parsed = urlsplit(url)
    family = socket.AF_INET6 if ipaddress.ip_address(parsed.hostname).version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        try:
            connection.bind((parsed.hostname, parsed.port))
        except OSError as exc:
            raise RuntimeError('Browser server port is already occupied or unavailable; existing services are not reused') from exc


def _wait_ready(process, config, guard):
    deadline = time.monotonic() + config['timeout_seconds']
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    while time.monotonic() < deadline:
        guard()
        if process.poll() is not None:
            raise RuntimeError('Browser server exited before readiness')
        try:
            with opener.open(config['ready_url'], timeout=min(0.5, max(0.05, deadline - time.monotonic()))) as response:
                if 200 <= response.status < 300:
                    return
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError('Browser server readiness timed out')


def _evidence(directory):
    return {path.name: {'sha256': _hash(path), 'size': path.stat().st_size}
            for path in sorted(directory.iterdir()) if path.is_file() and path.name not in ('report.json',)
            and not path.name.endswith(('.tmp', '.intent'))}


def _assemble(app, directory, request, reason=None):
    server_exit = runtime.read_record(directory / 'server.exit.json')
    reason = reason or (server_exit.get('reason') if server_exit else None)
    outcome = None
    if (directory / 'probe.exit.json').exists():
        outcome = runtime.verify_command(directory / 'probe')
    worker = None
    if (directory / 'browser-result.json').exists():
        worker = runtime._decode(runtime._read_bytes(directory / 'browser-result.json'))
        _need(isinstance(worker, dict) and worker.get('version') == 1 and worker.get('engine') == 'playwright'
              and worker.get('browser') == request['config']['browser']
              and worker.get('status') in ('PASS', 'FAIL', 'BLOCKED'), 'Malformed browser probe result')
    passed = bool(outcome and outcome['exit_code'] == 0 and outcome['termination_reason'] == 'completed'
                  and outcome['cleanup']['completed'] and worker and worker['status'] == 'PASS'
                  and server_exit and server_exit['cleanup']['completed'] and not reason)
    cases = worker.get('cases', []) if worker else []
    if passed:
        _need(len(cases) == len(request['config']['cases']), 'Browser probe omitted required cases')
        for expected, observed in zip(request['config']['cases'], cases):
            _need(observed.get('id') == expected['id'] and observed.get('passed') is True
                  and observed.get('width') == expected['width'] and observed.get('height') == expected['height']
                  and observed.get('path') == expected['path'] and observed.get('issues') == []
                  and len(observed.get('assertions', [])) == len(expected['expect'])
                  and all(item.get('passed') is True for item in observed['assertions']), 'Browser case has incomplete passing evidence')
            case_name = 'case-' + expected['id']
            _need(observed.get('screenshot') == case_name + '.png', 'Browser screenshot name differs')
            screenshot = runtime._read_bytes(directory / (case_name + '.png'))
            _need(len(screenshot) > 32 and screenshot[:8] == b'\x89PNG\r\n\x1a\n'
                  and int.from_bytes(screenshot[16:20], 'big') == expected['width']
                  and int.from_bytes(screenshot[20:24], 'big') == expected['height'], 'Browser screenshot is missing or has the wrong viewport')
            _need(runtime._decode(runtime._read_bytes(directory / (case_name + '.json'))) == observed,
                  'Browser case JSON differs from its result')
    try:
        after = runtime.snapshot(app)
    except (OSError, ValueError) as exc:
        after, reason = None, 'Browser source evidence is incomplete: ' + str(exc)
    if after != request['source_before']:
        passed, reason = False, 'Browser execution changed source files'
    status = 'PASS' if passed else 'BLOCKED' if reason or not worker or worker['status'] == 'BLOCKED' else 'FAIL'
    return {'version': 1, 'kind': 'browser', 'passed': passed, 'status': status,
            'reason': reason or (worker.get('reason') if worker else 'Browser probe did not finish'),
            'run_dir': str(directory), 'contract_sha256': hashlib.sha256(runtime._json(request['config'])).hexdigest(),
            'source_before': request['source_before'], 'source_after': after,
            'server': server_exit, 'probe': outcome, 'cases': cases,
            'artifacts': [{'path': str(directory / item['screenshot']), 'case': item['id'], 'kind': 'screenshot',
                           'sha256': _hash(directory / item['screenshot']),
                           'size': (directory / item['screenshot']).stat().st_size}
                          for item in cases if item.get('screenshot')], 'evidence': _evidence(directory)}


def run_browser(app_root, problem_root, config, run_dir, guard=None):
    """Run the approved browser contract once; never install or reuse a profile."""
    config = validate_browser(config)
    app = runtime._ordinary(app_root, directory=True)
    problem = runtime._ordinary(problem_root, directory=True)
    directory = runtime._ordinary(run_dir, directory=True, missing=True)
    _need(directory.is_relative_to(app / '.harness'), 'Browser evidence must be inside application .harness')
    directory.mkdir(parents=True, exist_ok=True)
    receipt = clock.load(problem, required=True)
    request = {'version': 1, 'app_root': str(app), 'problem_root': str(problem), 'config': config,
               'source_before': runtime.snapshot(app), 'problem_receipt': receipt,
               'probe_path': str(PROBE), 'probe_sha256': _hash(PROBE),
               'protected_before': _protected(app, problem, directory)}
    previous = runtime.read_record(directory / 'request.json')
    if previous is None:
        _need(not list(directory.iterdir()), 'Unowned browser execution directory')
        runtime.write_record(directory / 'request.json', request)
    else:
        _need({key: value for key, value in previous.items() if key != 'protected_before'} ==
              {key: value for key, value in request.items() if key != 'protected_before'},
              'Browser source, contract or probe changed; use a new approved attempt')
        request = previous
    saved = runtime.read_record(directory / 'report.json')
    if saved is not None:
        _need(not saved.get('protected_changed'), 'Browser protection failed; preserve evidence before continuing')
        return verify_browser(saved)
    protected = _protected(app, problem, directory)
    protection_error = [None]
    def unchanged():
        try:
            _need(clock.load(problem, required=True) == receipt, 'Original browser deadline receipt changed')
            clock.budget(problem, 1, required=True)
            _need(_protected(app, problem, directory) == protected and _hash(PROBE) == request['probe_sha256'],
                  'Browser execution changed protected source, controller state or input evidence')
            if guard:
                guard()
        except (OSError, ValueError, RuntimeError) as exc:
            protection_error[0] = exc
            raise
    launch_path, exit_path = directory / 'server.launch.json', directory / 'server.exit.json'
    if runtime.read_record(launch_path) is not None:
        ended = runtime.read_record(exit_path)
        _need(ended is not None, 'Browser server has no exit receipt; do not relaunch or kill an unowned PID')
        interrupted_guard = None if protected == request['protected_before'] and not ended.get('protected_changed') else 'Protected inputs changed before browser recovery'
        report = _assemble(app, directory, request, reason=interrupted_guard)
        report['protected_changed'] = interrupted_guard is not None
        runtime.write_record(directory / 'report.json', report)
        _need(interrupted_guard is None, interrupted_guard)
        return verify_browser(report)
    _need(not any((directory / name).exists() for name in
                  ('server.running.json', 'server.exit.json', 'probe.launch.json', 'probe.exit.json', 'browser-result.json')),
          'Browser execution evidence has no server launch binding; do not relaunch')
    unchanged()
    process, outcome, error, interrupted = None, None, None, None
    server = {'version': 1, 'started_at': _now(), 'finished_at': None, 'pid': None, 'exit_code': None,
              'process_started': False, 'cleanup': {'attempted': False, 'completed': False, 'error': None}}
    try:
        _port_available(config['server']['ready_url'])
        cwd = runtime._ordinary(app / config['server']['cwd'], directory=True)
        with (directory / 'server.stdout.log').open('wb') as stdout, (directory / 'server.stderr.log').open('wb') as stderr:
            runtime.write_record(launch_path, {'started_at': server['started_at'], 'argv': config['server']['argv'],
                                              'cwd': str(cwd), 'pid': None})
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                               HARNESS_BROWSER_RUN_DIR=str(directory))
            environment['PATH'] = str(Path(sys.executable).parent) + os.pathsep + environment.get('PATH', '')
            flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == 'nt' else 0
            process = processes.start_process(config['server']['argv'], cwd=str(cwd), stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, env=environment, start_new_session=os.name != 'nt', creationflags=flags)
            server.update(pid=process.pid, process_started=True)
            runtime.write_record(directory / 'server.running.json', {'pid': process.pid, 'started_at': server['started_at']})
            _wait_ready(process, config['server'], unchanged)
            (directory / 'browser-config.json').write_bytes(runtime._json(config))
            argv = [sys.executable, '-B', str(PROBE), '--config', str(directory / 'browser-config.json'),
                    '--output', str(directory), '--deadline', str(receipt['deadline_at'])]
            outcome = runtime.run_command(argv, app, directory / 'probe', problem, 180, guard=unchanged)
            if process.poll() is not None:
                raise RuntimeError('Browser server exited during functional verification')
    except BaseException as exc:
        error = type(exc).__name__ + ': ' + str(exc)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            interrupted = exc
    finally:
        server['cleanup']['attempted'] = process is not None
        try:
            if process is not None:
                processes.stop_process(process)
                server['exit_code'] = process.poll()
            server['cleanup']['completed'] = process is None or process.poll() is not None
        except BaseException as exc:
            server['cleanup']['error'] = type(exc).__name__ + ': ' + str(exc)
            error = error or 'Browser server cleanup could not be proven'
        server['finished_at'] = _now()
        server['reason'] = error
        server['protected_changed'] = protection_error[0] is not None
        if launch_path.exists():
            runtime.write_record(exit_path, server)
    if interrupted is not None:
        raise interrupted
    try:
        unchanged()
    except (OSError, ValueError, RuntimeError) as exc:
        error = error or str(exc)
    report = _assemble(app, directory, request, reason=error)
    report['protected_changed'] = protection_error[0] is not None
    runtime.write_record(directory / 'report.json', report)
    if protection_error[0] is not None:
        raise protection_error[0]
    return verify_browser(report)


def verify_browser(report):
    """Validate historical browser evidence without opening a browser or server."""
    _need(isinstance(report, dict) and report.get('kind') == 'browser' and type(report.get('passed')) is bool,
          'Invalid browser assurance report')
    directory = runtime._ordinary(report['run_dir'], directory=True)
    saved = runtime.read_record(directory / 'report.json')
    request = runtime.read_record(directory / 'request.json')
    _need(saved is not None and request is not None and all(report.get(k) == v for k, v in saved.items()),
          'Browser report differs from its saved evidence')
    validate_browser(request['config'])
    _need(_hash(Path(request['probe_path'])) == request['probe_sha256'], 'Browser probe implementation changed')
    _need(request['problem_receipt'] == clock.load(request['problem_root'], required=True), 'Browser receipt belongs to another problem')
    _need(saved['contract_sha256'] == hashlib.sha256(runtime._json(request['config'])).hexdigest()
          and saved['source_before'] == request['source_before'], 'Browser report contract/source binding differs')
    _need(_evidence(directory) == saved['evidence'], 'Browser logs, screenshot or JSON evidence is missing or changed')
    if (directory / 'server.launch.json').exists():
        launch = runtime.read_record(directory / 'server.launch.json')
        server = runtime.read_record(directory / 'server.exit.json')
        _need(server is not None and server['cleanup']['completed'] is True,
              'Browser server exit/cleanup evidence is incomplete')
        _need(server == saved['server'], 'Browser server exit result differs')
        _need(launch.get('argv') == request['config']['server']['argv']
              and launch.get('cwd') == str(Path(request['app_root']) / request['config']['server']['cwd'])
              and launch.get('started_at') == server.get('started_at'), 'Browser server launch contract differs')
        if server.get('process_started'):
            running = runtime.read_record(directory / 'server.running.json')
            _need(running is not None and running.get('pid') == server.get('pid')
                  and running.get('started_at') == server.get('started_at'), 'Browser server process identity differs')
    if saved['probe'] is not None:
        actual = runtime.verify_command(directory / 'probe')
        _need(all(actual.get(k) == saved['probe'].get(k) for k in
                  ('exit_code', 'termination_reason', 'cleanup', 'error', 'stdout_path', 'stderr_path')),
              'Browser process evidence differs')
    if saved['passed']:
        rebuilt = _assemble(Path(request['app_root']), directory, request)
        # Later approved document/code changes are checked separately by controller.
        _need(rebuilt['cases'] == saved['cases'] and saved['source_before'] == saved['source_after']
              and saved['probe']['exit_code'] == 0 and saved['server']['cleanup']['completed']
              and saved['server']['process_started'],
              'Browser passing evidence is incomplete')
    return saved
