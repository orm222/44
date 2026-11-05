"""Controller crash boundaries use real offline child processes and signed state."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_development_runtime


BASE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('development_recovery_tests', BASE / 'development.py')
development = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(development)
runtime = development.runtime


class Interrupted(BaseException):
    pass


class DevelopmentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='development recovery ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.problem = self.root / 'problem-project'
        self.app = self.root / 'app-project'
        self.problem.mkdir()
        self.app.mkdir()
        for name in ('SPEC.md', 'CONFIRMED_REQUIREMENTS.md', 'FUNCTIONAL_SPEC.md'):
            (self.problem / name).write_text('# 실제 확인 문서\n', encoding='utf-8')
        development.clock.begin(self.problem)
        self.binding = {'requirements': {'revision': 1}, 'functional': {'revision': 1},
                        'clock': development.clock.load(self.problem, required=True)}
        self.handoff_patch = patch.object(development, 'handoff', return_value=self.binding)
        self.handoff_patch.start()
        self.addCleanup(self.handoff_patch.stop)
        self.fake = self.root / 'fake.py'
        self.fake.write_text(test_development_runtime.FAKE, encoding='utf-8')
        self.config = {'model': 'offline-fixture', 'codex_command': [sys.executable, str(self.fake)]}
        self.scenario()

    def scenario(self, **values):
        (self.root / 'scenario.json').write_text(json.dumps(values), encoding='utf-8')

    def controller(self, **options):
        return development.Controller(self.problem, self.app, self.config, timeout=10, **options)

    def call(self, controller, role='implementer'):
        return controller.agent('one', role, '문서를 확인하세요.', test_development_runtime.SCHEMA)

    def count(self):
        path = self.root / 'calls.txt'
        return int(path.read_text()) if path.exists() else 0

    def test_successful_worker_recovers_when_controller_result_save_is_interrupted(self):
        controller = self.controller()
        original = controller.save
        def interrupt():
            if controller.state['responses']:
                raise Interrupted()
            original()
        with patch.object(controller, 'save', side_effect=interrupt), self.assertRaises(Interrupted):
            self.call(controller)
        self.assertEqual(self.count(), 1)
        resumed = self.controller()
        self.assertIsNotNone(resumed.state['active_call'])
        self.assertEqual(self.call(resumed), {'summary': '검토 완료'})
        self.assertEqual(self.count(), 1)
        self.assertEqual(resumed.state['calls'], 1)
        self.assertIsNone(resumed.state['active_call'])

    def test_successful_worker_survives_partial_controller_state_publication(self):
        controller = self.controller()
        original = runtime._atomic
        def interrupt(target, data, temporary=None, mode=None):
            if Path(target) == controller.path and json.loads(data.decode('utf-8'))['responses']:
                Path(target).with_name(Path(target).name + '.tmp').write_bytes(data[:31])
                raise Interrupted()
            return original(target, data, temporary, mode)
        with patch.object(runtime, '_atomic', side_effect=interrupt), self.assertRaises(Interrupted):
            self.call(controller)
        self.assertEqual(self.count(), 1)
        resumed = self.controller()
        self.assertIsNone(resumed.state['active_call'])
        self.assertEqual(self.call(resumed), {'summary': '검토 완료'})
        self.assertEqual(self.count(), 1)
        self.assertEqual(resumed.state['calls'], 1)

    def test_successful_worker_survives_incomplete_controller_intent_preparation(self):
        controller = self.controller()
        original = runtime._atomic
        def interrupt(target, data, temporary=None, mode=None):
            if Path(target).name == controller.path.name + '.intent':
                intent = json.loads(data.decode('utf-8'))
                saved = json.loads(runtime._decoded(intent['after']).decode('utf-8'))
                if saved['responses']:
                    Path(target).with_name(Path(target).name + '.tmp').write_bytes(data[:31])
                    raise Interrupted()
            return original(target, data, temporary, mode)
        with patch.object(runtime, '_atomic', side_effect=interrupt), self.assertRaises(Interrupted):
            self.call(controller)
        self.assertEqual(self.count(), 1)
        resumed = self.controller()
        self.assertIsNotNone(resumed.state['active_call'])
        self.assertEqual(self.call(resumed), {'summary': '검토 완료'})
        self.assertEqual(self.count(), 1)

    def test_failed_plan_call_counts_before_attempt_and_cannot_repeat_over_budget(self):
        self.scenario(exit=7)
        controller = self.controller(max_calls=10, max_plan_attempts=1)
        with self.assertRaises(ValueError):
            self.call(controller, role='coordinator')
        self.assertEqual(self.count(), 1)
        for _ in range(2):
            resumed = self.controller(max_calls=99, max_plan_attempts=99)
            self.assertEqual(resumed.state['plan_attempts'], 1)
            self.assertEqual(resumed.state['max_plan_attempts'], 1)
            self.assertEqual(resumed.state['handoff']['clock'], self.binding['clock'])
            with self.assertRaisesRegex(ValueError, '계획 호출 시도 예산'):
                self.call(resumed, role='coordinator')
        self.assertEqual(self.count(), 1)

    def test_unknown_exit_keeps_active_call_and_does_not_launch_again(self):
        controller = self.controller(max_calls=1)
        original = controller.save
        def interrupt():
            if controller.state['responses']:
                raise Interrupted()
            original()
        with patch.object(controller, 'save', side_effect=interrupt), self.assertRaises(Interrupted):
            self.call(controller)
        resumed = self.controller()
        run_id = resumed.state['active_call']['id']
        directory = resumed.work / 'agents' / run_id
        (directory / 'model.exit.json').unlink()
        with self.assertRaisesRegex(ValueError, 'without an exit receipt'):
            self.call(resumed)
        again = self.controller()
        self.assertIsNotNone(again.state['active_call'])
        self.assertEqual(again.state['calls'], 1)
        self.assertEqual(self.count(), 1)

    def test_cached_agent_result_refuses_removed_evidence(self):
        controller = self.controller()
        self.call(controller)
        run_id = controller.state['responses']['1-one']['run_id']
        (controller.work / 'agents' / run_id / 'model.stdout.log').unlink()
        with self.assertRaises((ValueError, OSError)):
            self.call(self.controller())
        self.assertEqual(self.count(), 1)

    def test_mutation_replay_after_publication_before_phase_transition_is_idempotent(self):
        (self.app / 'calc.py').write_text('old', encoding='utf-8')
        controller = self.controller()
        files = [{'path': 'calc.py', 'content': 'new'}]
        controller.mutate(files, ['calc.py'], 'first')
        resumed = self.controller()
        self.assertIsNone(resumed.state['mutation'])
        resumed.mutate(files, ['calc.py'], 'first')
        self.assertEqual((self.app / 'calc.py').read_text(), 'new')
        self.assertIsNone(resumed.state['mutation'])

    def test_mutation_pending_before_journal_does_not_adopt_unexpected_ignored_edit(self):
        (self.app / 'calc.py').write_text('old', encoding='utf-8')
        dependency = self.app / 'node_modules' / 'lib.py'
        dependency.parent.mkdir()
        dependency.write_text('old dependency', encoding='utf-8')
        controller = self.controller()
        files = [{'path': 'calc.py', 'content': 'new'}]
        with patch.object(runtime, 'apply_files', side_effect=Interrupted), self.assertRaises(Interrupted):
            controller.mutate(files, ['calc.py'], 'first')
        dependency.write_text('user changed dependency', encoding='utf-8')
        resumed = self.controller()
        with self.assertRaisesRegex(ValueError, 'baseline'):
            resumed.recover_mutation()
        self.assertEqual((self.app / 'calc.py').read_text(), 'old')
        self.assertEqual(dependency.read_text(), 'user changed dependency')

    def test_mutation_resumes_after_first_of_two_files_without_losing_originals(self):
        (self.app / 'a.py').write_text('a before', encoding='utf-8')
        (self.app / 'b.py').write_text('b before', encoding='utf-8')
        controller = self.controller()
        files = [{'path': 'a.py', 'content': 'a after'}, {'path': 'b.py', 'content': 'b after'}]
        original = runtime._apply_entry
        def interrupt(root, entry, directory, index):
            original(root, entry, directory, index)
            if index == 0:
                raise Interrupted()
        with patch.object(runtime, '_apply_entry', side_effect=interrupt), self.assertRaises(Interrupted):
            controller.mutate(files, ['a.py', 'b.py'], 'first')
        resumed = self.controller()
        resumed.recover_mutation()
        self.assertEqual((self.app / 'a.py').read_text(), 'a after')
        self.assertEqual((self.app / 'b.py').read_text(), 'b after')
        self.assertIsNone(resumed.state['mutation'])
        self.assertEqual(resumed.state['source'], runtime.snapshot(self.app))

    def test_stale_gate_token_or_ignored_edit_cannot_approve(self):
        controller = self.controller()
        controller.state['gate'] = {'token': 'PLAN-known', 'kind': 'PLAN', 'next_phase': 'RED',
            'source': copy.deepcopy(controller.state['source']), 'handoff': copy.deepcopy(self.binding),
            'plan': None, 'detail': 'reviewed plan'}
        controller.state['phase'] = 'GATE'
        controller.save()
        with self.assertRaisesRegex(ValueError, '현재 GATE'):
            controller.approve('PLAN-stale', 'OK')
        dependency = self.app / 'node_modules' / 'lib.py'
        dependency.parent.mkdir()
        dependency.write_text('unexpected', encoding='utf-8')
        result = development.main(['--root', str(self.problem), '--app-root', str(self.app),
                                   '--approve', 'PLAN-known', '--response', 'OK'])
        self.assertEqual(result, 2)
        self.assertEqual(self.controller().state['phase'], 'GATE')
        self.assertFalse(self.controller().state['approvals'])

    def test_invalid_generated_flow_rolls_back_and_stops_after_three_attempts(self):
        (self.app / 'calc.py').write_text('def calc():\n    return 1\n', encoding='utf-8')
        original_bytes = (self.app / 'calc.py').read_bytes()
        self.scenario(result={'summary': '잘못된 흐름 설명', 'files': [
            {'path': 'calc.py', 'content': 'def calc():\n    return 2\n'}],
            'flows': [{'id': 'F1', 'feature': '계산', 'entry': 'CLI',
                       'chain': [{'file': 'calc.py', 'function': 'missing_function'}],
                       'reads': [], 'writes': []}], 'decision': None})
        controller = self.controller()
        controller.state['plan'] = {'summary': '계산 구현',
            'architecture': {'framework': 'stdlib', 'ports': [], 'storage': '없음', 'algorithm': '계산'},
            'decisions': [], 'tasks': [{'id': 'T1', 'title': '계산', 'requirements': ['R1'],
                'depends_on': [], 'files': ['calc.py'], 'test_files': ['tests/test_calc.py'],
                'checks': [], 'expected_minutes': 5}], 'docker_files': [], 'docker': {},
            'run_commands': [], 'limitations': [], 'stage_minutes': {'plan': 5, 'implementation': 5, 'docker': 5}}
        controller.transition('IMPLEMENT')
        self.assertEqual(controller.cycle(), development.WAITING)
        self.assertEqual(self.count(), 3)
        self.assertEqual(controller.state['failures']['T1'], 3)
        self.assertEqual(controller.state['tasks']['T1']['attempt'], 3)
        self.assertEqual(controller.state['gate']['kind'], 'RETRY')
        self.assertEqual((self.app / 'calc.py').read_bytes(), original_bytes)
        self.assertEqual(self.controller().cycle(), development.WAITING)
        self.assertEqual(self.count(), 3)

    def test_human_explanation_allows_escaped_pipe_but_protects_code_columns(self):
        document = self.app / 'doc/UNDERSTANDING.md'
        document.parent.mkdir()
        original = '| 계산 <!-- flow:F1 --> | CLI | calc.py:calc | 없음 | |\n'
        document.write_bytes(original.encode('utf-8'))
        controller = self.controller()
        controller.state['understanding_text'] = original
        controller.state['understanding_checked'] = 'old-review'
        controller.save()
        document.write_bytes(original.replace('없음 | |', '없음 | A \\| B를 계산한다. |').encode('utf-8'))
        controller.verify_source()
        self.assertIsNone(controller.state['understanding_checked'])
        self.assertEqual(development.reports.read_explanations(self.app)['F1'], 'A | B를 계산한다.')
        document.write_bytes(document.read_bytes().replace(b'calc.py:calc', b'other.py:other'))
        with self.assertRaisesRegex(ValueError, '사용자 파일 변경'):
            controller.verify_source()


if __name__ == '__main__':
    unittest.main()
