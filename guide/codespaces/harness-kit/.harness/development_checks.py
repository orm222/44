"""Measured test results and ordered, locally executed Docker verification.

Uses the existing process lifecycle and deadline through development_runtime.
This is a Python 3.9 implementation; it does not import OOI's Python 3.11 CLI.
"""
import importlib.util
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
import time
import uuid


runtime = None


def _runtime():
    global runtime
    if runtime is None:
        spec = importlib.util.spec_from_file_location('checks_development_runtime',
            Path(__file__).with_name('development_runtime.py'))
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
    return runtime


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError(label + ' must be a nonempty string')
    return value


def _relative(value, label, dot=False):
    _text(value, label)
    path = PurePosixPath(value)
    if ('\\' in value or ':' in value or path.is_absolute() or PureWindowsPath(value).is_absolute()
            or '..' in path.parts or (not dot and str(path) == '.')):
        raise ValueError(label + ' must stay within its project')
    return path.as_posix()


def _argv(value, label, empty=False):
    if not isinstance(value, list) or (not value and not empty):
        raise ValueError(label + ' must be an argument array')
    for part in value:
        _text(part, label)
    return list(value)


def _positive(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(label + ' must be a positive integer')
    return value


def validate_checks(checks):
    if not isinstance(checks, list) or not checks:
        raise ValueError('At least one mandatory verification check is required')
    normalized, ids = [], set()
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError('Check must be an object')
        identifier = _text(check.get('id'), 'Check id')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', identifier) or identifier in ids:
            raise ValueError('Check ids must be unique safe names')
        ids.add(identifier)
        framework = check.get('test_framework')
        if framework not in ('unittest', 'pytest', 'node', 'command'):
            raise ValueError('Unsupported test_framework: ' + str(framework))
        normalized.append(dict(id=identifier, argv=_argv(check.get('argv'), 'Check argv'),
            cwd=_relative(check.get('cwd', '.'), 'Check cwd', dot=True),
            timeout_seconds=_positive(check.get('timeout_seconds', 300), 'Check timeout'),
            test_framework=framework, min_tests=_positive(check.get('min_tests'), 'min_tests'),
            requirement=_text(check.get('requirement'), 'Check requirement'),
            oracle=_text(check.get('oracle'), 'Independent expected result / oracle')))
    return normalized


def _within(root, relative, directory=False):
    root = Path(root).absolute()
    path = root / _relative(relative, 'Project path', dot=directory)
    if root != root.resolve() or path.absolute() != path.resolve() or not path.resolve().is_relative_to(root):
        raise ValueError('Redirected or escaping verification path: ' + relative)
    for member in (path, *path.parents):
        if member.is_symlink() or (member.exists() and getattr(member.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Redirected verification path: ' + str(member))
        if member == root:
            break
    if directory and not path.is_dir():
        raise ValueError('Missing verification working directory: ' + str(path))
    if not directory and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError('Verification input must be an ordinary file: ' + str(path))
    return path


def _log(outcome, key):
    value = outcome.get(key + '_path')
    return Path(value).read_text(encoding='utf-8', errors='replace') if value else ''


def measured_result(framework, text):
    """Parse executed tests; a command's zero exit alone is not test evidence."""
    result = dict(tests=0, failures=0, errors=0, skipped=0, measured=False)
    if framework == 'unittest':
        matches = re.findall(r'^Ran (\d+) tests? in ', text, re.MULTILINE)
        if len(matches) == 1:
            result.update(tests=int(matches[0]), measured=True)
        summary = re.search(r'^(?:FAILED|OK)(?: \(([^\n]*)\))?\s*$', text, re.MULTILINE)
        if not summary:
            result['measured'] = False
        for key, count in re.findall(r'\b(failures|errors|skipped)=(\d+)', summary.group(1) or '' if summary else ''):
            result[key] = int(count)
    elif framework == 'pytest':
        lines = [line for line in text.splitlines() if re.search(r'\b\d+ (?:passed|failed|error|errors|skipped|deselected|xfailed|xpassed)\b', line)
                 and re.search(r'\bin \d+(?:\.\d+)?s\b', line)]
        if len(lines) == 1:
            counts = {key: int(count) for count, key in re.findall(
                r'(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b', lines[0])}
            result.update(tests=counts.get('passed', 0) + counts.get('failed', 0) + counts.get('xpassed', 0),
                failures=counts.get('failed', 0), errors=counts.get('error', 0) + counts.get('errors', 0),
                skipped=counts.get('skipped', 0) + counts.get('xfailed', 0), measured=True)
    elif framework == 'node':
        counts = {key: int(count) for key, count in re.findall(
            r'^[#ℹ]\s+(tests|pass|fail|cancelled|skipped|todo)\s+(\d+)\s*$', text, re.MULTILINE)}
        if all(key in counts for key in ('tests', 'pass', 'fail')):
            result.update(tests=counts['tests'], failures=counts['fail'], errors=counts.get('cancelled', 0),
                skipped=counts.get('skipped', 0) + counts.get('todo', 0), measured=True)
    else:
        records = re.findall(r'^HARNESS_TEST_RESULT (\{[^\n]+\})\s*$', text, re.MULTILINE)
        if len(records) == 1:
            try:
                counts = json.loads(records[0])
                if (isinstance(counts, dict) and set(counts) == {'tests', 'failures', 'errors', 'skipped'}
                        and all(type(value) is int and value >= 0 for value in counts.values())):
                    result.update(counts, measured=True)
            except ValueError:
                pass
    if result['failures'] + result['errors'] + result['skipped'] > result['tests'] and framework != 'pytest':
        result['measured'] = False
    return result


def _check_result(check, outcome, expected):
    output = _log(outcome, 'stdout') + '\n' + _log(outcome, 'stderr')
    measured = measured_result(check['test_framework'], output)
    executed = measured['tests'] - (measured['skipped'] if check['test_framework'] != 'pytest' else 0)
    enough = measured['measured'] and executed >= check['min_tests']
    clean = (outcome.get('cleanup', {}).get('completed') is True
             and outcome.get('termination_reason') == 'completed' and not outcome.get('error'))
    observed_failure = False
    if expected == 'pass':
        passed = clean and outcome.get('exit_code') == 0 and enough and measured['failures'] == measured['errors'] == 0
    else:
        # Collection, import and syntax errors are not a demonstrated behavior failure.
        invalid = re.search(r'(?:SyntaxError|IndentationError|ModuleNotFoundError|ImportError|ERROR collecting|testCodeFailure.*ERR_MODULE_NOT_FOUND)', output)
        assertion = (check['test_framework'] in ('unittest', 'command') or
                     bool(re.search(r'AssertionError|ERR_ASSERTION|(?:^|\n)\s*E\s+assert\b', output)))
        observed_failure = bool(clean and outcome.get('exit_code') not in (None, 0) and enough
                  and measured['failures'] > 0 and measured['errors'] == 0 and not invalid and assertion)
        already_passes = clean and outcome.get('exit_code') == 0 and enough and measured['failures'] == measured['errors'] == 0
        passed = observed_failure or already_passes
    return dict(id=check['id'], passed=bool(passed), expected=expected, measurement=measured,
        argv=check['argv'], cwd=check['cwd'], test_framework=check['test_framework'], min_tests=check['min_tests'],
        observed_failure=observed_failure, requirement=check['requirement'], oracle=check['oracle'], outcome=outcome,
        reason='' if passed else 'Required measured test result, cleanup, or expected outcome was not satisfied')


def _protected_snapshot(app_root, run_dir):
    app_root, run_dir = Path(app_root).absolute(), Path(run_dir).absolute()
    if run_dir == app_root or run_dir != run_dir.resolve():
        raise ValueError('Verification output must be an ordinary dedicated run directory')
    exclusions = (run_dir.relative_to(app_root).as_posix(),) if run_dir.is_relative_to(app_root) else ()
    # Exclude only this runner's own files, never all controller/earlier evidence.
    return _runtime().snapshot(app_root, exclude=exclusions)


def run_checks(app_root, problem_root, checks, run_dir, expected='pass', guard=None):
    if expected not in ('pass', 'fail'):
        raise ValueError('expected must be pass or fail')
    checks = validate_checks(checks)
    rt, app_root, run_dir = _runtime(), Path(app_root), Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    before = rt.snapshot(app_root)
    request = dict(app_root=str(app_root.resolve()), problem_root=str(Path(problem_root).resolve()),
                   checks=checks, expected=expected, source_before=before)
    prior_request = rt.read_record(run_dir / 'request.json')
    if prior_request is not None and prior_request != request:
        raise ValueError('Test source or verification contract changed; use a new run directory')
    if prior_request is None:
        rt.write_record(run_dir / 'request.json', request)
    protected = _protected_snapshot(app_root, run_dir)

    def unchanged():
        if _protected_snapshot(app_root, run_dir) != protected:
            raise ValueError('Verification changed protected project source, controller state, or prior evidence')
        if guard:
            guard()

    results, blocked = [], False
    for check in checks:
        try:
            cwd = _within(app_root, check['cwd'], directory=True)
            outcome = rt.run_command(check['argv'], cwd, run_dir / check['id'] / 'check', problem_root,
                                     check['timeout_seconds'], guard=unchanged)
            unchanged()
            result = _check_result(check, outcome, expected)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            blocked = isinstance(exc, FileNotFoundError) or 'deadline' in str(exc).lower()
            result = dict(id=check['id'], passed=False, expected=expected, reason=str(exc),
                argv=check['argv'], cwd=check['cwd'], test_framework=check['test_framework'], min_tests=check['min_tests'],
                requirement=check['requirement'], oracle=check['oracle'])
        results.append(result)
        if not result['passed']:
            break
    after = rt.snapshot(app_root)
    protected_changed = _protected_snapshot(app_root, run_dir) != protected
    passed = (len(results) == len(checks) and all(item['passed'] for item in results) and before == after
              and not protected_changed
              and (expected == 'pass' or any(item.get('observed_failure') for item in results)))
    report = dict(passed=passed, status='PASS' if passed else 'BLOCKED' if blocked or protected_changed else 'FAIL',
        expected=expected, checks=results, source_before=before, source_after=after, protected_changed=protected_changed,
        run_dir=str(run_dir.resolve()))
    rt.write_record(run_dir / 'report.json', report)
    return report


def _verify_outcome(outcome):
    if not isinstance(outcome, dict) or not outcome.get('prefix'):
        raise ValueError('Verification outcome has no durable command prefix')
    actual = _runtime().verify_command(Path(outcome['prefix']))
    for key in ('exit_code', 'cleanup', 'termination_reason', 'error', 'stdout_path', 'stderr_path'):
        if outcome.get(key) != actual.get(key):
            raise ValueError('Recorded verification outcome differs from command evidence: ' + key)
    return actual


def verify_report(report):
    """Verify historical evidence without rerunning tests or requiring old source.

The controller separately checks current source and approval bindings. Failure
records may lack a launched process; no successful record may omit its proof.
"""
    if not isinstance(report, dict) or type(report.get('passed')) is not bool:
        raise ValueError('Invalid verification report')
    if 'expected' in report:
        expected = report['expected']
        if expected not in ('pass', 'fail') or not isinstance(report.get('checks'), list):
            raise ValueError('Invalid test verification report')
        if not report.get('run_dir'):
            raise ValueError('Test report has no original verification contract')
        request = _runtime().read_record(Path(report['run_dir']) / 'request.json')
        if (request is None or request.get('expected') != expected
                or request.get('source_before') != report.get('source_before')):
            raise ValueError('Test report differs from its original source/verification contract')
        required = validate_checks(request.get('checks'))
        if (len(report['checks']) > len(required)
                or (report['passed'] and len(report['checks']) != len(required))):
            raise ValueError('Required verification checks are missing from the report')
        checked, saw_failure = [], False
        for index, item in enumerate(report['checks']):
            contract = required[index]
            for key in ('id', 'argv', 'cwd', 'test_framework', 'min_tests', 'requirement', 'oracle'):
                if item.get(key) != contract[key]:
                    raise ValueError('Recorded check differs from its approved contract: ' + key)
            outcome = item.get('outcome')
            if outcome is None:
                if item.get('passed') or report['passed']:
                    raise ValueError('Successful check has no command evidence')
                checked.append(False)
                continue
            actual = _verify_outcome(outcome)
            invocation = _runtime().read_record(Path(outcome['prefix']).with_suffix('.request.json'))
            expected_cwd = str(Path(request['app_root']) / contract['cwd'])
            if (invocation.get('argv') != contract['argv'] or invocation.get('cwd') != expected_cwd
                    or invocation.get('timeout') != contract['timeout_seconds']):
                raise ValueError('Actual test invocation differs from its approved contract')
            reconstructed = _check_result(item, actual, expected)
            for key in ('passed', 'measurement', 'observed_failure'):
                if item.get(key) != reconstructed[key]:
                    raise ValueError('Recorded test result differs from measured evidence: ' + key)
            checked.append(reconstructed['passed'])
            saw_failure = saw_failure or reconstructed['observed_failure']
        if report['passed'] and (not checked or not all(checked) or
                (expected == 'fail' and not saw_failure) or report.get('source_before') != report.get('source_after')):
            raise ValueError('Successful report has incomplete required test proof')
        return True
    if 'stages' in report:
        if report.get('preflight', {}).get('outcome'):
            _verify_outcome(report['preflight']['outcome'])
        for stage in report['stages']:
            if stage.get('outcome'):
                actual = _verify_outcome(stage['outcome'])
                if stage.get('passed') != _successful(actual):
                    raise ValueError('Docker stage differs from its command outcome')
            if stage.get('report'):
                verify_report(stage['report'])
                if stage.get('passed') != stage['report']['passed']:
                    raise ValueError('Docker functional result differs from its measured checks')
        if report['passed']:
            if ([stage.get('stage') for stage in report['stages']] != ['config', 'build', 'start', 'verify']
                    or not all(stage.get('passed') for stage in report['stages'])
                    or report.get('preflight', {}).get('passed') is not True
                    or report.get('cleanup', {}).get('completed') is not True
                    or report.get('source_before') != report.get('source_after')):
                raise ValueError('Successful Docker report has missing stages, cleanup, or matching source')
            if not report.get('run_dir') or not report.get('evidence'):
                raise ValueError('Successful Docker report has no sealed evidence inventory')
        if report.get('evidence'):
            root = Path(report['run_dir'])
            for relative, checksum in report['evidence'].items():
                path = _within(root, relative)
                if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                    raise ValueError('Docker verification evidence changed: ' + relative)
        return True
    raise ValueError('Unrecognized verification report')


def validate_docker(config):
    if not isinstance(config, dict):
        raise ValueError('Docker configuration must be an object')
    normalized = dict(dockerfile=_relative(config.get('dockerfile', 'Dockerfile'), 'Dockerfile'),
        context=_relative(config.get('context', '.'), 'Docker context', dot=True),
        command=_argv(config.get('command', []), 'Container command', empty=True),
        checks=validate_checks(config.get('checks')),
        compose_file=config.get('compose_file'), service=config.get('service'))
    for field in ('host_port', 'container_port'):
        value = config.get(field, 0)
        if type(value) is not int or not 0 <= value <= 65535:
            raise ValueError(field + ' must be an integer between 0 and 65535')
        normalized[field] = value
    if bool(normalized['host_port']) != bool(normalized['container_port']):
        raise ValueError('host_port and container_port must both be zero or both be specified')
    if normalized['compose_file'] is not None:
        normalized['compose_file'] = _relative(normalized['compose_file'], 'Compose file')
        normalized['service'] = _text(normalized['service'], 'Compose service')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', normalized['service']):
            raise ValueError('Invalid Compose service')
        if normalized['host_port'] or normalized['command']:
            raise ValueError('Configure Compose ports and commands in the approved Compose file')
    elif normalized['service'] is not None:
        raise ValueError('Compose service requires compose_file')
    if any(check['cwd'] != '.' for check in normalized['checks']):
        raise ValueError('Container checks use the image WORKDIR; set check cwd to .')
    return normalized


def preflight(app_root, problem_root, run_dir, guard=None, strict=False):
    rt, run_dir = _runtime(), Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    protected = _protected_snapshot(app_root, run_dir)
    def unchanged():
        if _protected_snapshot(app_root, run_dir) != protected:
            raise ValueError('Docker preflight changed protected source or controller evidence')
        if guard:
            guard()
    try:
        runner = _availability_command if strict else rt.run_command
        result = runner(['docker', 'info', '--format', '{{.ServerVersion}}'], Path(app_root),
            run_dir / 'docker-info', problem_root, 30, guard=unchanged)
        unchanged()
        passed = (result.get('exit_code') == 0 and result.get('cleanup', {}).get('completed') is True
                  and result.get('termination_reason') == 'completed' and bool(_log(result, 'stdout').strip()))
        report = dict(passed=passed, status='PASS' if passed else 'BLOCKED', outcome=result,
            reason='' if passed else 'Docker CLI/engine is unavailable; Docker completion is blocked')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        if strict:
            raise
        report = dict(passed=False, status='BLOCKED', reason=str(exc))
    if _protected_snapshot(app_root, run_dir) != protected:
        raise ValueError('Docker preflight changed protected source or controller evidence')
    rt.write_record(run_dir / 'preflight.json', report)
    return report


def _availability_command(argv, cwd, prefix, problem_root, timeout, guard=None):
    """Only a proven ended execution may become an environmental BLOCKED result."""
    rt, guard_errors = _runtime(), []
    def checked():
        try:
            rt.problem_time.budget(problem_root, 1, required=True)
            if guard:
                guard()
        except BaseException as exc:
            guard_errors.append(exc)
            raise
    checked()
    try:
        outcome = rt.run_command(argv, cwd, prefix, problem_root, timeout, guard=checked)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        if guard_errors:
            raise guard_errors[0]
        # Missing exit, incomplete cleanup, or altered evidence raises here. An
        # executable which never started has an explicit launch_failed receipt.
        outcome = rt.verify_command(prefix)
    checked()
    if outcome.get('termination_reason') in ('guard_failed', 'cleanup_failed', 'cancelled'):
        raise ValueError('Readiness execution was interrupted unsafely; preserve its evidence')
    return outcome


def _quality():
    loader = importlib.util.spec_from_file_location('readiness_quality', Path(__file__).with_name('assurance_quality.py'))
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    return module


# A metadata-only subprocess: no browser launch, navigation or installation.
# Ask the installed driver registry for its own exact executable paths rather
# than guessing cache revisions. A changed/unsupported registry API blocks the
# check explicitly; actual browser execution is still tested at the web gate.
_BROWSER_AVAILABILITY = r'''
import importlib.metadata, json, os, subprocess, sys
from pathlib import Path
result = {'passed': False, 'browser': sys.argv[1], 'reason': '', 'executables': []}
try:
    import playwright
    from playwright.sync_api import BrowserContext, BrowserType, Locator, Page, expect
    from playwright._impl._driver import compute_driver_executable
    required = ((BrowserContext, 'route_web_socket'), (BrowserContext, 'new_page'),
                (BrowserType, 'launch'), (Page, 'screenshot'), (Locator, 'fill'))
    if not all(hasattr(owner, name) for owner, name in required):
        raise RuntimeError('Installed Playwright API is incompatible with the browser checks')
    result['version'] = importlib.metadata.version('playwright')
    node = compute_driver_executable()[0]
    registry = Path(playwright.__file__).parent / 'driver' / 'package' / 'lib' / 'server' / 'registry' / 'index.js'
    script = """
const r = require(process.argv[1]).registry;
const browser = process.argv[2];
const engine = browser === 'chromium' ? (r.findExecutable('chromium-headless-shell') || r.findExecutable(browser)) : r.findExecutable(browser);
if (!engine || typeof engine.executablePath !== 'function') throw Error('Unsupported Playwright registry API');
console.log(JSON.stringify({name:engine.name, path:engine.executablePath()}));
"""
    proc = subprocess.run([str(node), '-e', script, str(registry), sys.argv[1]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if proc.returncode:
        raise RuntimeError('Playwright executable registry could not be inspected')
    executable = json.loads(proc.stdout.decode('utf-8'))
    path = Path(executable['path']) if executable.get('path') else None
    if not path or not path.is_file() or (os.name != 'nt' and not os.access(str(path), os.X_OK)):
        raise RuntimeError('Required browser executable is not installed: ' + str(executable.get('name')))
    result['executables'] = [executable]
    result['passed'] = True
except Exception as exc:
    result['reason'] = type(exc).__name__ + ': ' + str(exc)
result['scope'] = 'Package API and installed engine only; runtime OS libraries are checked by the later actual browser test'
print(json.dumps(result, ensure_ascii=False))
'''


def _browser_availability(outcome, browser):
    try:
        value = _runtime()._decode(_log(outcome, 'stdout').encode('utf-8'))
        if not isinstance(value, dict) or type(value.get('passed')) is not bool or value.get('browser') != browser:
            raise ValueError('Unexpected browser availability result')
        if value['passed'] and (not value.get('version') or not value.get('executables')):
            raise ValueError('Browser availability lacks package/executable evidence')
    except (OSError, ValueError, UnicodeError) as exc:
        value = dict(passed=False, browser=browser, reason='Browser availability could not be measured: ' + str(exc))
    return dict(outcome=outcome, availability=value,
                passed=_successful(outcome) and value['passed'])


def _readiness_contract(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get('docker'), dict):
        raise ValueError('Readiness requires the approved Docker plan')
    assurance = plan.get('assurance') or {}
    project_type = assurance.get('project_type', 'non_web')
    if project_type not in ('web', 'non_web'):
        raise ValueError('Unknown readiness project_type')
    browser = None
    if project_type == 'web':
        browser = (assurance.get('browser') or {}).get('config', {}).get('browser')
        if browser not in ('chromium', 'firefox', 'webkit'):
            raise ValueError('Web readiness requires the selected browser configuration')
    elif assurance.get('browser') is not None:
        raise ValueError('Non-web readiness cannot request browser inspection')
    environment = assurance.get('environment', dict(executables=[], python_packages=[], ports=[]))
    _runtime().validate_schema(environment, _quality().ENVIRONMENT_SCHEMA)
    return dict(compose=plan['docker'].get('compose_file') is not None,
                environment=environment, browser=browser, project_type=project_type)


def readiness(app_root, problem_root, plan, run_dir, guard=None):
    """Measure approved prerequisites before RED, without installing or building."""
    rt = _runtime()
    app_root, problem_root, run_dir = Path(app_root).absolute(), Path(problem_root).absolute(), Path(run_dir).absolute()
    contract = _readiness_contract(plan)
    run_dir.mkdir(parents=True, exist_ok=True)
    request = dict(version=1, app_root=str(app_root), problem_root=str(problem_root),
        plan_sha256=hashlib.sha256(rt._json(plan)).hexdigest(), contract=contract,
        python_executable=sys.executable, source_before=rt.snapshot(app_root),
        problem_before=rt.snapshot(problem_root), problem_receipt=rt.problem_time.load(problem_root, required=True))
    previous = rt.read_record(run_dir / 'request.json')
    if previous is not None and previous != request:
        raise ValueError('Readiness source, inputs or plan changed; preserve the attempt and use a new one')
    if previous is None:
        if any(run_dir.iterdir()):
            raise ValueError('Unbound readiness evidence already exists; preserve the attempt')
        rt.write_record(run_dir / 'request.json', request)
    protected = _protected_snapshot(app_root, run_dir)
    problem_protected = _protected_snapshot(problem_root, run_dir)
    def unchanged():
        if (_protected_snapshot(app_root, run_dir) != protected or
                _protected_snapshot(problem_root, run_dir) != problem_protected):
            raise ValueError('Readiness changed protected source, inputs or controller evidence')
        if rt.problem_time.load(problem_root, required=True) != request['problem_receipt']:
            raise ValueError('Original readiness deadline receipt changed')
        rt.problem_time.budget(problem_root, 1, required=True)
        if guard:
            guard()
    unchanged()
    cached = rt.read_record(run_dir / 'report.json')
    if cached is not None:
        verify_readiness(cached)
        unchanged()
        return cached
    environment = rt.read_record(run_dir / 'environment.json')
    if environment is None:
        environment = _quality().inspect_environment(contract['environment'])
        unchanged()
        rt.write_record(run_dir / 'environment.json', environment)
    docker = preflight(app_root, problem_root, run_dir / 'docker', guard=unchanged, strict=True)
    compose = None
    if contract['compose']:
        outcome = _availability_command(['docker', 'compose', 'version', '--short'], app_root,
            run_dir / 'compose-version', problem_root, 30, guard=unchanged)
        compose = dict(passed=_successful(outcome) and bool(_log(outcome, 'stdout').strip()), outcome=outcome)
    browser = None
    if contract['browser'] is not None:
        outcome = _availability_command([sys.executable, '-c', _BROWSER_AVAILABILITY, contract['browser']],
            app_root, run_dir / 'browser-availability', problem_root, 30, guard=unchanged)
        browser = _browser_availability(outcome, contract['browser'])
    unchanged()
    reasons = []
    if environment.get('passed') is not True:
        reasons.append('Required environment unavailable: ' + ', '.join(environment.get('missing_required', [])))
    if not docker['passed']:
        reasons.append(docker['reason'])
    if compose is not None and not compose['passed']:
        reasons.append('Docker Compose is unavailable')
    if browser is not None and not browser['passed']:
        reasons.append('Browser: ' + browser['availability'].get('reason', 'availability check failed'))
    report = dict(kind='readiness', passed=not reasons, status='BLOCKED' if reasons else 'PASS',
        reason='; '.join(reasons), run_dir=str(run_dir), plan_sha256=request['plan_sha256'],
        source_before=request['source_before'], source_after=rt.snapshot(app_root),
        environment=environment, docker=docker, compose=compose, browser=browser)
    unchanged()
    rt.write_record(run_dir / 'report.json', report)
    return report


def verify_readiness(report):
    """Verify historical readiness evidence; never launch tools or reset time."""
    if not isinstance(report, dict) or report.get('kind') != 'readiness' or not report.get('run_dir'):
        raise ValueError('Invalid readiness report')
    rt, run_dir = _runtime(), Path(report['run_dir'])
    saved = rt.read_record(run_dir / 'report.json')
    if not isinstance(saved, dict) or any(report.get(key) != value for key, value in saved.items()):
        raise ValueError('Readiness report differs from its preserved evidence')
    request = rt.read_record(run_dir / 'request.json')
    if not request or report['plan_sha256'] != request['plan_sha256']:
        raise ValueError('Readiness has no matching plan contract')
    if (report['source_before'] != request['source_before'] or report['source_after'] != request['source_before']
            or request['problem_receipt'] != rt.problem_time.load(request['problem_root'], required=True)):
        raise ValueError('Readiness source or original deadline evidence differs')
    contract = request['contract']
    environment = rt.read_record(run_dir / 'environment.json')
    if environment != report['environment']:
        raise ValueError('Readiness environment evidence differs')
    def command(outcome, prefix, argv):
        if outcome.get('prefix') != str(prefix):
            raise ValueError('Readiness command belongs to another attempt')
        recorded = rt.read_record(prefix.with_suffix('.request.json'))
        if (not recorded or recorded['argv'] != argv or recorded['cwd'] != request['app_root']
                or recorded['problem_root'] != request['problem_root'] or recorded['timeout'] != 30):
            raise ValueError('Readiness command differs from its approved contract')
        actual = _verify_outcome(outcome)
        if actual.get('termination_reason') in ('guard_failed', 'cleanup_failed', 'cancelled'):
            raise ValueError('Readiness contains an interrupted protected execution')
    docker = report['docker']
    if rt.read_record(run_dir / 'docker' / 'preflight.json') != docker:
        raise ValueError('Readiness Docker evidence differs')
    command(docker['outcome'], run_dir / 'docker' / 'docker-info', ['docker', 'info', '--format', '{{.ServerVersion}}'])
    docker_pass = _successful(docker['outcome']) and bool(_log(docker['outcome'], 'stdout').strip())
    if docker['passed'] != docker_pass:
        raise ValueError('Docker readiness verdict differs from execution evidence')
    passed = environment.get('passed') is True and docker_pass
    compose = report['compose']
    if contract['compose'] != (compose is not None):
        raise ValueError('Readiness Compose coverage differs')
    if compose is not None:
        command(compose['outcome'], run_dir / 'compose-version', ['docker', 'compose', 'version', '--short'])
        measured = _successful(compose['outcome']) and bool(_log(compose['outcome'], 'stdout').strip())
        if compose['passed'] != measured:
            raise ValueError('Compose readiness verdict differs')
        passed = passed and measured
    browser = report['browser']
    if (contract['browser'] is not None) != (browser is not None):
        raise ValueError('Readiness browser coverage differs')
    if browser is not None:
        command(browser['outcome'], run_dir / 'browser-availability',
                [request['python_executable'], '-c', _BROWSER_AVAILABILITY, contract['browser']])
        if _browser_availability(browser['outcome'], contract['browser']) != browser:
            raise ValueError('Browser readiness result differs from execution evidence')
        passed = passed and browser['passed']
    if report['passed'] != passed or report['status'] != ('PASS' if passed else 'BLOCKED'):
        raise ValueError('Readiness verdict differs from measured prerequisites')
    return True


def _cleanup(argv, cwd, directory):
    """Cleanup remains bounded and available after the original deadline expires."""
    loader = importlib.util.spec_from_file_location('docker_cleanup_processes', Path(__file__).with_name('processes.py'))
    processes = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(processes)
    started, process, error, code = time.monotonic(), None, None, None
    stdout, stderr = directory / 'cleanup.stdout.log', directory / 'cleanup.stderr.log'
    try:
        with stdout.open('wb') as out, stderr.open('wb') as err:
            process = processes.start_process(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                start_new_session=os.name != 'nt',
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
            code = process.wait(timeout=20)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        error = str(exc)
    finally:
        if process is not None:
            try:
                processes.stop_process(process)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                error = str(exc)
    missing = False
    if error is None and code == 1 and argv[:3] == ['docker', 'rm', '--force']:
        messages = [line.strip() for line in stderr.read_text(encoding='utf-8', errors='replace').splitlines() if line.strip()]
        missing = bool(messages) and all(re.fullmatch(r'(?:Error response from daemon: )?No such container: ' + re.escape(argv[3]),
                                                    line, re.IGNORECASE) for line in messages)
    result = dict(argv=argv, completed=(code == 0 or missing) and error is None, exit_code=code, error=error,
        seconds=time.monotonic() - started, stdout_path=str(stdout), stderr_path=str(stderr))
    _runtime().write_record(directory / 'cleanup.json', result)
    return result


def _successful(outcome):
    return (outcome.get('exit_code') == 0 and outcome.get('cleanup', {}).get('completed') is True
            and outcome.get('termination_reason') == 'completed' and not outcome.get('error'))


def _host_input(value, base, roots, label, directory=False):
    """Accept existing ordinary inputs under explicitly authorized local roots."""
    if not isinstance(value, str) or not value or '\x00' in value or '${' in value or '://' in value:
        raise ValueError(label + ' must be a resolved local path')
    path = Path(value)
    if not path.is_absolute():
        path = Path(base) / path
    path = path.absolute()
    roots = [Path(root).absolute() for root in roots]
    if path != path.resolve() or not any(path.is_relative_to(root) for root in roots):
        raise ValueError(label + ' must stay inside the approved project/input folders')
    for member in (path, *path.parents):
        if member.is_symlink() or (member.exists() and getattr(member.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError(label + ' must not use redirected paths')
        if member in roots:
            break
    if not path.exists() or (directory and not path.is_dir()) or (not directory and not (path.is_dir() or path.is_file())):
        raise ValueError(label + ' must refer to an existing ordinary input')
    if path.is_file() and path.stat().st_nlink != 1:
        raise ValueError(label + ' must not use hard-linked inputs')
    return path


def _build_inputs(build, app_root):
    if isinstance(build, str):
        build = {'context': build}
    if not isinstance(build, dict):
        raise ValueError('Compose build must describe a local context')
    context = _host_input(build.get('context', '.'), app_root, [app_root], 'Compose build context', directory=True)
    if '.harness' in context.relative_to(app_root).parts:
        raise ValueError('Execution evidence cannot be a Docker build context')
    if build.get('privileged') or build.get('entitlements') or build.get('ssh') or build.get('secrets'):
        raise ValueError('Compose builds cannot request host privileges or credential forwarding')
    if build.get('network') == 'host':
        raise ValueError('Compose builds cannot use the host network namespace')
    additional = build.get('additional_contexts', {})
    if not isinstance(additional, dict):
        raise ValueError('Additional build contexts must be resolved local mappings')
    for value in additional.values():
        extra = _host_input(value, app_root, [app_root], 'Additional build context', directory=True)
        if extra == app_root or '.harness' in extra.relative_to(app_root).parts:
            raise ValueError('Additional contexts must not include execution evidence')
    filename = None
    if 'dockerfile_inline' in build:
        if not isinstance(build['dockerfile_inline'], str) or build.get('dockerfile'):
            raise ValueError('Compose must select one Dockerfile form')
    else:
        filename = _host_input(build.get('dockerfile', 'Dockerfile'), context, [app_root], 'Compose Dockerfile')
        if not filename.is_file():
            raise ValueError('Compose Dockerfile must be a file')
    if context == app_root:
        _require_harness_ignore(app_root, filename)


def _require_harness_ignore(app_root, dockerfile=None):
    name = '.dockerignore'
    if dockerfile is not None:
        specific = Path(str(dockerfile) + '.dockerignore')
        if specific.exists() or specific.is_symlink():
            name = specific.relative_to(app_root).as_posix()
    ignore = _within(app_root, name).read_text(encoding='utf-8-sig').splitlines()
    significant = [line.strip().rstrip('/') for line in ignore if line.strip() and not line.lstrip().startswith('#')]
    if not significant or significant[-1] not in ('.harness', '**/.harness'):
        raise ValueError('Put .harness last in .dockerignore to exclude execution evidence from the build context')


def _validate_compose(description, app_root, problem_root, project, selected_service):
    """Constrain rendered Compose resources to this run and approved input paths."""
    app_root, problem_root = Path(app_root).absolute(), Path(problem_root).absolute()
    if not isinstance(description, dict) or not isinstance(description.get('services'), dict):
        raise ValueError('Compose config must contain service objects')
    services = description['services']
    if selected_service not in services:
        raise ValueError('Compose must include the selected service')
    resources = {}
    for kind in ('volumes', 'networks', 'configs', 'secrets'):
        entries = description.get(kind, {})
        if not isinstance(entries, dict):
            raise ValueError('Compose resource definitions must be objects')
        resources[kind] = entries
        for key, value in entries.items():
            value = value or {}
            if not isinstance(key, str) or not isinstance(value, dict):
                raise ValueError('Invalid Compose resource definition')
            if value.get('external') or value.get('name') not in (None, project + '_' + key):
                raise ValueError('Compose volumes/networks/configs/secrets must belong to this unique project')
            if value.get('driver_opts') or value.get('driver') not in (None, '', 'local' if kind == 'volumes' else 'bridge'):
                raise ValueError('Compose resources cannot attach host devices or external storage drivers')
            if kind in ('configs', 'secrets'):
                _host_input(value.get('file'), app_root, [app_root, problem_root], 'Compose config/secret input')
    for service in services.values():
        if not isinstance(service, dict):
            raise ValueError('Compose services must be objects')
        if service.get('container_name'):
            raise ValueError('Compose must not use fixed container_name')
        if any(service.get(key) for key in ('privileged', 'devices', 'device_cgroup_rules', 'volumes_from', 'cap_add')):
            raise ValueError('Compose services cannot request host privileges, devices, or shared container volumes')
        if any(option not in ('no-new-privileges', 'no-new-privileges:true') for option in service.get('security_opt', [])):
            raise ValueError('Compose services cannot relax host security profiles')
        for key in ('network_mode', 'pid', 'ipc', 'uts', 'userns_mode', 'cgroup'):
            mode = service.get(key)
            if mode in (None, '', 'none', 'private', 'shareable'):
                continue
            if isinstance(mode, str) and mode.startswith('service:') and mode[len('service:'):] in services:
                continue
            raise ValueError('Compose services cannot join host or external namespaces')
        if service.get('build') is not None:
            _build_inputs(service['build'], app_root)
        for mount in service.get('volumes', []):
            if not isinstance(mount, dict):
                raise ValueError('Compose mounts must be rendered objects')
            kind = mount.get('type')
            if kind == 'bind':
                if mount.get('read_only') is not True:
                    raise ValueError('Compose host binds must be read_only')
                bind = mount.get('bind') or {}
                if (not isinstance(bind, dict) or bind.get('selinux')
                        or bind.get('propagation') not in (None, '', 'private', 'rprivate')):
                    raise ValueError('Compose host binds cannot relabel or propagate host mounts')
                _host_input(mount.get('source'), app_root, [app_root, problem_root], 'Compose bind source')
            elif kind == 'volume':
                if mount.get('source') is not None and mount['source'] not in resources['volumes']:
                    raise ValueError('Compose named volumes must be declared as project-owned resources')
            elif kind != 'tmpfs' or mount.get('source') is not None:
                raise ValueError('Unsupported Compose mount type')
        for env_file in service.get('env_file', []):
            value = env_file.get('path') if isinstance(env_file, dict) else env_file
            _host_input(value, app_root, [app_root, problem_root], 'Compose environment file')


def _evidence(directory, base):
    return {path.relative_to(base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob('*') if path.is_file()}


def _recover_docker_resources(app_root, run_dir, binding):
    """A prior controller can die after Docker creates a daemon-owned container."""
    rt = _runtime()
    saved = rt.read_record(run_dir / 'docker-state.json')
    if saved is None:
        return
    if saved.get('binding') != binding:
        raise ValueError('Docker retry belongs to different source or configuration; use a new run directory')
    if saved.get('finished'):
        return
    attempt = run_dir / _relative(saved['attempt'], 'Docker attempt')
    for launch in attempt.rglob('*.launch.json'):
        exit_path = launch.with_name(launch.name[:-len('.launch.json')] + '.exit.json')
        if not exit_path.exists():
            raise ValueError('Previous Docker execution has no exit evidence; refusing duplicate execution')
        exit_record = json.loads(exit_path.read_text(encoding='utf-8'))
        running_path = launch.with_name(launch.name[:-len('.launch.json')] + '.running.json')
        failed_start = (exit_record.get('termination_reason') == 'launch_failed'
                        and exit_record.get('exit_code') is None
                        and exit_record.get('cleanup', {}).get('attempted') is False
                        and not running_path.exists())
        if exit_record.get('cleanup', {}).get('completed') is not True and not failed_start:
            raise ValueError('Previous Docker process cleanup was not confirmed')
    if saved.get('creating'):
        name = saved.get('resource_name', '')
        if not re.fullmatch(r'harness-[0-9a-f]{20}', name):
            raise ValueError('Invalid owned Docker resource name')
        compose_file = saved.get('compose_file')
        if compose_file is not None:
            compose_file = _relative(compose_file, 'Compose file')
            argv = ['docker', 'compose', '--project-name', name, '--file', compose_file,
                    'down', '--remove-orphans']
        else:
            argv = ['docker', 'rm', '--force', name]
        cleanup = _cleanup(argv, app_root, attempt)
        if not cleanup['completed']:
            raise ValueError('Previous owned Docker resources could not be cleaned up')
    saved['finished'] = True
    rt.write_record(run_dir / 'docker-state.json', saved)


def run_docker(app_root, problem_root, docker_config, run_dir, guard=None):
    config, rt = validate_docker(docker_config), _runtime()
    app_root, run_dir = Path(app_root), Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    before = rt.snapshot(app_root)
    protected = _protected_snapshot(app_root, run_dir)
    binding = hashlib.sha256(json.dumps(dict(config=config, source=before,
        problem_root=str(Path(problem_root).resolve())), sort_keys=True).encode('utf-8')).hexdigest()

    def unchanged():
        if _protected_snapshot(app_root, run_dir) != protected:
            raise ValueError('Docker verification changed protected project source, controller state, or prior evidence')
        if guard:
            guard()

    unchanged()
    previous_report = rt.read_record(run_dir / 'report.json')
    if previous_report and previous_report.get('passed'):
        if previous_report.get('binding') != binding:
            raise ValueError('Completed Docker verification belongs to different source or configuration')
        for relative, checksum in previous_report.get('evidence', {}).items():
            path = _within(run_dir, relative)
            if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise ValueError('Docker verification evidence changed: ' + relative)
        if not previous_report.get('evidence'):
            raise ValueError('Docker verification evidence is missing')
        return previous_report
    _recover_docker_resources(app_root, run_dir, binding)
    # Each invocation owns a fresh resource name. Docker creation/cleanup are
    # rerun as a unit after interruption, never inferred from old command exits.
    attempt = run_dir / ('docker-' + uuid.uuid4().hex)
    attempt.mkdir()
    name = 'harness-' + uuid.uuid4().hex[:20]
    pre = preflight(app_root, problem_root, attempt / 'preflight')
    stages, cleanup, created = [], None, False
    report = dict(passed=False, status='BLOCKED' if not pre['passed'] else 'FAIL',
        preflight=pre, stages=stages, cleanup=None, resource_name=name,
        source_before=before, source_after=before, binding=binding, run_dir=str(run_dir.resolve()))
    if not pre['passed']:
        rt.write_record(run_dir / 'report.json', report)
        return report
    compose = config['compose_file'] is not None
    command_base = ['docker', 'compose', '--project-name', name, '--file', config['compose_file']] if compose else None
    ownership = dict(binding=binding, attempt=attempt.relative_to(run_dir).as_posix(), resource_name=name,
                     compose_file=config['compose_file'], creating=False, finished=False)
    rt.write_record(run_dir / 'docker-state.json', ownership)

    def command(stage, argv, timeout=300):
        unchanged()
        result = rt.run_command(argv, app_root, attempt / stage, problem_root, timeout, guard=unchanged)
        unchanged()
        stages.append(dict(stage=stage, passed=_successful(result), outcome=result))
        if not _successful(result):
            raise ValueError('Docker stage failed: ' + stage)
        return result

    try:
        if compose:
            _within(app_root, config['compose_file'])
            rendered = command('config', command_base + ['config', '--format', 'json'], 30)
            description = json.loads(_log(rendered, 'stdout'))
            _validate_compose(description, app_root, problem_root, name, config['service'])
            command('build', command_base + ['build'], 540)
            ownership['creating'] = True
            rt.write_record(run_dir / 'docker-state.json', ownership)
            created = True
            command('start', command_base + ['up', '--detach', '--no-build'], 120)
            prefix = command_base + ['exec', '-T', config['service']]
        else:
            context = _within(app_root, config['context'], directory=True)
            dockerfile = _within(app_root, config['dockerfile'])
            if not re.search(r'^\s*FROM\s+\S+', dockerfile.read_text(encoding='utf-8-sig'), re.MULTILINE | re.IGNORECASE):
                raise ValueError('Dockerfile has no FROM instruction')
            if context == app_root:
                _require_harness_ignore(app_root, dockerfile)
            stages.append(dict(stage='config', passed=True, method='local-path-and-Dockerfile-precheck'))
            command('build', ['docker', 'build', '--file', config['dockerfile'], '--tag', name, config['context']], 540)
            args = ['docker', 'run', '--detach', '--name', name]
            if config['host_port']:
                args += ['--publish', '127.0.0.1:{}:{}'.format(config['host_port'], config['container_port'])]
            ownership['creating'] = True
            rt.write_record(run_dir / 'docker-state.json', ownership)
            created = True
            command('start', args + [name] + config['command'], 120)
            prefix = ['docker', 'exec', name]
        translated = [dict(check, argv=prefix + check['argv']) for check in config['checks']]
        verification = run_checks(app_root, problem_root, translated, attempt / 'functional', guard=unchanged)
        stages.append(dict(stage='verify', passed=verification['passed'], report=verification))
        report['passed'] = verification['passed']
        report['status'] = verification['status']
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        report['reason'] = str(exc)
        report['status'] = 'BLOCKED' if 'deadline' in str(exc).lower() or isinstance(exc, FileNotFoundError) else 'FAIL'
    finally:
        if created:
            cleanup_argv = command_base + ['down', '--remove-orphans'] if compose else ['docker', 'rm', '--force', name]
            cleanup = _cleanup(cleanup_argv, app_root, attempt)
            report['cleanup'] = cleanup
            if not cleanup['completed']:
                report.update(passed=False, status='BLOCKED', reason='Owned Docker resource cleanup was not confirmed')
        report['source_after'] = rt.snapshot(app_root)
        report['protected_changed'] = _protected_snapshot(app_root, run_dir) != protected
        if report['source_after'] != before or report['protected_changed']:
            report.update(passed=False, status='BLOCKED', reason='Docker verification changed protected source or controller evidence')
        ownership['finished'] = not created or (cleanup is not None and cleanup['completed'])
        rt.write_record(run_dir / 'docker-state.json', ownership)
        report['evidence'] = _evidence(attempt, run_dir)
        rt.write_record(run_dir / 'report.json', report)
    return report
