"""Approved, bounded data/algorithm/browser checks sharing the original deadline."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import time

BASE = Path(__file__).resolve().parent


def helper(name):
    spec = importlib.util.spec_from_file_location('assurance_' + name, BASE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = helper('development_runtime')
clock = helper('problem_time')
data = helper('assurance_data')
quality = helper('assurance_quality')
browser = helper('assurance_browser')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def schema(c):
    probe = c.obj(argv=c.STRINGS, cwd=c.STRING, timeout_seconds=c.INTEGER)
    reconciliation = c.obj(after_task=c.STRING, contract=data.RECONCILIATION_SCHEMA, probe=probe)
    algorithm = c.obj(after_task=c.STRING, contract=quality.ALGORITHM_SCHEMA,
                      measurements=c.STRING, probe=probe)
    return c.obj(project_type={'type': 'string', 'enum': ['web', 'non_web']}, reason=c.STRING,
                 environment=quality.ENVIRONMENT_SCHEMA,
                 joins=c.array(data.JOIN_SCHEMA), join_reason=c.STRING,
                 reconciliations=c.array(reconciliation), reconciliation_reason=c.STRING,
                 algorithms=c.array(algorithm), algorithm_reason=c.STRING,
                 browser=c.nullable(c.obj(after_task=c.STRING, config=browser.BROWSER_SCHEMA)))


def defaults():
    """Explicit legacy/test contract; new model outputs must supply their own reasons."""
    return {'project_type': 'non_web', 'reason': '웹 화면이 없는 과제',
            'environment': {'executables': [], 'python_packages': [], 'ports': []},
            'joins': [], 'join_reason': '연결할 입력 파일이 없음',
            'reconciliations': [], 'reconciliation_reason': '행 분류 처리가 없음',
            'algorithms': [], 'algorithm_reason': '최적화 비교가 필요 없는 기능', 'browser': None}


def validate(config, plan):
    task_ids = {task['id'] for task in plan['tasks']}
    for key in ('reason', 'join_reason', 'reconciliation_reason', 'algorithm_reason'):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError('검사 적용/제외 이유가 필요합니다: ' + key)
    if config['project_type'] not in ('web', 'non_web'):
        raise ValueError('웹/비웹 과제 분류가 필요합니다.')
    if (config['project_type'] == 'web') != (config['browser'] is not None):
        raise ValueError('웹 과제에는 브라우저 검사가 필수이며 비웹 과제에서는 실행할 수 없습니다.')
    data.validate_joins(config['joins'])
    ids = set()
    for kind in ('reconciliations', 'algorithms'):
        for item in config[kind]:
            if item['after_task'] not in task_ids:
                raise ValueError('검사 담당 작업이 없습니다: ' + item['after_task'])
            identifier = item['contract']['id']
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', identifier) or identifier in ids:
                raise ValueError('검사 ID는 고유한 영문/숫자 식별자여야 합니다.')
            ids.add(identifier)
            validate_probe(item['probe'])
            if kind == 'reconciliations':
                data.validate_reconciliations([item['contract']])
            else:
                quality.validate_algorithm(item['contract'])
                relative(item['measurements'])
    if config['browser']:
        if config['browser']['after_task'] not in task_ids:
            raise ValueError('브라우저 수정 담당 작업이 없습니다.')
        browser.validate_browser(config['browser']['config'])
    return config


def relative(name, dot=False):
    if dot and name == '.':
        return name
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
        raise ValueError('Invalid relative assurance path')
    if Path(name).is_absolute() or any(part in ('', '.', '..') for part in name.split('/')):
        raise ValueError('Assurance path escapes project')
    if name.split('/')[0] in ('.harness', '.git'):
        raise ValueError('Assurance input must not be controller state')
    return name


def validate_probe(probe):
    if (not isinstance(probe['argv'], list) or not probe['argv']
            or any(not isinstance(s, str) or not s.strip() for s in probe['argv'])):
        raise ValueError('측정 명령은 비어 있지 않은 인수 배열이어야 합니다.')
    relative(probe['cwd'], dot=True)
    if type(probe['timeout_seconds']) is not int or not 0 < probe['timeout_seconds'] <= 300:
        raise ValueError('측정 실행은 최대 300초이며 문제 접수 시 기록한 전체 마감 안에서 수행합니다.')
    if not any('{artifacts}' in argument for argument in probe['argv']):
        raise ValueError('측정 명령에 {artifacts} 출력 경로 인수가 필요합니다.')


def environment(app_root, request=None):
    report = quality.inspect_environment(request)
    path = Path(app_root) / '.harness' / 'environment.json'
    runtime.write_record(path, report)
    # Full installed-package inventory stays in a file; the model gets a bounded view.
    summary = dict(report['summary'], passed=report['passed'], python=report['python'], report_path=str(path))
    return summary


def _protected(app, directory):
    return runtime.snapshot(app, exclude=(directory.relative_to(app).as_posix(), '.harness/work/operation.lock'))


def _files(directory):
    return {name: item['sha256'] for name, item in runtime.snapshot(directory, exclude=()).items()}


def run_probe(app_root, problem_root, kind, item, run_dir, guard=None):
    app, problem, base = Path(app_root), Path(problem_root), Path(run_dir)
    clock.budget(problem, 1, required=True)
    base.mkdir(parents=True, exist_ok=True)
    attempt = runtime.read_record(base / 'active.json') or {'number': 1}
    directory = base / ('attempt-' + str(attempt['number']))
    saved_report = runtime.read_record(directory / 'report.json')
    if saved_report and saved_report['status'] == 'BLOCKED':
        verify(saved_report)
        outcome = saved_report['outcome']
        if not (outcome.get('cleanup', {}).get('completed') or
                (outcome.get('termination_reason') == 'launch_failed' and not (directory / 'producer.running.json').exists())):
            raise ValueError('이전 측정 프로세스의 종료·정리를 확인하지 못했습니다.')
        attempt = {'number': attempt['number'] + 1}
        directory = base / ('attempt-' + str(attempt['number']))
    runtime.write_record(base / 'active.json', attempt)
    directory.mkdir(parents=True, exist_ok=True)
    artifacts = directory / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    source = runtime.snapshot(app)
    inputs = runtime.snapshot(problem, exclude=('.harness/work/operation.lock',))
    binding = {'kind': kind, 'item': item, 'source': source, 'inputs': inputs}
    previous = runtime.read_record(directory / 'request.json')
    if previous is not None and previous != binding:
        raise ValueError('검사 계약/입력이 변경되었습니다. 새 실행 기록이 필요합니다.')
    if previous is None:
        runtime.write_record(directory / 'request.json', binding)
    old = runtime.read_record(directory / 'report.json')
    if old is not None:
        verify(old)
        return old
    protected = _protected(app, directory)

    def unchanged():
        clock.budget(problem, 1, required=True)
        if (_protected(app, directory) != protected
                or runtime.snapshot(problem, exclude=('.harness/work/operation.lock',)) != inputs):
            raise ValueError('검사 중 소스 또는 제어 기록이 변경되었습니다.')
        if guard:
            guard()

    probe = item['probe']
    argv = [word.replace('{artifacts}', str(artifacts)).replace('{problem}', str(problem)).replace('{app}', str(app))
            for word in probe['argv']]
    relative(probe['cwd'], dot=True)
    cwd = runtime._ordinary(app / probe['cwd'], directory=True)
    started = time.monotonic()
    try:
        outcome = runtime.run_command(argv, cwd, directory / 'producer', problem,
                                      probe['timeout_seconds'], guard=unchanged)
    except (ValueError, OSError, RuntimeError):
        unchanged()  # A mutation or expired deadline remains a hard stop.
        if not (directory / 'producer.exit.json').is_file():
            raise
        outcome = runtime.verify_command(directory / 'producer')
    unchanged()
    if outcome['exit_code'] != 0 or not outcome.get('cleanup', {}).get('completed'):
        details = {'passed': False, 'status': 'BLOCKED' if outcome.get('termination_reason') == 'launch_failed' else 'FAIL',
                   'reason': '측정/처리대장 생성 명령 실패'}
    else:
        try:
            details = (data.reconcile(problem, artifacts, [item['contract']]) if kind == 'reconciliation' else
                       quality.compare_algorithms(problem, artifacts / item['measurements'], item['contract']))
            if kind == 'reconciliation' and details['status'] == 'BLOCKED':
                for check in details.get('checks', []):
                    if check['status'] == 'BLOCKED' and check.get('stage') == 'artifact':
                        check['status'] = 'FAIL'
                        details['counts']['blocked'] -= 1
                        details['counts']['failed'] += 1
                if not any(check['status'] == 'BLOCKED' for check in details.get('checks', [])):
                    details['status'] = 'FAIL'
        except (ValueError, OSError) as exc:
            details = {'passed': False, 'status': 'FAIL', 'reason': str(exc)}
    unchanged()
    report = {'kind': kind, 'id': item['contract']['id'], 'task_id': item['after_task'],
              'passed': details['passed'], 'status': details['status'], 'details': details,
              'run_dir': str(directory), 'request_sha256': digest(binding), 'outcome': outcome,
              'artifacts': _files(artifacts), 'seconds': time.monotonic() - started}
    runtime.write_record(directory / 'report.json', report)
    return report


def run(app_root, problem_root, plan, run_dir, completed_tasks, final=False, guard=None):
    clock.budget(problem_root, 1, required=True)
    config = plan.get('assurance')
    if config is None:
        return {'passed': True, 'status': 'LEGACY', 'reports': [], 'note': '이전 승인 계획: 추가 검사 계약 없음'}
    reports = []
    for kind, key in (('reconciliation', 'reconciliations'), ('algorithm', 'algorithms')):
        for item in config[key]:
            if item['after_task'] not in completed_tasks:
                continue
            report = run_probe(app_root, problem_root, kind, item,
                               Path(run_dir) / item['contract']['id'], guard)
            reports.append(report)
            if not report['passed']:
                return {'passed': False, 'status': report['status'], 'reports': reports, 'task_id': item['after_task']}
    # A non-web plan never discovers, imports, installs or launches a browser.
    if final and config['project_type'] == 'web':
        base = Path(run_dir) / 'browser'
        attempt = runtime.read_record(base / 'active.json') or {'number': 1}
        directory = base / ('attempt-' + str(attempt['number']))
        previous = runtime.read_record(directory / 'report.json')
        if previous and previous['status'] == 'BLOCKED':
            browser.verify_browser(previous)
            attempt = {'number': attempt['number'] + 1}
            directory = base / ('attempt-' + str(attempt['number']))
        runtime.write_record(base / 'active.json', attempt)
        report = browser.run_browser(app_root, problem_root, config['browser']['config'],
                                     directory, guard=guard)
        reports.append({'kind': 'browser', 'task_id': config['browser']['after_task'], 'details': report,
                        'passed': report['passed'], 'status': report['status']})
        if not report['passed']:
            return {'passed': False, 'status': report['status'], 'reports': reports,
                    'task_id': config['browser']['after_task']}
    return {'passed': True, 'status': 'PASS' if reports else 'NOT_APPLICABLE', 'reports': reports}


def verify(report):
    if report.get('kind') == 'browser':
        return browser.verify_browser(report['details'])
    directory = Path(report['run_dir'])
    saved = runtime.read_record(directory / 'report.json')
    request = runtime.read_record(directory / 'request.json')
    if saved != report or request is None or digest(request) != report['request_sha256']:
        raise ValueError('추가 검사 기록 또는 계약이 변경되었습니다.')
    outcome = runtime.verify_command(directory / 'producer')
    for key in ('exit_code', 'termination_reason', 'cleanup'):
        if outcome[key] != report['outcome'][key]:
            raise ValueError('측정 명령 종료 증거가 다릅니다.')
    if _files(directory / 'artifacts') != report['artifacts']:
        raise ValueError('처리대장/알고리즘 측정 파일이 손상되었습니다.')
    return True
