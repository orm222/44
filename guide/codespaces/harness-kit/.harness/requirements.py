#!/usr/bin/env python3
"""Clarify, confirm requirements, then write and confirm the functional specification."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('requirements_clarify', BASE / 'clarify.py')
clarify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clarify)
boundary, lifecycle, problem_time = clarify.boundary, clarify.lifecycle, clarify.problem_time
operation_lock = clarify.operation_lock
STATE = '.harness/requirements.json'
DOCUMENT = 'CONFIRMED_REQUIREMENTS.md'
FUNCTIONAL_STATE = '.harness/functional-spec.json'
FUNCTIONAL_DOCUMENT = 'FUNCTIONAL_SPEC.md'
WAITING = 3
STATUSES = {'GENERATING', 'WAITING_CONFIRMATION', 'CONFIRMED', 'STALE'}
STAGES = {
    'requirements': dict(name='requirements', state=STATE, document=DOCUMENT,
        result='requirements_markdown', prompt='requirements_prompt.md', schema='requirements.schema.json',
        title='요구사항 정리 문서'),
    'functional': dict(name='functional', state=FUNCTIONAL_STATE, document=FUNCTIONAL_DOCUMENT,
        result='functional_spec_markdown', prompt='functional_prompt.md', schema='functional.schema.json',
        title='기능명세서'),
}


def _stage(value='requirements'):
    if isinstance(value, dict):
        directory = value.get('directory', '')
        names = [name for name in STAGES if re.fullmatch(r'\.harness/work/' + name + '-[0-9a-f]{32}', directory)]
        boundary.require(len(names) == 1, 'Invalid document transaction directory')
        value = names[0]
    boundary.require(isinstance(value, str) and value in STAGES, 'Unknown document stage')
    return STAGES[value]


def _path(root, name, exists=False):
    path = boundary.project_path(root, name, exists=exists, runtime=True)
    for item in (path, *path.parents):
        if item == root:
            break
        boundary.require(not item.exists() or not getattr(item.lstat(), 'st_file_attributes', 0) & 0x400,
                         'Requirements path is redirected: ' + str(item))
    return path


def _raw(path):
    path = Path(path).absolute()
    boundary.require(path == path.resolve() and not path.is_symlink(), 'Requirements file is redirected: ' + str(path))
    if not path.exists():
        return None
    before = path.lstat()
    boundary.require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                     and not getattr(before, 'st_file_attributes', 0) & 0x400, 'Requirements file must be ordinary: ' + str(path))
    data = path.read_bytes()
    after = path.lstat()
    boundary.require(path == path.resolve() and all(getattr(before, key) == getattr(after, key)
        for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode')),
        'Requirements file changed while reading: ' + str(path))
    return data


def _signed(value):
    return clarify._json_bytes(dict(value, _integrity=boundary.json_digest(value)))


def _read_signed(path):
    raw = _raw(path)
    if raw is None:
        return None
    value = boundary.decode(raw.decode('utf-8'))
    boundary.require(isinstance(value, dict), 'Invalid requirements record')
    checksum = value.pop('_integrity', None)
    boundary.require(checksum == boundary.json_digest(value), 'Requirements record checksum differs')
    return value


def _write(root, name, data, directory):
    clarify._atomic_runtime(_path(root, name), data, _path(root, directory + '/' + Path(name).name + '.tmp'))


def _save_journal(root, journal):
    _write(root, journal['directory'] + '/transaction.json', _signed(journal), journal['directory'])


def _invocation(journal):
    return boundary.json_digest({key: journal[key] for key in
        ('argv', 'context', 'source_files', 'start_files', 'prompt_sha256')})


def _binding(journal):
    # The pointer cannot hash the start-state bytes that contain that same hash.
    keys = ['argv', 'context', 'source_files', 'prompt_sha256']
    if 'registration_state_before' in journal:
        keys.append('registration_state_before')
    return boundary.json_digest({key: journal[key] for key in keys})


def _journal(root, state):
    value = _read_signed(_path(root, state['directory'] + '/transaction.json', exists=True))
    boundary.require(value is not None and value.get('version') == 1 and value.get('project') == str(root)
        and value.get('directory') == state['directory'] and value.get('revision') == state['revision']
        and value.get('stage') in ('starting', 'running', 'failed', 'publishing', 'completed'), 'Requirements journal context differs')
    boundary.require(_binding(value) == state['invocation_sha256']
        and value['context']['input_hashes'] == state['input_hashes'], 'Requirements invocation binding differs')
    return value


def _snapshot(root, directory):
    result = clarify.source_snapshot(root, directory)
    # Clarify owns this pointer, but a requirements worker does not.
    pending = _path(root, clarify.ANALYSIS_PENDING)
    data = _raw(pending)
    if data is not None:
        result[clarify.ANALYSIS_PENDING] = [stat.S_IMODE(pending.lstat().st_mode), boundary.digest(data)]
    # These two outputs have separate exact before/target checks; no broad exclusion.
    kind = _stage({'directory': directory})
    result.pop(kind['state'], None)
    result.pop(kind['document'], None)
    return result


def _inputs_match(root, state):
    return all((lambda data: data is not None and boundary.digest(data) == checksum)(_raw(_path(root, name)))
               for name, checksum in state['input_hashes'].items())


def _deadline(root, timeout=1):
    return problem_time.budget(root, timeout, required=True)


def _current_files(root, stage='requirements'):
    kind = _stage(stage)
    return {name: clarify._encoded(raw) if raw is not None else None
            for name in (kind['state'], kind['document']) for raw in [_raw(_path(root, name))]}


def _evidence(root, journal):
    kind = _stage(journal)
    folder = _path(root, journal['directory'] + '/transaction.json', exists=True).parent
    expected = clarify.ANALYSIS_FILES - {'analysis-transaction.json'}
    extras = {'transaction.json', 'confirmation.json', 'returned-confirmation.md'}
    temporaries = {'transaction.json.tmp', Path(kind['state']).name + '.tmp', kind['document'] + '.tmp',
                   'prompt.txt.tmp', 'result-receipt.json.tmp', 'confirmation.json.tmp', 'returned-confirmation.md.tmp'}
    result = {}
    for path in folder.iterdir():
        if path.name == 'model.pycache':
            boundary.require(path.is_dir() and path == path.resolve() and not path.is_symlink()
                and not getattr(path.lstat(), 'st_file_attributes', 0) & 0x400, 'Redirected requirements runtime cache')
            continue
        boundary.require(path.name in expected | extras | temporaries, 'Unexpected requirements execution evidence: ' + path.name)
        data = _raw(path)
        if path.name in expected:
            result[path.name] = boundary.digest(data)
    return result


def _validate_report(journal, value):
    key = _stage(journal)['result']
    boundary.require(isinstance(value, dict) and set(value) == {key}
        and boundary.text(value[key]), 'Invalid document model result')
    return value


def _report(root, journal):
    return _validate_report(journal, clarify._execution_result(root, journal))


def _document(journal, report):
    kind = _stage(journal)
    sources = '\n'.join('- `{}` — SHA256 `{}`'.format(name, checksum)
        for name, checksum in sorted(journal['context']['input_hashes'].items()))
    text = ('> ' + kind['title'] + '입니다. 확정 여부는 별도의 사용자 확인 기록으로 판정합니다.\n\n'
            + report[kind['result']].rstrip() + '\n\n---\n\n## 입력 출처\n\n'
            + sources + '\n\n## 실제 질문과 답변 원문\n\n' + journal['answers_markdown'])
    return text.encode('utf-8')


def _targets(journal, report):
    kind = _stage(journal)
    document = _document(journal, report)
    state = boundary.decode(clarify._decoded(journal['start_files'][kind['state']]).decode('utf-8'))
    checksum = state.pop('_integrity', None)
    boundary.require(checksum == boundary.json_digest(state), 'Requirements starting state checksum differs')
    state.update(status='WAITING_CONFIRMATION', document_sha256=boundary.digest(document), confirmation=None)
    return {kind['state']: clarify._encoded(_signed(state)), kind['document']: clarify._encoded(document)}


def _parent_reference(root, state):
    boundary.require(state['status'] == 'CONFIRMED', 'Confirm the requirements before generating a functional specification')
    return {'revision': state['revision'], 'document_sha256': state['document_sha256'],
            'state_sha256': boundary.digest(_raw(_path(root, STATE, exists=True))),
            'confirmation_token': state['confirmation']['token']}


def _document_context(root, spec, state, inputs, revision, upstream=None):
    context = {'project': str(root), 'spec': spec, 'references': state['references'],
        'input_hashes': inputs, 'revision': revision, 'actual_answers': state['answers']}
    if upstream is not None:
        context.update(requirements_document=DOCUMENT, upstream=upstream)
    summary = boundary.data_context(root, inputs)
    if summary is not None:
        context['data_checks'] = summary
    return context


def _source_views(root, state, journal):
    current, expected = _snapshot(root, journal['directory']), dict(journal['source_files'])
    if _stage(state)['name'] == 'requirements' and state['status'] == 'CONFIRMED':
        child = load(root, current=False, stage='functional')
        if child:
            child_journal = _journal(root, child)
            if child_journal['context'].get('upstream') == _parent_reference(root, state):
                _evidence(root, child_journal)
                owned = {child['directory']}
                # A retry keeps earlier failed attempts, sealed in its source snapshot.
                previous = {match.group(1) for name in child_journal['source_files']
                    for match in [re.match(r'(\.harness/work/functional-[0-9a-f]{32})/', name)] if match}
                for directory in previous:
                    prefix = directory + '/'
                    if any(name.startswith(prefix) for name in expected):
                        continue
                    failed = _read_signed(_path(root, prefix + 'transaction.json', exists=True))
                    boundary.require(failed is not None and failed.get('stage') == 'failed'
                        and failed.get('project') == str(root) and failed.get('directory') == directory
                        and failed['context'].get('upstream') == child_journal['context']['upstream']
                        and _retry_reason(root, failed), 'Unverified earlier functional attempt')
                    boundary.require({name: value for name, value in current.items() if name.startswith(prefix)}
                        == {name: value for name, value in child_journal['source_files'].items() if name.startswith(prefix)},
                        'Earlier functional failure evidence changed')
                    owned.add(directory)
                # Only this validated successor's exact outputs/runtime are independent.
                for files in (current, expected):
                    for name in list(files):
                        if name in (FUNCTIONAL_STATE, FUNCTIONAL_DOCUMENT) or any(name.startswith(path + '/') for path in owned):
                            del files[name]
    return current, expected


def _source_matches(root, state, journal):
    current, expected = _source_views(root, state, journal)
    return current == expected


def _recover_functional_registration(root, parent, previous, config, model, timeout):
    """Publish only a fully saved, unlaunched intent left before its state pointer."""
    if parent is None or parent['status'] != 'CONFIRMED' or (previous and previous['status'] == 'GENERATING'):
        return previous
    revision = previous['revision'] + 1 if previous else 1
    candidates = []
    work = _path(root, '.harness/work/operation.lock').parent
    for folder in sorted(work.glob('functional-*')):
        if previous and folder == root / previous['directory']:
            continue
        path = _path(root, folder.relative_to(root).as_posix() + '/transaction.json')
        if not path.exists():
            continue  # Partial/unrecognized intents remain visible to the source guard.
        journal = _read_signed(path)
        if journal.get('stage') == 'starting' and journal.get('revision') == revision:
            candidates.append(journal)
    if not candidates:
        return previous
    boundary.require(len(candidates) == 1, 'Multiple pending functional registrations; preserve the evidence')
    journal = candidates[0]
    kind = _stage(journal)
    boundary.require(kind['name'] == 'functional' and journal['context'].get('upstream') == _parent_reference(root, parent)
        and journal['context'].get('spec') == _journal(root, parent)['context']['spec'], 'Functional registration belongs to another confirmed input')
    boundary.require(_inputs_match(root, parent), 'Requirements inputs changed before functional registration')
    expected_inputs = dict(sorted(dict(parent['input_hashes'], **{STATE: boundary.digest(_raw(_path(root, STATE))), DOCUMENT: parent['document_sha256']}).items()))
    boundary.require(journal['context']['input_hashes'] == expected_inputs, 'Functional registration input seal differs')
    spec = _journal(root, parent)['context']['spec']
    boundary.require_ready(root, spec)
    boundary_state = boundary.load_state(root)
    context = _document_context(root, spec, boundary_state, expected_inputs, revision, _parent_reference(root, parent))
    prompt = (BASE / 'functional_prompt.md').read_text(encoding='utf-8')
    prompt += '\n\nREQUIREMENTS_CONTEXT\n' + json.dumps(context, ensure_ascii=False)
    boundary.require(boundary.text(model) and not model.startswith('SET_'), 'Supply the original registration model')
    argv = clarify.codex_command(config) + ['--ask-for-approval', 'never', 'exec', '--cd', str(root), '--model', model,
        '--sandbox', 'read-only', '--skip-git-repo-check', '--json', '--output-schema', str(BASE / 'functional.schema.json'),
        '--output-last-message', str(_path(root, journal['directory'] + '/answer.json')),
        '--config', 'shell_environment_policy.set.PYTHONDONTWRITEBYTECODE="1"', '-']
    boundary.require(journal['context'] == context and journal['prompt'] == prompt and journal['argv'] == argv
        and journal['answers_markdown'] == _raw(_path(root, boundary.ANSWERS, exists=True)).decode('utf-8'),
        'Functional registration command, prompt or actual answers differ from trusted current inputs')
    boundary.require(type(journal['timeout']) in (int, float) and 0 < journal['timeout'] <= min(timeout, problem_time.load(root, required=True)['limit_seconds']),
                     'Resume with the original registration timeout')
    state = boundary.decode(clarify._decoded(journal['start_files'][FUNCTIONAL_STATE]).decode('utf-8'))
    checksum = state.pop('_integrity', None)
    expected_state = dict(version=1, project=str(root), revision=revision, status='GENERATING',
        directory=journal['directory'], invocation_sha256=_binding(journal), input_hashes=expected_inputs,
        document_sha256=None, confirmation=None)
    boundary.require(state == expected_state and checksum == boundary.json_digest(state), 'Functional registration target is invalid')
    _journal(root, state)
    current = _current_files(root, 'functional')
    boundary.require('registration_state_before' in journal and current[FUNCTIONAL_STATE] == journal['registration_state_before']
        and current[FUNCTIONAL_DOCUMENT] == journal['start_files'][FUNCTIONAL_DOCUMENT], 'Functional registration output changed')
    folder = _path(root, journal['directory'] + '/transaction.json', exists=True).parent
    temporary_name = Path(FUNCTIONAL_STATE).name + '.tmp'
    boundary.require({path.name for path in folder.iterdir()} <= {'transaction.json', temporary_name}
        and all(journal.get(key) is None for key in ('report', 'evidence', 'target_files', 'result_receipt_sha256')),
        'Functional registration has execution or unexpected evidence; do not replay it')
    for path in folder.iterdir():
        data = _raw(path)
        if path.name == temporary_name:
            boundary.require(_signed(state).startswith(data), 'Functional registration temporary differs')
    boundary.require(boundary.digest(journal['prompt'].encode('utf-8')) == journal['prompt_sha256']
        and _snapshot(root, journal['directory']) == journal['source_files'], 'Functional registration source/prompt changed')
    actual, expected = _source_views(root, parent, _journal(root, parent))
    prefix = journal['directory'] + '/'
    boundary.require(not any(name.startswith(prefix) for name in expected), 'Functional registration is not a new directory')
    actual = {name: value for name, value in actual.items() if not name.startswith(prefix)}
    boundary.require(actual == expected, 'Other source changes prevent functional registration recovery')
    _deadline(root)
    _write(root, FUNCTIONAL_STATE, _signed(state), journal['directory'])
    return load(root, current=False, stage='functional')


def _verify_finished(root, state, current=True):
    kind = _stage(state)
    journal = _journal(root, state)
    boundary.require(journal['stage'] == 'completed' and journal['evidence'] == _evidence(root, journal),
                     'Requirements execution evidence is incomplete or changed')
    report = _report(root, journal)
    expected = _document(journal, report)
    boundary.require(report == journal['report'] and _targets(journal, report) == journal['target_files']
        and state['document_sha256'] == boundary.digest(expected) and _raw(_path(root, kind['document'])) == expected,
        'Requirements document changed outside its publication')
    waiting = dict(state, status='WAITING_CONFIRMATION', confirmation=None)
    boundary.require(clarify._encoded(_signed(waiting)) == journal['target_files'][kind['state']], 'Document metadata differs from its generated result')
    if state['status'] == 'CONFIRMED':
        saved = _read_signed(_path(root, journal['directory'] + '/confirmation.json', exists=True))
        returned = _raw(_path(root, journal['directory'] + '/returned-confirmation.md', exists=True))
        confirmation = state['confirmation']
        boundary.require(isinstance(confirmation, dict) and set(confirmation) == {'token', 'confirmed_at', 'returned_sha256'}
            and boundary.text(confirmation['confirmed_at']) and confirmation['returned_sha256'] == boundary.digest(expected)
            and saved == state and returned == expected and confirmation['token'] == _token(waiting),
                         'Requirements confirmation evidence differs')
    else:
        boundary.require(state['confirmation'] is None, 'Unconfirmed requirements cannot contain approval')
    if current:
        boundary.require(state['status'] != 'STALE' and _inputs_match(root, state), 'Requirements inputs changed; regenerate before confirmation')
        boundary.require(_source_matches(root, state, journal), 'Source changed since document generation')
        if kind['name'] == 'functional':
            parent = load(root)
            boundary.require(parent is not None and journal['context'].get('upstream') == _parent_reference(root, parent),
                             'The confirmed requirements changed; regenerate the functional specification')
    return journal


def load(root, current=True, stage='requirements'):
    root = Path(root).resolve()
    kind = _stage(stage)
    state = _read_signed(_path(root, kind['state']))
    if state is None:
        return None
    keys = {'version', 'project', 'revision', 'status', 'directory', 'invocation_sha256',
            'input_hashes', 'document_sha256', 'confirmation'}
    boundary.require(set(state) == keys and type(state['version']) is int and state['version'] == 1
        and state['project'] == str(root) and type(state['revision']) is int and state['revision'] > 0
        and state['status'] in STATUSES and isinstance(state['directory'], str)
        and re.fullmatch(r'\.harness/work/' + kind['name'] + '-[0-9a-f]{32}', state['directory']), 'Invalid document state')
    boundary.require(isinstance(state['input_hashes'], dict) and state['input_hashes']
        and all(isinstance(checksum, str) and re.fullmatch('[0-9a-f]{64}', checksum)
                for checksum in state['input_hashes'].values()), 'Invalid requirements input hashes')
    for name in state['input_hashes']:
        boundary.require(name not in (kind['state'], kind['document']), 'Document output cannot be its input')
        _path(root, name)
    journal = _journal(root, state)
    if journal['stage'] == 'failed':
        boundary.require(_retry_reason(root, journal), 'Invalid failed document execution')
    if journal['stage'] != 'completed':
        before = journal['start_files'][kind['document']]
        target = (journal.get('target_files') or {}).get(kind['document'], before)
        actual = _current_files(root, kind['name'])[kind['document']]
        boundary.require(actual in (before, target), 'Document changed outside the pending transaction')
    if state['status'] != 'GENERATING' and journal['stage'] == 'completed':
        _verify_finished(root, state, current=current)
    elif current:
        boundary.require(_inputs_match(root, state), 'Requirements inputs changed during generation')
    return state


def _token(state):
    return boundary.json_digest({key: state[key] for key in
        ('project', 'revision', 'input_hashes', 'document_sha256', 'invocation_sha256')})


def _guard(root, journal):
    _deadline(root)
    boundary.require(_snapshot(root, journal['directory']) == journal['source_files'], 'Source changed during requirements generation')
    boundary.require(_inputs_match(root, {'input_hashes': journal['context']['input_hashes']}), 'Requirements inputs changed')
    boundary.require(_current_files(root, _stage(journal)['name']) == journal['start_files'], 'Document outputs changed during generation')


def _prepare_result(root, journal):
    _guard(root, journal)
    evidence = _evidence(root, journal)
    report = _report(root, journal)
    boundary.require(evidence == _evidence(root, journal), 'Requirements execution evidence changed while validating')
    journal.update(stage='publishing', report=report, evidence=evidence,
                   target_files=_targets(journal, report))
    _save_journal(root, journal)


def _publish(root, journal):
    kind = _stage(journal)
    _deadline(root)
    boundary.require(_snapshot(root, journal['directory']) == journal['source_files']
        and _inputs_match(root, {'input_hashes': journal['context']['input_hashes']}), 'Requirements inputs changed during recovery')
    boundary.require(_evidence(root, journal) == journal['evidence'] and _report(root, journal) == journal['report'],
                     'Requirements response or execution evidence changed')
    boundary.require(_targets(journal, journal['report']) == journal['target_files'],
                     'Requirements publication target differs from model output')
    current = _current_files(root, kind['name'])
    for name in (kind['document'], kind['state']):
        boundary.require(current[name] in (journal['start_files'][name], journal['target_files'][name]),
                         'Unexpected requirements edit during publication: ' + name)
    for name in (kind['document'], kind['state']):
        _deadline(root)
        actual = _current_files(root, kind['name'])[name]
        boundary.require(actual in (journal['start_files'][name], journal['target_files'][name]),
                         'Requirements file changed during publication: ' + name)
        if actual != journal['target_files'][name]:
            _write(root, name, clarify._decoded(journal['target_files'][name]), journal['directory'])
    boundary.require(_current_files(root, kind['name']) == journal['target_files'], 'Incomplete document publication')
    _deadline(root)
    journal['stage'] = 'completed'
    _save_journal(root, journal)
    return load(root, stage=kind['name'])


def _prelaunch(root, journal):
    _guard(root, journal)
    folder = _path(root, journal['directory'] + '/transaction.json', exists=True).parent
    boundary.require(not (folder / 'model.launch.json').exists(), 'Requirements launch exists; automatic replay is forbidden')
    allowed = {'prompt.txt', 'model.input.txt', 'model.command.json', 'model.stdout.log', 'model.stderr.log'}
    boundary.require(set(_evidence(root, journal)) <= allowed, 'Unexpected requirements evidence before launch')
    prompt = journal['prompt'].encode('utf-8')
    boundary.require(boundary.digest(prompt) == journal['prompt_sha256'], 'Requirements prompt differs')
    for name in ('prompt.txt', 'model.input.txt'):
        data = _raw(folder / name)
        expected = prompt if name == 'prompt.txt' else journal['prompt'].replace('\n', os.linesep).encode('utf-8')
        boundary.require(data is None or expected.startswith(data), 'Unexpected prelaunch requirements input')
    for name in ('model.stdout.log', 'model.stderr.log'):
        boundary.require(_raw(folder / name) in (None, b''), 'Requirements output exists without a launch receipt')
    command = _raw(folder / 'model.command.json')
    if command is not None:
        boundary.require(boundary.decode(command.decode('utf-8')) == {'argv': journal['argv'], 'cwd': str(root),
            'timeout': journal['timeout'], 'python_environment': {'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONPYCACHEPREFIX': str((folder / 'model.pycache').resolve())}}, 'Requirements prelaunch command differs')
    cache = folder / 'model.pycache'
    if cache.exists():
        boundary.require(cache.is_dir() and not cache.is_symlink() and cache == cache.resolve()
            and not any(cache.iterdir()), 'Unexpected requirements prelaunch cache')
        cache.rmdir()
    for name in allowed - {'prompt.txt'}:
        path = folder / name
        if path.exists():
            path.unlink()
    _write(root, journal['directory'] + '/prompt.txt', prompt, journal['directory'])


def _resume(root, state, allow_launch=True):
    journal = _journal(root, state)
    boundary.require(state['status'] != 'STALE' and journal['stage'] != 'failed',
                     'Failed or stale document generation must be regenerated before confirmation')
    if journal['stage'] == 'completed':
        _verify_finished(root, state)
        return state
    if journal['stage'] in ('starting', 'running'):
        folder = _path(root, journal['directory'] + '/transaction.json', exists=True).parent
        if not (folder / 'model.exit.json').exists():
            boundary.require(not (folder / 'model.launch.json').exists(),
                'Requirements process cleanup is unconfirmed; preserve evidence, do not call the model again')
            boundary.require(allow_launch, 'Confirmation cannot launch a requirements model')
            _prelaunch(root, journal)
            journal['stage'] = 'running'
            _save_journal(root, journal)
            def stop(process):
                clarify.stop_process(process)
                if process.poll() == 0:
                    answer = _raw(folder / 'answer.json')
                    boundary.require(answer is not None, 'Requirements model answer is missing')
                    receipt = {'answer_sha256': boundary.digest(answer), 'invocation_sha256': _invocation(journal)}
                    raw = clarify._json_bytes(receipt)
                    _write(root, journal['directory'] + '/result-receipt.json', raw, journal['directory'])
                    journal['result_receipt_sha256'] = boundary.digest(raw)
                    _save_journal(root, journal)
            try:
                code = lifecycle.execute(journal['argv'], root, folder / 'model', journal['timeout'], journal['prompt'],
                    lambda: _guard(root, journal), deadline_guard=lambda: _deadline(root),
                    start_process=clarify.start_process, stop_process=stop, error_type=boundary.BoundaryError)
            except BaseException:
                _retire_failed(root, state)
                raise
            if code != 0:
                _retire_failed(root, state)
            boundary.require(code == 0, 'Requirements model failed; preserve execution evidence')
        try:
            _prepare_result(root, journal)
        except BaseException:
            _retire_failed(root, state)
            raise
    return _publish(root, journal)


def _retry_reason(root, journal):
    """A finished attempt can retry only for a failure or a superseded valid input."""
    folder = _path(root, journal['directory'] + '/transaction.json', exists=True).parent
    raw = _raw(folder / 'model.exit.json')
    if raw is None:
        return False
    outcome = boundary.decode(raw.decode('utf-8'))
    boundary.require(isinstance(outcome, dict) and isinstance(outcome.get('cleanup'), dict),
                     'Invalid document execution outcome')
    if outcome['cleanup'].get('completed') is not True:
        return False
    boundary.require(outcome.get('version') == 1 and type(outcome.get('exit_code')) is int
        and boundary.text(outcome.get('started_at')) and boundary.text(outcome.get('finished_at'))
        and outcome['cleanup'].get('attempted') is True
        and outcome.get('termination_reason') in ('completed', 'timed_out', 'guard_failed', 'cancelled', 'execution_failed'),
        'Document failure has incomplete execution or cleanup evidence')
    def read(name):
        data = _raw(folder / name)
        boundary.require(data is not None, 'Document failure evidence is missing: ' + name)
        return boundary.decode(data.decode('utf-8'))
    command, launch, running = read('model.command.json'), read('model.launch.json'), read('model.running.json')
    boundary.require(command.get('argv') == journal['argv'] and command.get('cwd') == str(root)
        and command.get('timeout') == journal['timeout']
        and launch.get('started_at') == running.get('started_at') == outcome['started_at']
        and type(running.get('pid')) is int and running['pid'] > 0, 'Document failure invocation differs')
    evidence = _evidence(root, journal)
    if journal['stage'] == 'failed':
        boundary.require(journal['evidence'] == evidence, 'Document failure evidence changed')
    reason = 'execution_failed'
    if outcome['termination_reason'] == 'completed' and outcome['exit_code'] == 0:
        # Validate provenance outside the parse exception handler: tampering is never retryable.
        raw_answer = clarify._execution_result(root, journal, decode_answer=False)
        try:
            report = _validate_report(journal, boundary.decode(raw_answer.decode('utf-8-sig')))
        except ValueError:
            boundary.require(journal['stage'] != 'publishing', 'A validated publication result became invalid')
            reason = 'invalid_result'
        else:
            if journal['stage'] == 'publishing':
                boundary.require(evidence == journal['evidence'] and report == journal['report']
                    and _targets(journal, report) == journal['target_files'], 'Validated document publication changed')
            if not _inputs_match(root, {'input_hashes': journal['context']['input_hashes']}):
                reason = 'inputs_changed'
            elif _snapshot(root, journal['directory']) != journal['source_files']:
                reason = 'source_changed'
            else:
                reason = None
            if journal['stage'] == 'failed' and journal.get('retry_reason') in ('inputs_changed', 'source_changed'):
                reason = journal['retry_reason']
    boundary.require(evidence == _evidence(root, journal), 'Document failure evidence changed while validating')
    if journal['stage'] == 'failed':
        boundary.require(reason and reason == journal.get('retry_reason'), 'Document failure reason differs')
    return reason


def _retire_failed(root, state):
    if state is not None and state['status'] in ('GENERATING', 'STALE', 'WAITING_CONFIRMATION'):
        journal = _journal(root, state)
        reason = _retry_reason(root, journal) if journal['stage'] in ('starting', 'running', 'publishing', 'failed') else None
        if reason:
            expected = dict(journal['start_files'])
            expected[_stage(state)['state']] = clarify._encoded(_signed(state))
            current = _current_files(root, _stage(state)['name'])
            kind = _stage(state)
            target = (journal.get('target_files') or {}).get(kind['document'], expected[kind['document']])
            boundary.require(current[kind['state']] == expected[kind['state']]
                and current[kind['document']] in (expected[kind['document']], target),
                             'Document outputs changed before retiring the failed attempt')
            if journal['stage'] != 'failed':
                journal.update(stage='failed', retry_reason=reason, evidence=_evidence(root, journal))
                _save_journal(root, journal)
            return _invalidate(root, state)
    return state


def _invalidate(root, state):
    if state is not None and state['status'] != 'STALE':
        journal = _journal(root, state)
        if journal['stage'] != 'completed':
            folder = _path(root, state['directory'] + '/transaction.json', exists=True).parent
            if (folder / 'model.launch.json').exists():
                raw = _raw(folder / 'model.exit.json')
                outcome = boundary.decode(raw.decode('utf-8')) if raw is not None else {}
                boundary.require(outcome.get('cleanup', {}).get('completed') is True,
                                 'Document process cleanup is unconfirmed; preserve this attempt')
        state = dict(state, status='STALE', confirmation=None)
        _write(root, _stage(state)['state'], _signed(state), state['directory'])
    return state


def generate(root, spec, config, model, timeout=600, stage='requirements'):
    """The caller owns the shared project operation lock."""
    kind = _stage(stage)
    boundary.require_ready(root, spec)
    boundary.require(boundary.text(model) and not model.startswith('SET_'), 'Supply an actual requirements model ID')
    previous = _retire_failed(root, load(root, current=False, stage=stage))
    upstream = None
    if stage == 'functional':
        _recover_functional_registration(root, load(root, current=False), load(root, current=False, stage='functional'),
                                         config, model, timeout)
        parent = load(root)
        boundary.require(parent is not None, 'No confirmed requirements exist')
        upstream = _parent_reference(root, parent)
    previous = load(root, current=False, stage=stage)
    if previous:
        boundary.require(_journal(root, previous)['context']['spec'] == spec,
                         'This project already belongs to another specification; use a new project')
    if previous and previous['status'] == 'GENERATING':
        boundary.require(_journal(root, previous)['context']['spec'] == spec,
                         'Pending requirements belong to another specification')
        return _resume(root, previous)
    if previous and previous['status'] != 'STALE' and _inputs_match(root, previous) \
            and _journal(root, previous)['context']['spec'] == spec:
        _verify_finished(root, previous)
        return previous
    boundary_state = boundary.load_state(root)
    names = sorted(set(boundary_state['input_hashes']) | {boundary.STATE, boundary.ANSWERS})
    boundary.require(not set(names) & {STATE, DOCUMENT, FUNCTIONAL_STATE, FUNCTIONAL_DOCUMENT},
                     'Generated documents cannot be original input specifications/references')
    if upstream is not None:
        names = sorted(set(names) | {STATE, DOCUMENT})
    boundary.require(previous is not None or _raw(_path(root, kind['document'])) is None,
                     'Existing document is not owned; refusing overwrite')
    inputs = {name: boundary.digest(_raw(_path(root, name, exists=True))) for name in names}
    directory = '.harness/work/' + kind['name'] + '-' + uuid.uuid4().hex
    revision = previous['revision'] + 1 if previous else 1
    context = _document_context(root, spec, boundary_state, inputs, revision, upstream)
    prompt = (BASE / kind['prompt']).read_text(encoding='utf-8')
    prompt += '\n\nREQUIREMENTS_CONTEXT\n' + json.dumps(context, ensure_ascii=False)
    answer = _path(root, directory + '/answer.json')
    argv = clarify.codex_command(config) + ['--ask-for-approval', 'never', 'exec', '--cd', str(root), '--model', model,
        '--sandbox', 'read-only', '--skip-git-repo-check', '--json', '--output-schema', str(BASE / kind['schema']),
        '--output-last-message', str(answer), '--config', 'shell_environment_policy.set.PYTHONDONTWRITEBYTECODE="1"', '-']
    state = {'version': 1, 'project': str(root), 'revision': revision, 'status': 'GENERATING', 'directory': directory,
        'invocation_sha256': None, 'input_hashes': inputs, 'document_sha256': None, 'confirmation': None}
    # Bind the pointer before freezing its start bytes into the execution receipt.
    start = {kind['state']: clarify._encoded(_signed(state)),
             kind['document']: _current_files(root, stage)[kind['document']]}
    journal = {'version': 1, 'project': str(root), 'revision': revision, 'directory': directory, 'stage': 'starting',
        'context': context, 'argv': argv, 'timeout': _deadline(root, timeout), 'prompt': prompt,
        'prompt_sha256': boundary.digest(prompt.encode('utf-8')), 'source_files': _snapshot(root, directory),
        'start_files': start, 'answers_markdown': _raw(_path(root, boundary.ANSWERS, exists=True)).decode('utf-8'),
        'result_receipt_sha256': None, 'report': None, 'evidence': None, 'target_files': None}
    if stage == 'functional':
        journal['registration_state_before'] = _current_files(root, stage)[kind['state']]
    state['invocation_sha256'] = _binding(journal)
    journal['start_files'][kind['state']] = clarify._encoded(_signed(state))
    _save_journal(root, journal)
    _write(root, kind['state'], _signed(state), directory)
    return _resume(root, state)


def _external(root, filename):
    path = Path(filename).expanduser().absolute()
    boundary.require(path != root and root not in path.parents and root not in path.resolve().parents,
                     'Answer/confirmation file must be outside the project')
    data = _raw(path)
    boundary.require(data is not None, 'Answer/confirmation file is missing')
    return data


def _confirm(root, state, filename):
    kind = _stage(state)
    _deadline(root)
    returned = _external(root, filename)
    state = _resume(root, state, allow_launch=False)
    boundary.require(state['status'] in ('WAITING_CONFIRMATION', 'CONFIRMED'), 'Requirements are not awaiting confirmation')
    _verify_finished(root, state)
    boundary.require(returned == _raw(_path(root, kind['document'])), 'Returned document differs byte-for-byte')
    _deadline(root)
    if state['status'] == 'CONFIRMED':
        return state
    confirmed = dict(state, status='CONFIRMED', confirmation={'token': _token(state),
        'confirmed_at': datetime.now(timezone.utc).isoformat(), 'returned_sha256': boundary.digest(returned)})
    for name, data in (('returned-confirmation.md', returned), ('confirmation.json', _signed(confirmed))):
        path = _path(root, state['directory'] + '/' + name)
        existing = _raw(path)
        if name == 'confirmation.json' and existing is not None:
            saved = _read_signed(path)
            boundary.require(dict(saved, confirmation=None, status='WAITING_CONFIRMATION') == state
                and saved['confirmation']['token'] == _token(state)
                and saved['confirmation']['returned_sha256'] == boundary.digest(returned), 'Unrelated pending requirements confirmation')
            confirmed, data = saved, existing
        boundary.require(existing in (None, data), 'Confirmation evidence changed')
        _write(root, state['directory'] + '/' + name, data, state['directory'])
    _deadline(root)
    _verify_finished(root, state)
    _deadline(root)
    _write(root, kind['state'], _signed(confirmed), state['directory'])
    _deadline(root)
    return load(root, stage=kind['name'])


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('spec', 'config', 'model', 'received-at', 'answer-file'):
        parser.add_argument('--' + name)
    parser.add_argument('--root', default=str(BASE.parent))
    parser.add_argument('--timeout', type=int, default=600)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--answer', metavar='Q-ID')
    modes.add_argument('--confirm-requirements', '--confirm-file', dest='confirm_file', metavar='RETURNED_MD')
    modes.add_argument('--confirm-functional', metavar='RETURNED_MD')
    modes.add_argument('--status', action='store_true')
    args = parser.parse_args(arguments)
    try:
        boundary.require(sys.version_info[:2] >= (3, 9), 'Requirements requires Python 3.9 or newer')
        root = Path(args.root).absolute()
        boundary.require(root.is_dir() and root == root.resolve(), 'Requirements project root is missing or redirected')
        config, config_path = clarify.configuration(root, args.config)
        if 'project' in config:
            root = (config_path.parent / config['project']).resolve()
        boundary.require(args.timeout > 0 and bool(args.answer) == bool(args.answer_file),
                         'Use a positive timeout and --answer Q-ID with --answer-file')
        problem_time.required(config)
        model = args.model or config.get('model') or config.get('builder_model')
        if args.status:
            state = load(root, current=False)
            functional = load(root, current=False, stage='functional')
            view = dict(state) if state else {'status': 'NOT_STARTED'}
            view['requirements_status'] = state['status'] if state else 'NOT_STARTED'
            view['functional_status'] = functional['status'] if functional else 'NOT_STARTED'
            raw_boundary = _raw(_path(root, boundary.STATE))
            if raw_boundary is not None:
                saved = boundary.decode(raw_boundary.decode('utf-8-sig'))
                boundary.validate_state(root, saved, current=False, document=False)
                unanswered = sorted({q['id'] for q in saved['questions']} - set(boundary.actual_answers(saved)))
                view['unanswered_question_ids'] = unanswered
                if state is None:
                    view['status'] = 'WAITING_ANSWERS' if unanswered else 'READY_TO_WRITE' if saved['status'] == 'ready' else 'NEEDS_ANALYSIS'
            if state and not _inputs_match(root, state):
                view['status'] = 'STALE'
                view['requirements_status'] = view['functional_status'] = 'STALE'
            elif functional:
                view['functional_status'] = functional['status'] if _inputs_match(root, functional) else 'STALE'
                if state and state['status'] == 'CONFIRMED':
                    view['status'] = ('COMPLETE' if view['functional_status'] == 'CONFIRMED'
                                      else 'WAITING_FUNCTIONAL_CONFIRMATION' if view['functional_status'] == 'WAITING_CONFIRMATION'
                                      else view['functional_status'])
            print(json.dumps(view, ensure_ascii=False, indent=2))
            return 0
        spec = clarify.selected_spec(root, args.spec, config)
        boundary.require(spec not in (STATE, DOCUMENT, FUNCTIONAL_STATE, FUNCTIONAL_DOCUMENT),
                         'Generated document cannot be its specification')
        with operation_lock.operation_lock(root, 'documents'):
            state = _retire_failed(root, load(root, current=False))
            functional = _retire_failed(root, load(root, current=False, stage='functional'))
            boundary.require(state is not None or functional is None, 'Functional specification has no requirements record')
            if state and state['status'] == 'CONFIRMED' and functional and functional['status'] == 'STALE' \
                    and _journal(root, functional)['stage'] == 'failed' and not _source_matches(root, state, _journal(root, state)):
                # A stopped child must not strand the confirmed parent after a user source edit.
                state = _invalidate(root, state)
            for saved in (state, functional):
                if saved:
                    boundary.require(_journal(root, saved)['context']['spec'] == spec,
                                     'This project already belongs to another specification; use a new project')
            raw_boundary = _raw(_path(root, boundary.STATE))
            if raw_boundary is not None:
                saved_boundary = boundary.decode(raw_boundary.decode('utf-8-sig'))
                boundary.validate_state(root, saved_boundary, current=False, document=False)
                boundary.require(saved_boundary['spec'] == spec,
                                 'This project already belongs to another specification; use a new project')
            functional = _recover_functional_registration(root, state, functional, config, model, args.timeout)
            if args.confirm_functional:
                boundary.require(state is not None and functional is not None, 'No functional specification to confirm')
                functional = _confirm(root, functional, args.confirm_functional)
            elif args.confirm_file:
                boundary.require(state is not None, 'No generated requirements to confirm')
                boundary.require(_journal(root, state)['context']['spec'] == spec,
                                 'Selected specification differs from the generated requirements')
                state = _confirm(root, state, args.confirm_file)
            else:
                if state:
                    problem_time.load(root, required=True)
                problem_time.begin(root, received_at=args.received_at)
                clarify.recover_answer(root, spec)
                answer_text, new_answer = None, bool(args.answer)
                if args.answer:
                    answer_text = _external(root, args.answer_file).decode('utf-8-sig')
                    saved_boundary = boundary.load_state(root, current=False)
                    new_answer = boundary.actual_answers(saved_boundary).get(args.answer) != answer_text
                if state and state['status'] != 'STALE' and (state['status'] == 'GENERATING' or _journal(root, state)['stage'] != 'completed'):
                    boundary.require(not new_answer, 'Recover the pending requirements generation before changing answers')
                    boundary.require(_journal(root, state)['context']['spec'] == spec,
                                     'Pending requirements belong to another specification')
                    state = _resume(root, state)
                elif state and not new_answer and state['status'] != 'STALE' and _inputs_match(root, state) \
                        and _journal(root, state)['context']['spec'] == spec:
                    _deadline(root)
                    _verify_finished(root, state)
                else:
                    boundary.require(state is not None or _raw(_path(root, DOCUMENT)) is None,
                                     'Existing requirements document is not owned; refusing overwrite')
                    functional = _invalidate(root, functional)
                    state = _invalidate(root, state)
                    if new_answer:
                        clarify.submit_answer(root, spec, args.answer, answer_text)
                    _deadline(root)
                    clarify.recover_analysis(root, spec, deadline_check=lambda: _deadline(root))
                    boundary_state = boundary.load_state(root, current=False) if (root / boundary.STATE).exists() else None
                    if boundary_state and {q['id'] for q in boundary_state['questions']} - set(boundary.actual_answers(boundary_state)):
                        clarify.show(boundary_state)
                        return WAITING
                    ready = False
                    try:
                        boundary.require_ready(root, spec)
                        ready = True
                    except boundary.BoundaryError:
                        pass
                    if not ready:
                        code = clarify.analyze(root, spec, config, model,
                                               args.timeout, allow_nested=True)
                        if code != 0:
                            boundary_state = boundary.load_state(root)
                            return WAITING if boundary_state['status'] == 'needs_answers' else 2
                    state = generate(root, spec, config, model, args.timeout)
            if not args.confirm_functional and state['status'] == 'CONFIRMED':
                functional = generate(root, spec, config, model, args.timeout, stage='functional')
            _deadline(root)
            if state['status'] == 'CONFIRMED' and functional and functional['status'] == 'CONFIRMED':
                print('DOCUMENTS COMPLETE: requirements and functional specification confirmed. Use develop for implementation or run for the full workflow.')
                return 0
            active = functional if state['status'] == 'CONFIRMED' and functional else state
            kind = _stage(active)
            print(active['status'] + ': ' + str(root / kind['document']))
            if active['status'] == 'WAITING_CONFIRMATION':
                receipt = problem_time.status(root, required=True)
                timing = boundary.decode(_raw(_path(root, active['directory'] + '/model.exit.json')).decode('utf-8'))
                started = datetime.fromisoformat(timing['started_at'])
                minutes = max(0, (datetime.now(timezone.utc) - started).total_seconds() / 60)
                estimates = config.get('document_stage_minutes', {})
                boundary.require(isinstance(estimates, dict), 'document_stage_minutes must be an object')
                planned = estimates.get(kind['name'], 15 if kind['name'] == 'requirements' else 10)
                limit_minutes = receipt['limit_seconds'] // 60
                boundary.require(type(planned) in (int, float) and 0 < planned <= limit_minutes,
                                 'Document stage estimate must be between 0 and {} minutes'.format(limit_minutes))
                print('[GATE {}] 사용자 확인 필요 — 전체 경과 약 {:.1f}분 / 제한 {}분; 문서 단계 {:.1f}분 / 계획 {}분'.format(
                    kind['name'], max(0, (datetime.now(timezone.utc).timestamp() - receipt['received_at']) / 60), limit_minutes, minutes, planned))
                if type(planned) in (int, float) and minutes > planned:
                    print('지연 제안: 부가 범위 축소를 담당자와 확인하세요. 필수 조건과 검증은 유지합니다.')
                print('Return the reviewed document with --confirm-' + kind['name'] +
                      '. Confirmation token: ' + _token(active))
            return WAITING
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, operation_lock.OperationBusy) as exc:
        print('Requirements stopped: ' + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Requirements interrupted; preserve the current execution evidence', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
