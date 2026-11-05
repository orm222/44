"""Offline child processes exercise publication, guards and durable receipts."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


BASE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('development_runtime_tests', BASE / 'development_runtime.py')
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)

SCHEMA = {'type': 'object', 'properties': {'summary': {'type': 'string'}},
          'required': ['summary'], 'additionalProperties': False}
FAKE = r'''
import json, sys
from pathlib import Path
root = Path(__file__).parent
scenario = json.loads((root / 'scenario.json').read_text(encoding='utf-8'))
count = root / 'calls.txt'
count.write_text(str(int(count.read_text()) + 1) if count.exists() else '1')
arguments = sys.argv[1:]
sys.stdin.read()
if scenario.get('mutate'):
    path = Path(scenario['mutate'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('unexpected worker edit', encoding='utf-8')
Path(arguments[arguments.index('--output-last-message') + 1]).write_text(
    json.dumps(scenario.get('result', {'summary': '검토 완료'})), encoding='utf-8')
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 123,
                   'cached_input_tokens': 42, 'output_tokens': 17}}))
raise SystemExit(scenario.get('exit', 0))
'''


class Interrupted(BaseException):
    pass


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='development runtime ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = self.root / 'app-project'
        self.problem = self.root / 'problem-project'
        self.app.mkdir()
        self.problem.mkdir()
        (self.problem / 'SPEC.md').write_text('문제 설명', encoding='utf-8')
        runtime.problem_time.begin(self.problem)
        self.prefix = self.app / '.harness' / 'checks' / 'first'

    def write(self, name, text):
        target = self.app / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        return target

    def command(self, code, **options):
        return runtime.run_command([sys.executable, '-c', code], self.app, self.prefix,
                                   self.problem, options.pop('timeout', 10), **options)

    def fake(self, **scenario):
        script = self.root / 'fake.py'
        script.write_text(FAKE, encoding='utf-8')
        (self.root / 'scenario.json').write_text(json.dumps(scenario), encoding='utf-8')
        return {'model': 'offline-fixture', 'codex_command': [sys.executable, str(script)]}

    def agent(self, config, role='implementer'):
        return runtime.run_agent(role, '한국어로 결과를 반환하세요.', SCHEMA, self.app, self.problem,
                                 self.app / '.harness' / 'runs' / 'attempt-1', config, timeout=10)

    def test_records_reject_tampering_and_resume_partial_atomic_write(self):
        path = self.app / '.harness' / 'state.json'
        runtime.write_record(path, {'version': 1})
        self.assertEqual(runtime.read_record(path), {'version': 1})
        value = json.loads(path.read_text(encoding='utf-8'))
        value['version'] = 2
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            runtime.read_record(path)
        expected = {'answer': '보존할 답변'}
        raw = runtime._json(dict(expected, _integrity=runtime._digest(runtime._json(expected))))
        path.with_name(path.name + '.tmp').write_bytes(raw[:8])
        runtime.write_record(path, expected)
        self.assertEqual(runtime.read_record(path), expected)

    def test_record_intent_recovers_every_publication_boundary(self):
        for boundary in ('partial-intent', 'full-intent-temp', 'before-target', 'partial-target', 'after-target'):
            with self.subTest(boundary=boundary):
                path = self.app / '.harness' / (boundary + '.json')
                runtime.write_record(path, {'state': 'before'})
                original = runtime._atomic
                def interrupt(target, data, temporary=None, mode=None):
                    target = Path(target)
                    if target.name == path.name + '.intent' and boundary in ('partial-intent', 'full-intent-temp'):
                        target.with_name(target.name + '.tmp').write_bytes(data[:23] if boundary == 'partial-intent' else data)
                        raise Interrupted()
                    if target == path:
                        if boundary == 'partial-target':
                            target.with_name(target.name + '.tmp').write_bytes(data[:17])
                            raise Interrupted()
                        if boundary == 'before-target':
                            raise Interrupted()
                        if boundary == 'after-target':
                            original(target, data, temporary, mode)
                            raise Interrupted()
                    return original(target, data, temporary, mode)
                with patch.object(runtime, '_atomic', side_effect=interrupt), self.assertRaises(Interrupted):
                    runtime.write_record(path, {'state': 'after'})
                expected = 'before' if boundary == 'partial-intent' else 'after'
                self.assertEqual(runtime.read_record(path), {'state': expected})
                self.assertFalse(path.with_name(path.name + '.intent').exists())
                self.assertFalse(path.with_name(path.name + '.intent.tmp').exists())
                self.assertFalse(path.with_name(path.name + '.tmp').exists())

    def test_record_intent_refuses_unexpected_user_target(self):
        path = self.app / '.harness/state.json'
        runtime.write_record(path, {'state': 'before'})
        original = runtime._atomic
        def interrupt(target, data, temporary=None, mode=None):
            if Path(target) == path:
                raise Interrupted()
            return original(target, data, temporary, mode)
        with patch.object(runtime, '_atomic', side_effect=interrupt), self.assertRaises(Interrupted):
            runtime.write_record(path, {'state': 'after'})
        user_bytes = runtime._record_bytes({'user': 'do not overwrite'})
        path.write_bytes(user_bytes)
        with self.assertRaisesRegex(ValueError, 'Unexpected user change during record'):
            runtime.read_record(path)
        self.assertEqual(path.read_bytes(), user_bytes)

    def test_unrelated_preparation_is_preserved_and_rejected(self):
        path = self.app / '.harness/state.json'
        runtime.write_record(path, {'state': 'before'})
        temporary = path.with_name(path.name + '.intent.tmp')
        temporary.write_bytes(b'{"unrelated":')
        with self.assertRaisesRegex(ValueError, 'Unrecognized record preparation'):
            runtime.read_record(path)
        self.assertEqual(temporary.read_bytes(), b'{"unrelated":')

    def test_snapshot_includes_ignored_and_dependency_files(self):
        self.write('.gitignore', 'node_modules/\nignored.py\n')
        self.write('ignored.py', 'value=1')
        dependency = self.write('node_modules/lib/index.js', 'export const value = 1;')
        before = runtime.snapshot(self.app)
        self.assertIn('ignored.py', before)
        self.assertIn('node_modules/lib/index.js', before)
        dependency.write_text('changed', encoding='utf-8')
        self.assertNotEqual(runtime.snapshot(self.app), before)

    def test_snapshot_rejects_hardlinks(self):
        original = self.write('original.py', 'hello')
        try:
            os.link(str(original), str(self.app / 'linked.py'))
        except OSError as exc:
            self.skipTest('Hard links unavailable: ' + str(exc))
        with self.assertRaisesRegex(ValueError, 'unlinked'):
            runtime.snapshot(self.app)

    def test_patch_add_modify_delete_and_recover_between_files(self):
        self.write('a.py', 'old')
        self.write('remove.py', 'old deletion')
        files = [{'path': 'a.py', 'content': 'new'}, {'path': 'new.py', 'content': 'added'},
                 {'path': 'remove.py', 'content': None}]
        allowed = [item['path'] for item in files]
        journal_dir = self.app / '.harness' / 'patches' / 'one'
        original = runtime._apply_entry
        def interrupt(root, entry, directory, index):
            original(root, entry, directory, index)
            if index == 0:
                raise Interrupted()
        with patch.object(runtime, '_apply_entry', side_effect=interrupt), self.assertRaises(Interrupted):
            runtime.apply_files(self.app, files, allowed, journal_dir)
        journal = runtime.read_record(journal_dir / 'patch.json')
        self.assertEqual(journal['status'], 'prepared')
        self.assertEqual((self.app / 'a.py').read_text(), 'new')
        self.assertEqual(runtime._decoded(journal['entries'][0]['before']), b'old')
        result = runtime.apply_files(self.app, files, allowed, journal_dir)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((self.app / 'new.py').read_text(), 'added')
        self.assertFalse((self.app / 'remove.py').exists())
        self.assertEqual(runtime.apply_files(self.app, files, allowed, journal_dir), result)

    def test_patch_recovery_refuses_user_target_edit(self):
        self.write('a.py', 'old')
        files = [{'path': 'a.py', 'content': 'new'}]
        directory = self.app / '.harness' / 'patch'
        with patch.object(runtime, '_apply_entry', side_effect=Interrupted), self.assertRaises(Interrupted):
            runtime.apply_files(self.app, files, ['a.py'], directory)
        self.write('a.py', 'user work')
        with self.assertRaisesRegex(ValueError, 'Unexpected user change'):
            runtime.apply_files(self.app, files, ['a.py'], directory)
        self.assertEqual((self.app / 'a.py').read_text(), 'user work')

    def test_patch_recovery_refuses_ignored_file_change(self):
        self.write('a.py', 'old')
        self.write('node_modules/lib.py', 'dependency')
        files = [{'path': 'a.py', 'content': 'new'}]
        directory = self.app / '.harness' / 'patch'
        with patch.object(runtime, '_apply_entry', side_effect=Interrupted), self.assertRaises(Interrupted):
            runtime.apply_files(self.app, files, ['a.py'], directory)
        self.write('node_modules/lib.py', 'user dependency change')
        with self.assertRaisesRegex(ValueError, 'outside the patch'):
            runtime.apply_files(self.app, files, ['a.py'], directory)
        self.assertEqual((self.app / 'a.py').read_text(), 'old')

    def test_patch_publication_rechecks_unaffected_files_after_writes(self):
        self.write('a.py', 'old')
        self.write('node_modules/lib.py', 'dependency')
        files = [{'path': 'a.py', 'content': 'new'}]
        directory = self.app / '.harness' / 'patch'
        original = runtime._apply_entry
        def concurrent_change(*args):
            original(*args)
            self.write('node_modules/lib.py', 'concurrent user change')
        with patch.object(runtime, '_apply_entry', side_effect=concurrent_change):
            with self.assertRaisesRegex(ValueError, 'outside the patch targets during publication'):
                runtime.apply_files(self.app, files, ['a.py'], directory)
        self.assertEqual(runtime.read_record(directory / 'patch.json')['status'], 'prepared')

    def test_patch_rejects_unapproved_paths_and_runtime_metadata(self):
        for path, allowed in [('other.py', ['calc.py']), ('../outside.py', ['../outside.py']),
                              ('.harness/state.json', ['.harness/state.json']), ('.git/config', ['.git/config'])]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                runtime.apply_files(self.app, [{'path': path, 'content': 'x'}], allowed,
                                    self.app / '.harness' / 'patch')

    def test_real_command_runs_once_and_validates_logs_on_replay(self):
        program = "from pathlib import Path;p=Path('counter');p.write_text(str(int(p.read_text())+1) if p.exists() else '1');print('ran')"
        first = self.command(program)
        self.assertEqual(first['exit_code'], 0)
        self.assertTrue(first['cleanup']['completed'])
        self.assertFalse(first['recovered'])
        again = self.command(program)
        self.assertTrue(again['recovered'])
        self.assertEqual((self.app / 'counter').read_text(), '1')
        Path(first['stdout_path']).write_text('forged result', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'evidence hashes'):
            self.command(program)

    def test_started_execution_without_exit_cannot_repeat(self):
        program = "from pathlib import Path;Path('counter').write_text('one')"
        self.command(program)
        self.prefix.with_suffix('.exit.json').unlink()
        self.prefix.with_suffix('.receipt.json').unlink()
        with patch.object(runtime.processes, 'start_process') as start:
            with self.assertRaisesRegex(ValueError, 'without an exit receipt'):
                self.command(program)
            start.assert_not_called()

    def test_request_change_and_expired_budget_block_new_execution(self):
        self.command('print(1)')
        with self.assertRaisesRegex(ValueError, 'request differs'):
            self.command('print(2)')
        self.prefix = self.app / '.harness' / 'checks' / 'second'
        with patch.object(runtime.problem_time, 'budget', side_effect=runtime.problem_time.DeadlineError('expired')):
            with patch.object(runtime.processes, 'start_process') as start:
                with self.assertRaisesRegex(ValueError, 'expired'):
                    self.command('print(2)')
                start.assert_not_called()

    def test_nonzero_command_records_failure_without_repeating(self):
        result = self.command('raise SystemExit(7)')
        self.assertEqual(result['exit_code'], 7)
        with patch.object(runtime.processes, 'start_process') as start:
            self.assertEqual(self.command('raise SystemExit(7)')['exit_code'], 7)
            start.assert_not_called()

    def test_agent_result_and_token_usage_are_recovered_without_model_call(self):
        config = self.fake()
        result = self.agent(config)
        self.assertEqual(result['result'], {'summary': '검토 완료'})
        self.assertEqual(result['usage'], {'input_tokens': 123, 'cached_input_tokens': 42,
                                           'output_tokens': 17, 'reported': True})
        self.assertEqual(self.agent(config), result)
        self.assertEqual((self.root / 'calls.txt').read_text(), '1')
        request = runtime.read_record(self.app / '.harness/runs/attempt-1/agent-request.json')
        self.assertEqual(request['role'], 'implementer')
        self.assertIn('read-only', request['argv'])
        self.assertEqual(request['argv'][2:5], ['--ask-for-approval', 'never', 'exec'])

    def test_agent_refuses_changed_input_or_result_evidence(self):
        config = self.fake()
        self.agent(config)
        (self.problem / 'SPEC.md').write_text('user revised problem', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Agent inputs changed'):
            self.agent(config)
        self.assertEqual((self.root / 'calls.txt').read_text(), '1')

    def test_agent_recovery_ignores_only_new_operation_lock_lease(self):
        config = self.fake()
        self.write('.harness/work/operation.lock', 'first lease')
        lock = self.problem / '.harness/work/operation.lock'
        lock.parent.mkdir(parents=True)
        lock.write_text('first problem lease', encoding='utf-8')
        result = self.agent(config)
        self.write('.harness/work/operation.lock', 'resumed lease')
        lock.write_text('resumed problem lease', encoding='utf-8')
        self.assertEqual(self.agent(config), result)
        self.assertEqual((self.root / 'calls.txt').read_text(), '1')
        self.write('.harness/controller.json', '{"unexpected": true}')
        with self.assertRaisesRegex(ValueError, 'Agent inputs changed'):
            self.agent(config)

    def test_agent_nonzero_or_invalid_result_never_repeats_same_attempt(self):
        config = self.fake(exit=9)
        with self.assertRaisesRegex(ValueError, 'no successful'):
            self.agent(config)
        with self.assertRaisesRegex(ValueError, 'no successful'):
            self.agent(config)
        self.assertEqual((self.root / 'calls.txt').read_text(), '1')

    def test_completed_agent_reply_survives_result_publication_interruption(self):
        config = self.fake()
        original = runtime.write_record
        def interrupt(path, value):
            if Path(path).name == 'agent-result.json':
                raise Interrupted()
            return original(path, value)
        with patch.object(runtime, 'write_record', side_effect=interrupt), self.assertRaises(Interrupted):
            self.agent(config)
        self.assertEqual((self.root / 'calls.txt').read_text(), '1')
        self.assertEqual(self.agent(config)['result'], {'summary': '검토 완료'})
        self.assertEqual((self.root / 'calls.txt').read_text(), '1')

    def test_invalid_agent_reply_requires_new_attempt_instead_of_repeating(self):
        config = self.fake(result={'summary': 23})
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'type differs'):
                self.agent(config)
        self.assertEqual((self.root / 'calls.txt').read_text(), '1')

    def test_agent_detects_dependency_write_even_when_gitignored(self):
        self.write('.gitignore', 'node_modules/')
        config = self.fake(mutate='node_modules/lib/index.py')
        with self.assertRaisesRegex(ValueError, 'Read-only subagent changed'):
            self.agent(config)
        exit_path = self.app / '.harness/runs/attempt-1/model.exit.json'
        outcome = json.loads(exit_path.read_text(encoding='utf-8'))
        self.assertEqual(outcome['termination_reason'], 'guard_failed')
        self.assertTrue(outcome['cleanup']['completed'])

    def test_schema_does_not_accept_bool_as_number_or_unknown_fields(self):
        with self.assertRaisesRegex(ValueError, 'type differs'):
            runtime.validate_schema(True, {'type': 'integer'})
        with self.assertRaisesRegex(ValueError, 'unknown fields'):
            runtime.validate_schema({'summary': 'ok', 'undeclared': 1}, SCHEMA)

    def test_historical_agent_verification_accepts_later_source_but_rejects_lost_log(self):
        config = self.fake()
        result = self.agent(config)
        self.write('later.py', 'approved later implementation')
        directory = self.app / '.harness/runs/attempt-1'
        self.assertEqual(runtime.verify_agent_result(directory), result)
        (directory / 'model.stdout.log').unlink()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            runtime.verify_agent_result(directory)

    def test_command_inspection_never_reexecutes_or_requires_remaining_budget(self):
        self.command('print(1)')
        with patch.object(runtime.problem_time, 'budget', side_effect=ValueError('expired')):
            with patch.object(runtime.processes, 'start_process') as start:
                self.assertEqual(runtime.verify_command(self.prefix)['exit_code'], 0)
                start.assert_not_called()
        self.prefix.with_suffix('.receipt.json').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing historical'):
            runtime.verify_command(self.prefix)

    def test_partial_usage_preserves_unknown_fields(self):
        path = self.root / 'usage.log'
        path.write_text(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 12}}), encoding='utf-8')
        self.assertEqual(runtime.read_usage(path), {'input_tokens': 12, 'cached_input_tokens': None,
                                                  'output_tokens': None, 'reported': True})


if __name__ == '__main__':
    unittest.main()
