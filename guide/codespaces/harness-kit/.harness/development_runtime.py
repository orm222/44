"""Durable, bounded child-agent execution and journalled source publication.

The implementation worker returns a proposed patch from a read-only Codex process.
Only the controller publishes an approved set of paths.  Git ignores never define
the mutation boundary, and a missing process exit receipt never means success.
"""
import base64
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat


BASE = Path(__file__).resolve().parent


def _helper(name):
    loader = importlib.util.spec_from_file_location('development_runtime_' + name, BASE / (name + '.py'))
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    return module


lifecycle = _helper('lifecycle')
processes = _helper('processes')
problem_time = lifecycle.problem_time
codex_protocol = _helper('codex_protocol')


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, 'Duplicate development record field: ' + key)
        result[key] = value
    return result


def _decode(data):
    def invalid(value):
        raise ValueError('Nonfinite development JSON value: ' + value)
    return json.loads(data.decode('utf-8-sig'), object_pairs_hook=_pairs, parse_constant=invalid)


def _ordinary(path, directory=False, missing=False):
    path = Path(path).absolute()
    _require(path == path.resolve(), 'Redirected development path: ' + str(path))
    for item in (path, *path.parents):
        _require(not item.is_symlink(), 'Development symlink is not allowed: ' + str(item))
        if item.exists():
            info = item.lstat()
            _require(not getattr(info, 'st_file_attributes', 0) & 0x400,
                     'Development reparse point is not allowed: ' + str(item))
    if not path.exists():
        _require(missing, 'Missing development path: ' + str(path))
        return path
    info = path.lstat()
    if directory:
        _require(stat.S_ISDIR(info.st_mode), 'Development directory expected: ' + str(path))
    else:
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                 'Ordinary, unlinked development file expected: ' + str(path))
    return path


