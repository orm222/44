"""Read-only environment facts and approved, fixture-bound algorithm comparisons.

No package installation, subprocess execution or remote network access occurs.
The controller executes benchmark probes under the original deadline; this module
validates their measurements instead of trusting a probe's own pass/fail claim.
"""
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import socket
import stat
import sys
from decimal import Decimal, localcontext


def _object(**properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


def _array(items):
    return dict(type='array', items=items)


STRING = {'type': 'string'}
NUMBER = {'type': 'number'}
NULLABLE_NUMBER = {'anyOf': [NUMBER, {'type': 'null'}]}
ENVIRONMENT_SCHEMA = _object(executables=_array(STRING), python_packages=_array(STRING), ports=_array({'type': 'integer'}))
ALGORITHM_SCHEMA = _object(
    id=STRING, direction={'type': 'string', 'enum': ['minimize', 'maximize']},
    baseline=STRING, selected=STRING, candidates=_array(STRING),
    cases=_array(_object(id=STRING, input=STRING, exact=NULLABLE_NUMBER, max_relative_gap=NULLABLE_NUMBER)),
    constraints=_array(_object(name=STRING, min=NULLABLE_NUMBER, max=NULLABLE_NUMBER)), max_regression=NUMBER)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _name(value, label):
    _require(isinstance(value, str) and bool(value.strip()) and value == value.strip() and '\x00' not in value,
             label + ' must be a nonempty name')
    return value


def _number(value, label):
    _require(type(value) in (int, float), label + ' must be a finite number')
    try:
        valid = math.isfinite(value)
    except OverflowError:
        valid = False
    _require(valid, label + ' must be a finite number')
    return value


def _keys(value, expected, label):
    _require(isinstance(value, dict) and set(value) == set(expected), label + ' has missing or unexpected fields')


def _names(values, label):
    _require(isinstance(values, list), label + ' must be an array')
    for value in values:
        _name(value, label)
    _require(len(values) == len(set(values)), label + ' contains duplicates')
    return values


def _relative(value):
    _name(value, 'Input path')
    path = PurePosixPath(value)
    _require(not path.is_absolute() and '\\' not in value and ':' not in value
             and all(part not in ('', '.', '..') for part in value.split('/')),
             'Input must be a problem-relative ordinary file')
    return path


def _read_file(path):
    path = Path(path).absolute()
    _require(path == path.resolve() and not path.is_symlink(), 'Evidence file is redirected: ' + str(path))
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
             and not getattr(before, 'st_file_attributes', 0) & 0x400,
             'Evidence must be an ordinary file: ' + str(path))
    data = path.read_bytes()
    after = path.lstat()
    fields = ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    _require(path == path.resolve() and all(getattr(before, key) == getattr(after, key) for key in fields),
             'Evidence changed during inspection: ' + str(path))
    return data


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, 'Duplicate measurement JSON key: ' + key)
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError('Non-finite measurement JSON value: ' + value)


def inspect_environment(request=None):
    """Collect local facts; a discovery-only report never claims requirements pass.

    Explicit requests require every named executable/package and each currently
    available loopback port. Port results are observations, not reservations.
    """
    discovery = request is None
    request = {'executables': ['python', 'node', 'npm', 'docker', 'codex'], 'python_packages': [], 'ports': []} if discovery else request
    _keys(request, ('executables', 'python_packages', 'ports'), 'Environment request')
    _names(request['executables'], 'Executables')
    packages = _names(request['python_packages'], 'Python packages')
    for package in packages:
        _require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', package) is not None,
                 'Python package must be a distribution name without a version specifier')
    ports = request['ports']
    _require(isinstance(ports, list) and all(type(port) is int and 1 <= port <= 65535 for port in ports),
             'Ports must be integer values between 1 and 65535')
    _require(len(ports) == len(set(ports)), 'Ports contains duplicates')
    installed, inventory_errors = [], []
    try:
        for distribution in importlib.metadata.distributions():
            try:
                name, version = distribution.metadata.get('Name'), distribution.version
                if name:
                    installed.append({'name': str(name), 'version': str(version)})
            except (OSError, ValueError, KeyError) as exc:
                inventory_errors.append(type(exc).__name__)
    except (OSError, ValueError) as exc:
        inventory_errors.append(type(exc).__name__)
    installed.sort(key=lambda item: (item['name'].casefold(), item['version']))
    missing, tools, selected, port_results = [], [], [], []
    search_path = str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')
    for name in request['executables']:
        path = shutil.which(name, path=search_path)
        tools.append({'name': name, 'available': path is not None, 'path': path})
        if path is None and not discovery:
            missing.append('executable:' + name)
    for name in packages:
        try:
            selected.append({'name': name, 'status': 'AVAILABLE', 'version': importlib.metadata.version(name)})
        except importlib.metadata.PackageNotFoundError:
            selected.append({'name': name, 'status': 'MISSING', 'version': None})
            missing.append('python_package:' + name)
        except (OSError, ValueError) as exc:
            selected.append({'name': name, 'status': 'UNKNOWN', 'version': None, 'error': type(exc).__name__})
            missing.append('python_package:' + name)
    for port in ports:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(('127.0.0.1', port))
            port_results.append({'port': port, 'status': 'AVAILABLE', 'available': True})
        except OSError as exc:
            port_results.append({'port': port, 'status': 'UNAVAILABLE', 'available': False,
                                 'error': type(exc).__name__, 'errno': exc.errno})
            missing.append('port:' + str(port))
    supported = sys.version_info[:2] >= (3, 9)
    if not supported:
        missing.append('python:>=3.9')
    status = 'BLOCKED' if missing else ('PARTIAL' if inventory_errors else 'DISCOVERED') if discovery else 'PASS'
    return {
        'status': status, 'passed': False if missing else None if discovery else True,
        'python': {'executable': sys.executable, 'version': platform.python_version(), 'supported': supported},
        'installed_packages': installed, 'inventory_complete': not inventory_errors, 'inventory_errors': inventory_errors,
        'executables': tools, 'python_packages': selected, 'ports': port_results, 'missing_required': missing,
        'summary': {'status': status, 'python': platform.python_version(), 'available_tools': [item['name'] for item in tools if item['available']],
                    'missing_required': missing, 'requested_packages': selected, 'ports': port_results,
                    'installed_package_count': len(installed), 'package_sample': installed[:20],
                    'additional_package_count': max(0, len(installed) - 20), 'inventory_complete': not inventory_errors},
    }