def _read_bytes(path, missing=False):
    path = _ordinary(path, missing=missing)
    if not path.exists():
        return None
    before = path.lstat()
    data = path.read_bytes()
    after = path.lstat()
    _require(all(getattr(before, field) == getattr(after, field) for field in
                 ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')),
             'Development file changed while reading: ' + str(path))
    _ordinary(path)
    return data


def _atomic(path, data, temporary=None, mode=None):
    path = _ordinary(path, missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ordinary(path.parent, directory=True)
    temporary = _ordinary(temporary or path.with_name(path.name + '.tmp'), missing=True)
    temporary.parent.mkdir(parents=True, exist_ok=True)
    prior = _read_bytes(temporary, missing=True)
    _require(prior is None or data.startswith(prior), 'Unrelated pending development bytes: ' + str(temporary))
    with temporary.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if mode is not None:
        os.chmod(str(temporary), mode)
    _ordinary(path, missing=True)
    os.replace(str(temporary), str(path))


def _record_bytes(value):
    _require(isinstance(value, dict) and '_integrity' not in value, 'Development record must be an object')
    return _json(dict(value, _integrity=_digest(_json(value))))


def _record_value(raw, path):
    value = _decode(raw)
    _require(isinstance(value, dict), 'Development record must be an object')
    checksum = value.pop('_integrity', None)
    _require(checksum == _digest(_json(value)), 'Development record integrity differs: ' + str(path))
    return value


def _recover_record(path, allow_unbound_target=False):
    """Finish only a publication whose complete before/target intent is durable."""
    path = _ordinary(path, missing=True)
    intent_path = path.with_name(path.name + '.intent')
    preparation = intent_path.with_name(intent_path.name + '.tmp')
    staged = _read_bytes(preparation, missing=True)
    committed = _read_bytes(intent_path, missing=True)
    if staged is not None:
        if committed is not None:
            _require(committed.startswith(staged), 'Conflicting pending record intents: ' + str(path))
            preparation.unlink()
        else:
            try:
                candidate = _record_value(staged, preparation)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                # This is preparation only: source/state publication cannot start
                # before the complete intent is atomically published. Discarding
                # a truncated preparation retains the previous committed record.
                header = b'{\n  "_integrity": "'
                _require(header.startswith(staged) or staged.startswith(header),
                         'Unrecognized record preparation: ' + str(path))
                if isinstance(exc, UnicodeDecodeError):
                    _require(exc.end == len(staged), 'Invalid UTF-8 record preparation')
                else:
                    _require(exc.pos == len(exc.doc) or exc.msg.startswith('Unterminated string'),
                             'Invalid record preparation JSON')
                current = _read_bytes(path, missing=True)
                if current is not None:
                    _record_value(current, path)
                preparation.unlink()
            else:
                _require(candidate.get('path') == str(path), 'Record preparation belongs to another file')
                os.replace(str(preparation), str(intent_path))
                committed = staged
    if committed is not None:
        intent = _record_value(committed, intent_path)
        _require(set(intent) == {'version', 'path', 'before', 'after', 'mode'}
                 and intent['version'] == 1 and intent['path'] == str(path), 'Invalid record publication intent')
        before, after = _decoded(intent['before']), _decoded(intent['after'])
        _require(after is not None, 'A record publication cannot delete its target')
        _record_value(after, path)
        current = _read_bytes(path, missing=True)
        _require(current in (before, after), 'Unexpected user change during record publication: ' + str(path))
        temporary = path.with_name(path.name + '.tmp')
        partial = _read_bytes(temporary, missing=True)
        _require(partial is None or after.startswith(partial), 'Unexpected record publication temporary bytes')
        if current != after:
            _atomic(path, after, mode=intent['mode'])
        elif partial is not None:
            temporary.unlink()
        _require(_read_bytes(path) == after, 'Record publication target changed')
        intent_path.unlink()
    elif not allow_unbound_target:
        _require(not path.with_name(path.name + '.tmp').exists(), 'Unbound pending record publication: ' + str(path))


def write_record(path, value):
    """Save a checksummed record with recoverable before/target publication intent."""
    path = _ordinary(path, missing=True)
    _recover_record(path, allow_unbound_target=True)
    before, after = _read_bytes(path, missing=True), _record_bytes(value)
    if before == after and not path.with_name(path.name + '.tmp').exists():
        return
    intent = {'version': 1, 'path': str(path), 'before': _encoded(before), 'after': _encoded(after),
              'mode': stat.S_IMODE(path.stat().st_mode) if path.exists() else (0o666 if os.name == 'nt' else 0o644)}
    _atomic(path.with_name(path.name + '.intent'), _record_bytes(intent))
    _recover_record(path)


def read_record(path):
    """Recover a prepared publication, then validate its committed record."""
    _recover_record(path)
    raw = _read_bytes(path, missing=True)
    return None if raw is None else _record_value(raw, path)


def _relative(name):
    _require(isinstance(name, str) and bool(name) and '\x00' not in name and '\\' not in name,
             'Expected a portable relative file path')
    relative = PurePosixPath(name)
    _require(not relative.is_absolute() and not PureWindowsPath(name).anchor
             and name == relative.as_posix() and all(part not in ('', '.', '..') for part in relative.parts)
             and all(':' not in part and not part.endswith((' ', '.')) for part in relative.parts),
             'Invalid development relative path: ' + name)
    return name


def project_path(root, name, missing=True):
    root = _ordinary(root, directory=True)
    name = _relative(name)
    target = root / name
    _require(target.resolve().is_relative_to(root), 'Development path escapes project: ' + name)
    return _ordinary(target, missing=missing)


def snapshot(root, exclude=('.harness',)):
    """Hash every ordinary file, including Git-ignored and dependency files."""
    root = _ordinary(root, directory=True)
    exclusions = tuple(_relative(item) for item in exclude)
    def excluded(name):
        return any(name == item or name.startswith(item + '/') for item in exclusions)
    def fail(exc):
        raise ValueError('Cannot enumerate development source: ' + str(exc)) from exc
    result = {}
    for directory, dirs, names in os.walk(str(root), followlinks=False, onerror=fail):
        folder = _ordinary(directory, directory=True)
        keep = []
        for name in sorted(dirs):
            child = _ordinary(folder / name, directory=True)
            if not excluded(child.relative_to(root).as_posix()):
                keep.append(name)
        dirs[:] = keep
        for name in sorted(names):
            path = _ordinary(folder / name)
            relative = path.relative_to(root).as_posix()
            if excluded(relative):
                continue
            data = _read_bytes(path)
            result[relative] = {'sha256': _digest(data), 'mode': stat.S_IMODE(path.stat().st_mode), 'size': len(data)}
    return result


def _encoded(data):
    return None if data is None else {'sha256': _digest(data), 'base64': base64.b64encode(data).decode('ascii')}


def _decoded(value):
    if value is None:
        return None
    _require(isinstance(value, dict) and set(value) == {'sha256', 'base64'}, 'Invalid patch byte record')
    data = base64.b64decode(value['base64'], validate=True)
    _require(value['sha256'] == _digest(data), 'Patch bytes differ from their hash')
    return data


def _apply_entry(root, entry, journal_dir, index):
    path = project_path(root, entry['path'])
    before, after = _decoded(entry['before']), _decoded(entry['after'])
    current = _read_bytes(path, missing=True)
    _require(current in (before, after), 'Unexpected user change during patch publication: ' + entry['path'])
    if current == after:
        if after is not None:
            _require(stat.S_IMODE(path.stat().st_mode) == entry['mode'], 'Patch target mode differs: ' + entry['path'])
        return
    if after is None:
        path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic(path, after, journal_dir / ('source-' + str(index) + '.tmp'), mode=entry['mode'])


def apply_files(app_root, files, allowed, journal_dir, expected_before=None):
    """Publish an exact-path patch transaction or finish its durable journal.

    Every before/after byte sequence is stored before the first source mutation.
    Recovery accepts only those two states; unrelated user changes are refused.
    """
    root = _ordinary(app_root, directory=True)
    journal_dir = _ordinary(journal_dir, directory=True, missing=True)
    _require(journal_dir.resolve().is_relative_to(root / '.harness'),
             'Patch journal must be in the application .harness directory')
    _require(isinstance(files, list) and isinstance(allowed, (list, tuple, set)), 'Invalid patch or allowed paths')
    allowed = sorted({_relative(path) for path in allowed})
    _require(not any(path.split('/')[0] in ('.harness', '.git') for path in allowed),
             'Runtime and Git metadata cannot be patch targets')
    names = set()
    normalized = []
    for item in files:
        _require(isinstance(item, dict) and set(item) == {'path', 'content'}, 'Invalid patch entry')
        name = _relative(item['path'])
        _require(name in allowed and name not in names, 'Patch path is unapproved or repeated: ' + name)
        _require(item['content'] is None or isinstance(item['content'], str), 'Patch content must be text or null')
        names.add(name)
        normalized.append({'path': name, 'content': item['content']})
    normalized.sort(key=lambda item: item['path'])
    request = {'project': str(root), 'allowed': allowed, 'files': normalized}
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_path = journal_dir / 'patch.json'
    saved = read_record(journal_path)
    if saved is None:
        before = snapshot(root)
        _require(expected_before is None or before == expected_before,
                 'Source differs from the controller baseline before patch publication')
        entries = []
        for item in normalized:
            path = project_path(root, item['path'])
            raw = _read_bytes(path, missing=True)
            entries.append({'path': item['path'], 'before': _encoded(raw),
                            'after': _encoded(None if item['content'] is None else item['content'].encode('utf-8')),
                            'mode': before.get(item['path'], {}).get('mode', 0o644 if os.name != 'nt' else 0o666)})
        saved = {'version': 1, 'status': 'prepared', 'request': request, 'before': before, 'entries': entries}
        write_record(journal_path, saved)
    _require(saved.get('version') == 1 and saved.get('request') == request
             and saved.get('status') in ('prepared', 'completed'), 'Patch journal belongs to another publication')
    _require(expected_before is None or saved.get('before') == expected_before,
             'Patch journal differs from the controller baseline')
    current = snapshot(root)
    _require({key: value for key, value in current.items() if key not in names}
             == {key: value for key, value in saved['before'].items() if key not in names},
             'Unexpected source change outside the patch targets')
    _require([entry.get('path') for entry in saved['entries']] == [item['path'] for item in normalized],
             'Patch journal paths differ')
    for item, entry in zip(normalized, saved['entries']):
        expected = None if item['content'] is None else item['content'].encode('utf-8')
        _require(_decoded(entry['after']) == expected, 'Patch journal target differs')
        path = project_path(root, entry['path'])
        _require(_read_bytes(path, missing=True) in (_decoded(entry['before']), expected),
                 'Unexpected user change during patch publication: ' + entry['path'])
        if path.exists():
            _require(stat.S_IMODE(path.stat().st_mode) == entry['mode'],
                     'Unexpected user mode change during patch publication: ' + entry['path'])
    for index, entry in enumerate(saved['entries']):
        _apply_entry(root, entry, journal_dir, index)
    after = snapshot(root)
    _require({key: value for key, value in after.items() if key not in names}
             == {key: value for key, value in saved['before'].items() if key not in names},
             'Unexpected source change outside the patch targets during publication')
    for entry in saved['entries']:
        target = _decoded(entry['after'])
        expected = None if target is None else {'sha256': _digest(target), 'mode': entry['mode'], 'size': len(target)}
        _require(after.get(entry['path']) == expected, 'Patch target changed during publication: ' + entry['path'])
    result = dict(saved, status='completed', after=after, changed_paths=[entry['path'] for entry in saved['entries']
                  if entry['before'] != entry['after']])
    write_record(journal_path, result)
    return result


def _command_files(prefix):
    names = ('.command.json', '.input.txt', '.launch.json', '.running.json', '.exit.json', '.stdout.log', '.stderr.log')
    result = {}
    for suffix in names:
        data = _read_bytes(prefix.with_suffix(suffix), missing=True)
        result[suffix] = None if data is None else {'sha256': _digest(data), 'size': len(data)}
    return result


def _command_outcome(prefix, request, recovered):
    exit_path = prefix.with_suffix('.exit.json')
    raw = _read_bytes(exit_path, missing=True)
    _require(raw is not None, 'Execution has no exit receipt; preserve evidence and do not relaunch')
    outcome = _decode(raw)
    _require(isinstance(outcome, dict) and isinstance(outcome.get('cleanup'), dict), 'Malformed command exit receipt')
    launch = _read_bytes(prefix.with_suffix('.launch.json'), missing=True)
    if launch is not None:
        _require(_decode(launch).get('started_at') == outcome.get('started_at'), 'Execution launch/exit identity differs')
        failed_start = (outcome.get('termination_reason') == 'launch_failed' and outcome.get('exit_code') is None
                        and outcome['cleanup'].get('attempted') is False
                        and not prefix.with_suffix('.running.json').exists())
        _require(failed_start or (outcome['cleanup'].get('completed') is True and outcome.get('exit_code') is not None),
                 'Execution cleanup is not proven; preserve evidence and do not relaunch')
    else:
        _require(outcome.get('termination_reason') == 'launch_failed' and outcome.get('exit_code') is None,
                 'Execution result has no matching launch receipt')
    command = _read_bytes(prefix.with_suffix('.command.json'), missing=True)
    if outcome.get('termination_reason') == 'completed':
        _require(type(outcome.get('exit_code')) is int and launch is not None and command is not None
                 and all(_read_bytes(prefix.with_suffix(suffix), missing=True) is not None for suffix in
                         ('.input.txt', '.running.json', '.stdout.log', '.stderr.log')),
                 'Completed execution evidence is incomplete')
    if command is not None:
        recorded = _decode(command)
        _require(recorded.get('argv') == request['argv'] and recorded.get('cwd') == request['cwd']
                 and recorded.get('timeout') == request['timeout'], 'Command evidence differs from its request')
    _require(_read_bytes(prefix.with_suffix('.input.txt'), missing=True) in
             (None, request['prompt'].replace('\n', os.linesep).encode('utf-8')), 'Command stdin evidence differs')
    evidence = _command_files(prefix)
    seal_path = prefix.with_suffix('.receipt.json')
    seal = read_record(seal_path)
    expected = {'request_sha256': _digest(_json(request)), 'evidence': evidence}
    if seal is None:
        write_record(seal_path, expected)
    else:
        _require(seal == expected, 'Command evidence hashes differ')
    return dict(outcome, stdout_path=str(prefix.with_suffix('.stdout.log')),
                stderr_path=str(prefix.with_suffix('.stderr.log')), recovered=recovered, prefix=str(prefix))


def verify_command(prefix):
    """Inspect a historical command without executing or repairing its evidence."""
    prefix = _ordinary(prefix, missing=True)
    request = read_record(prefix.with_suffix('.request.json'))
    receipt = read_record(prefix.with_suffix('.receipt.json'))
    _require(isinstance(request, dict) and isinstance(receipt, dict), 'Missing historical command evidence')
    _require(request.get('problem_receipt') == problem_time.load(request['problem_root'], required=True),
             'Historical command problem receipt differs')
    return _command_outcome(prefix, request, recovered=True)


def run_command(argv, cwd, prefix, problem_root, timeout, guard=None, prompt=None):
    """Run once with durable evidence and the original problem deadline."""
    _require(isinstance(argv, list) and argv and all(isinstance(value, str) and '\x00' not in value for value in argv),
             'Commands must be argument arrays')
    _require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0, 'Invalid command timeout')
    cwd, problem_root = _ordinary(cwd, directory=True), _ordinary(problem_root, directory=True)
    prefix = _ordinary(prefix, missing=True)
    _require(not prefix.suffix, 'Execution prefix must not have a filename extension')
    prefix.parent.mkdir(parents=True, exist_ok=True)
    receipt = problem_time.load(problem_root, required=True)
    request = {'version': 1, 'argv': argv, 'cwd': str(cwd), 'problem_root': str(problem_root),
               'problem_receipt': receipt, 'timeout': timeout, 'prompt': prompt or ''}
    path = prefix.with_suffix('.request.json')
    previous = read_record(path)
    if previous is None:
        _require(not any(prefix.with_suffix(suffix).exists() for suffix in
                         ('.exit.json', '.launch.json', '.running.json', '.receipt.json')),
                 'Unbound command execution evidence already exists')
        write_record(path, request)
    else:
        _require(previous == request, 'Execution request differs; use a new attempt after reviewing prior evidence')
    if prefix.with_suffix('.exit.json').exists():
        outcome = _command_outcome(prefix, request, recovered=True)
        if guard:
            guard()
        return outcome
    _require(not prefix.with_suffix('.launch.json').exists() and not prefix.with_suffix('.running.json').exists(),
             'Execution started without an exit receipt; preserve evidence and do not relaunch')
    def deadline():
        _require(problem_time.load(problem_root, required=True) == receipt, 'Original problem receipt changed')
        problem_time.budget(problem_root, 1, required=True)
    deadline()
    if guard:
        guard()
    # Only pre-launch files are disposable; a launch marker above forbids this path.
    cache = prefix.with_suffix('.pycache')
    if cache.exists():
        _ordinary(cache, directory=True)
        cache.rmdir()  # A nonempty unexplained cache is retained and blocks recovery.
    for suffix in ('.command.json', '.input.txt', '.stdout.log', '.stderr.log'):
        candidate = _ordinary(prefix.with_suffix(suffix), missing=True)
        if candidate.exists():
            candidate.unlink()
    try:
        lifecycle.execute(argv, cwd, prefix, timeout,
                          prompt=prompt, guard=guard, start_process=processes.start_process,
                          stop_process=processes.stop_process, deadline_guard=deadline)
    except BaseException:
        # The lifecycle records even a launch failure. Do not hide the original error.
        if prefix.with_suffix('.exit.json').exists():
            try:
                _command_outcome(prefix, request, recovered=False)
            except (OSError, ValueError):
                pass
        raise
    return _command_outcome(prefix, request, recovered=False)


def validate_schema(value, schema, path='$'):
    """Validate the strict JSON subset used by the bundled agent contracts."""
    _require(isinstance(schema, dict), 'Agent schema must be an object')
    if 'anyOf' in schema:
        errors = []
        for option in schema['anyOf']:
            try:
                validate_schema(value, option, path)
                return
            except ValueError as exc:
                errors.append(str(exc))
        raise ValueError('Agent result does not match any allowed type: ' + path)
    kind = schema.get('type')
    types = kind if isinstance(kind, list) else [kind]
    valid = {'object': isinstance(value, dict), 'array': isinstance(value, list),
             'string': isinstance(value, str), 'integer': type(value) is int,
             'number': type(value) is int or (type(value) is float and math.isfinite(value)),
             'boolean': type(value) is bool, 'null': value is None}
    _require(kind is None or any(valid.get(item, False) for item in types), 'Agent result type differs: ' + path)
    if 'enum' in schema:
        _require(value in schema['enum'], 'Agent result is outside enum: ' + path)
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        _require(set(schema.get('required', [])) <= set(value), 'Agent result required field is missing: ' + path)
        _require(schema.get('additionalProperties') is not False or set(value) <= set(properties),
                 'Agent result contains unknown fields: ' + path)
        for key, child in value.items():
            if key in properties:
                validate_schema(child, properties[key], path + '.' + key)
    elif isinstance(value, list):
        _require(len(value) >= schema.get('minItems', 0) and len(value) <= schema.get('maxItems', len(value)),
                 'Agent result array size differs: ' + path)
        for index, child in enumerate(value):
            validate_schema(child, schema.get('items', {}), path + '[' + str(index) + ']')
    elif isinstance(value, str):
        _require(len(value) >= schema.get('minLength', 0) and len(value) <= schema.get('maxLength', len(value)),
                 'Agent result string length differs: ' + path)


def read_usage(path):
    """Read reported usage, preserving missing fields instead of guessing zero."""
    totals = {'input_tokens': None, 'cached_input_tokens': None, 'output_tokens': None, 'reported': False}
    raw = _read_bytes(path)
    for line in raw.decode('utf-8', errors='replace').splitlines():
        try:
            event = _decode(line.encode('utf-8'))
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict) or event.get('type') != 'turn.completed':
            continue
        usage = event.get('usage')
        if not isinstance(usage, dict):
            continue
        valid = {name: usage.get(name) for name in totals if name != 'reported'}
        if any(type(value) is int and value >= 0 for value in valid.values()):
            totals['reported'] = True
        for name, value in valid.items():
            if type(value) is int and value >= 0:
                totals[name] = (totals[name] or 0) + value
    return totals