def validate_algorithm(contract):
    """Validate user-approved thresholds; never manufacture an acceptance budget."""
    _keys(contract, ('id', 'direction', 'baseline', 'selected', 'candidates', 'cases', 'constraints', 'max_regression'), 'Algorithm contract')
    _name(contract['id'], 'Contract ID')
    _require(contract['direction'] in ('minimize', 'maximize'), 'Direction must be minimize or maximize')
    candidates = _names(contract['candidates'], 'Candidates')
    _require(1 <= len(candidates) <= 3, 'Require one to three approved candidates')
    _require(contract['baseline'] in candidates and contract['selected'] in candidates, 'Baseline and selected candidate must be listed')
    _require(_number(contract['max_regression'], 'max_regression') >= 0, 'max_regression cannot be negative')
    _require(isinstance(contract['cases'], list) and bool(contract['cases']), 'At least one approved case is required')
    case_ids = set()
    for case in contract['cases']:
        _keys(case, ('id', 'input', 'exact', 'max_relative_gap'), 'Case')
        _name(case['id'], 'Case ID')
        _require(case['id'] not in case_ids, 'Duplicate case ID')
        case_ids.add(case['id'])
        _relative(case['input'])
        if case['exact'] is not None:
            _number(case['exact'], 'exact')
        if case['max_relative_gap'] is not None:
            _require(_number(case['max_relative_gap'], 'max_relative_gap') >= 0, 'max_relative_gap cannot be negative')
            _require(case['exact'] is not None, 'Relative-gap threshold needs an approved exact objective')
    _require(isinstance(contract['constraints'], list), 'Constraints must be an array')
    names = set()
    for constraint in contract['constraints']:
        _keys(constraint, ('name', 'min', 'max'), 'Constraint')
        _name(constraint['name'], 'Constraint name')
        _require(constraint['name'] not in names, 'Duplicate constraint name')
        names.add(constraint['name'])
        low, high = constraint['min'], constraint['max']
        _require(low is not None or high is not None, 'Constraint needs at least one approved bound')
        if low is not None:
            _number(low, 'Constraint minimum')
        if high is not None:
            _number(high, 'Constraint maximum')
        _require(low is None or high is None or low <= high, 'Constraint minimum exceeds maximum')
    return contract


def _display(value):
    """Retain a readable finite representation even for a huge difference/ratio."""
    number = float(value)
    return number if math.isfinite(number) else str(value)


def compare_algorithms(problem_root, measurements_file, contract):
    """Compare complete measurements, feasibility first, without running probes.

    max_regression is an absolute objective deterioration. Exact objectives are
    genuine approved optima, not rounded estimates. Relative gap is distance from
    that optimum / abs(exact). For exact=0, only equality has a finite gap (0).
    """
    validate_algorithm(contract)
    root = Path(problem_root).absolute()
    _require(root.is_dir() and root == root.resolve(), 'Problem root is missing or redirected')
    input_hashes = {}
    for case in contract['cases']:
        relative = _relative(case['input'])
        raw = _read_file(root.joinpath(*relative.parts))
        input_hashes[case['id']] = hashlib.sha256(raw).hexdigest()
    raw_measurements = _read_file(measurements_file)
    measurements = json.loads(raw_measurements.decode('utf-8-sig'), object_pairs_hook=_pairs, parse_constant=_nonfinite)
    _keys(measurements, ('results',), 'Measurements')
    _require(isinstance(measurements['results'], list), 'Measurement results must be an array')
    expected = {(case['id'], candidate) for case in contract['cases'] for candidate in contract['candidates']}
    indexed, constraint_names = {}, {value['name'] for value in contract['constraints']}
    for measurement in measurements['results']:
        _keys(measurement, ('case_id', 'candidate_id', 'input_sha256', 'objective', 'metrics'), 'Measurement row')
        _name(measurement['case_id'], 'Measurement case ID')
        _name(measurement['candidate_id'], 'Measurement candidate ID')
        key = (measurement['case_id'], measurement['candidate_id'])
        _require(key in expected and key not in indexed, 'Unexpected or duplicate case/candidate measurement')
        _require(measurement['input_sha256'] == input_hashes[key[0]], 'Measurement input hash differs from actual fixture')
        _number(measurement['objective'], 'Objective')
        _keys(measurement['metrics'], constraint_names, 'Constraint metrics')
        for value in measurement['metrics'].values():
            _number(value, 'Constraint metric')
        indexed[key] = measurement
    _require(set(indexed) == expected, 'Missing case/candidate measurements')
    summaries, failures = [], []
    sign = Decimal(1 if contract['direction'] == 'minimize' else -1)
    with localcontext() as context:
        context.prec = 50
        for case in contract['cases']:
            candidates, feasible = [], {}
            reasons = []
            for candidate in contract['candidates']:
                measurement = indexed[(case['id'], candidate)]
                violations = [c['name'] for c in contract['constraints']
                              if (c['min'] is not None and measurement['metrics'][c['name']] < c['min'])
                              or (c['max'] is not None and measurement['metrics'][c['name']] > c['max'])]
                candidates.append({'id': candidate, 'objective': measurement['objective'], 'feasible': not violations,
                                   'violations': violations})
                if not violations:
                    feasible[candidate] = Decimal(str(measurement['objective']))
                    if case['exact'] is not None and sign * (feasible[candidate] - Decimal(str(case['exact']))) < 0:
                        reasons.append(candidate + ': objective contradicts approved exact optimum')
            selected, baseline = contract['selected'], contract['baseline']
            if selected not in feasible:
                reasons.append('selected candidate violates mandatory constraints')
            if baseline not in feasible:
                reasons.append('baseline violates mandatory constraints; no valid regression comparison')
            regression = None
            if selected in feasible and baseline in feasible:
                regression = sign * (feasible[selected] - feasible[baseline])
                if regression > Decimal(str(contract['max_regression'])):
                    reasons.append('selected candidate exceeds approved absolute regression budget')
            gap, exact_status = None, 'not_measured'
            if case['exact'] is not None and selected in feasible:
                exact = Decimal(str(case['exact']))
                distance = sign * (feasible[selected] - exact)
                if distance < 0:
                    exact_status = 'contradiction'
                elif exact == 0 and distance != 0:
                    exact_status = 'relative_gap_undefined_exact_zero'
                    if case['max_relative_gap'] is not None:
                        reasons.append('nonzero distance from exact=0 has no finite relative gap')
                else:
                    gap = Decimal(0) if exact == 0 else distance / abs(exact)
                    exact_status = 'matches_exact_on_case' if gap == 0 else 'measured_gap'
                    if case['max_relative_gap'] is not None and gap > Decimal(str(case['max_relative_gap'])):
                        reasons.append('selected candidate exceeds approved relative gap')
            ranking = sorted(feasible, key=lambda value: feasible[value], reverse=contract['direction'] == 'maximize')
            summaries.append({'case_id': case['id'], 'input_sha256': input_hashes[case['id']],
                              'passed': not reasons, 'candidates': candidates, 'feasible_ranking': ranking,
                              'baseline': baseline, 'selected': selected,
                              'absolute_regression': _display(regression) if regression is not None else None,
                              'exact': case['exact'], 'exact_status': exact_status,
                              'relative_gap': _display(gap) if gap is not None else None, 'reasons': reasons})
            failures.extend({'case_id': case['id'], 'reason': reason} for reason in reasons)
    for case in contract['cases']:
        path = root.joinpath(*_relative(case['input']).parts)
        _require(hashlib.sha256(_read_file(path)).hexdigest() == input_hashes[case['id']],
                 'Benchmark fixture changed while comparing measurements')
    return {'passed': not failures, 'status': 'PASS' if not failures else 'FAIL', 'contract_id': contract['id'],
            'direction': contract['direction'], 'max_regression': contract['max_regression'],
            'regression_unit': 'absolute_objective', 'case_count': len(summaries),
            'candidate_count': len(contract['candidates']), 'measurement_count': len(indexed),
            'cases': summaries, 'failures': failures, 'optimality_claim': 'Only approved exact cases were checked; no general optimality claim.',
            'evidence': {'measurements_sha256': hashlib.sha256(raw_measurements).hexdigest(),
                         'contract_sha256': hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest(),
                         'input_sha256': input_hashes}}