def run_agent(role, prompt, schema, app_root, problem_root, run_dir, config, timeout=600):
    """Execute a fresh role-specific child Codex process or recover its result."""
    _require(role in ('coordinator', 'implementer', 'reviewer'), 'Unknown development agent role')
    _require(isinstance(prompt, str) and prompt.strip(), 'Agent prompt is empty')
    _require(isinstance(config, dict), 'Agent configuration must be an object')
    model = config.get(role + '_model') or config.get('model') or config.get('builder_model')
    _require(isinstance(model, str) and model.strip() and model != 'SET_ACTUAL_MODEL_ID', 'Configure an actual model ID')
    root, problem = _ordinary(app_root, directory=True), _ordinary(problem_root, directory=True)
    run_dir = _ordinary(run_dir, directory=True, missing=True)
    _require(run_dir.resolve().is_relative_to(root / '.harness'), 'Agent evidence must be inside application .harness')
    run_dir.mkdir(parents=True, exist_ok=True)
    excluded = (run_dir.relative_to(root).as_posix(), '.harness/work/operation.lock')
    source = snapshot(root, exclude=excluded)
    inputs = snapshot(problem, exclude=('.harness/work/operation.lock',))
    argv = codex_protocol.resolve_command(config.get('codex_command', ['codex'])) + [
        '--ask-for-approval', 'never', 'exec', '--model', model, '--sandbox', 'read-only', '--skip-git-repo-check', '--json',
        '--output-schema', str(run_dir / 'schema.json'), '--output-last-message', str(run_dir / 'answer.json'), '-']
    request = {'version': 1, 'role': role, 'model': model, 'prompt': prompt, 'schema': schema,
               'app_root': str(root), 'problem_root': str(problem), 'sources': source, 'inputs': inputs,
               'argv': argv, 'timeout': timeout}
    previous = read_record(run_dir / 'agent-request.json')
    if previous is None:
        _require(not any((run_dir / name).exists() for name in ('answer.json', 'agent-result.json', 'model.request.json')),
                 'Unbound agent evidence already exists')
        write_record(run_dir / 'agent-request.json', request)
    else:
        _require(previous == request, 'Agent inputs changed; saved result belongs to an earlier source revision')
    for name, data in (('schema.json', _json(schema)), ('prompt.txt', prompt.encode('utf-8'))):
        existing = _read_bytes(run_dir / name, missing=True)
        _require(existing in (None, data), 'Agent prompt/schema evidence changed: ' + name)
        if existing is None:
            _atomic(run_dir / name, data)
    def guard():
        _require(snapshot(root, exclude=excluded) == source, 'Read-only subagent changed application files')
        _require(snapshot(problem, exclude=('.harness/work/operation.lock',)) == inputs,
                 'Problem inputs changed during development subagent execution')
    outcome = run_command(argv, root, run_dir / 'model', problem, timeout, guard=guard, prompt=prompt)
    _require(outcome.get('exit_code') == 0 and outcome.get('termination_reason') == 'completed'
             and outcome['cleanup'].get('completed') is True, 'Subagent has no successful completed execution and cleanup')
    answer = _read_bytes(run_dir / 'answer.json')
    result = _decode(answer)
    validate_schema(result, schema)
    usage = read_usage(run_dir / 'model.stdout.log')
    value = {'request_sha256': _digest(_json(request)), 'answer_sha256': _digest(answer),
             'command_receipt_sha256': _digest(_read_bytes(run_dir / 'model.receipt.json')),
             'result': result, 'usage': usage, 'run_id': run_dir.name}
    previous_result = read_record(run_dir / 'agent-result.json')
    if previous_result is None:
        write_record(run_dir / 'agent-result.json', value)
    else:
        _require(previous_result == value, 'Saved subagent result evidence differs')
    return {'result': result, 'usage': usage, 'run_id': run_dir.name}


def verify_agent_result(run_dir):
    """Verify historical model evidence without requiring today's source revision.

    Later approved patches legitimately change the project. The original immutable
    invocation, completed execution, output and token receipts must still agree.
    This function never launches a process or recreates missing evidence.
    """
    directory = _ordinary(run_dir, directory=True)
    request = read_record(directory / 'agent-request.json')
    result = read_record(directory / 'agent-result.json')
    command = read_record(directory / 'model.request.json')
    seal = read_record(directory / 'model.receipt.json')
    _require(all(isinstance(value, dict) for value in (request, result, command, seal)),
             'Missing historical subagent execution evidence')
    _require(request.get('role') in ('coordinator', 'implementer', 'reviewer')
             and directory.resolve().is_relative_to(Path(request['app_root']) / '.harness'),
             'Historical subagent evidence belongs to another application')
    for key, expected in (('argv', request['argv']), ('cwd', request['app_root']),
                          ('problem_root', request['problem_root']), ('prompt', request['prompt']),
                          ('timeout', request['timeout'])):
        _require(command.get(key) == expected, 'Historical subagent command differs: ' + key)
    _require(command.get('problem_receipt') == problem_time.load(request['problem_root'], required=True),
             'Historical subagent problem receipt differs')
    _require(_read_bytes(directory / 'schema.json') == _json(request['schema'])
             and _read_bytes(directory / 'prompt.txt') == request['prompt'].encode('utf-8'),
             'Historical subagent prompt/schema differs')
    outcome = _command_outcome(directory / 'model', command, recovered=True)
    _require(outcome.get('exit_code') == 0 and outcome.get('termination_reason') == 'completed'
             and outcome['cleanup'].get('completed') is True, 'Historical subagent execution was not successful')
    answer = _read_bytes(directory / 'answer.json')
    parsed = _decode(answer)
    validate_schema(parsed, request['schema'])
    usage = read_usage(directory / 'model.stdout.log')
    expected = {'request_sha256': _digest(_json(request)), 'answer_sha256': _digest(answer),
                'command_receipt_sha256': _digest(_read_bytes(directory / 'model.receipt.json')),
                'result': parsed, 'usage': usage, 'run_id': directory.name}
    _require(result == expected, 'Historical subagent result differs from its evidence')
    return {'result': parsed, 'usage': usage, 'run_id': directory.name}
