"""Controller ordering/recovery with simulated readiness, agents and test runners."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_controller_plan_errors as fixtures


loader = importlib.util.spec_from_file_location('refinement_controller', Path(__file__).resolve().parents[1] / 'development.py')
development = importlib.util.module_from_spec(loader)
loader.loader.exec_module(development)


class ControllerRefinementsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='controller-refinements-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.problem, self.app = self.root / 'problem', self.root / 'app'
        self.problem.mkdir()
        (self.problem / 'config.json').write_text('{"problem_time_limit_seconds":9600}', encoding='utf-8')
        for name in ('CONFIRMED_REQUIREMENTS.md', 'FUNCTIONAL_SPEC.md'):
            (self.problem / name).write_text('R-1: 두 수를 더한다.\n', encoding='utf-8')
        self.receipt = development.clock.begin(self.problem)
        self.handoff = {'clock': self.receipt, 'requirements': {'revision': 1}, 'functional': {'revision': 1}}
        self.plan = fixtures.plan()
        self.ready = False
        self.unknown_execution = False
        self.agent_calls, self.readiness_calls, self.verified_readiness = [], [], []
        self.agent_results, self.readiness_results = {}, {}
        patches = [
            mock.patch.object(development, 'handoff', side_effect=lambda root: copy.deepcopy(self.handoff)),
            mock.patch.object(development.runtime, 'run_agent', side_effect=self.agent),
            mock.patch.object(development.runtime, 'verify_agent_result', side_effect=self.verify_agent),
            mock.patch.object(development.checks, 'preflight', return_value={'status': 'BLOCKED', 'simulation': True}),
            mock.patch.object(development.assurance, 'environment', return_value={'status': 'DISCOVERED', 'simulation': True}),
            mock.patch.object(development.assurance.data, 'inspect_joins', return_value={'status': 'NOT_APPLICABLE', 'passed': True}),
            mock.patch.object(development.checks, 'readiness', side_effect=self.readiness, create=True),
            mock.patch.object(development.checks, 'verify_readiness', side_effect=self.verify_readiness, create=True),
            mock.patch.object(development.checks, 'run_checks', return_value={'passed': True, 'status': 'PASS', 'checks': [], 'simulation': True}),
            mock.patch.object(development.assurance, 'run', return_value={'passed': True, 'status': 'NOT_APPLICABLE', 'reports': []}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reopen()

    def reopen(self):
        self.controller = development.Controller(self.problem, self.app, {'model': 'offline-test'})
        self.controller.recover_mutation()
        self.controller.verify_source()
        self.controller.publish = lambda: None
        return self.controller

    def agent(self, role, prompt, schema, app, problem, directory, config, timeout=600):
        phase = self.controller.state['phase']
        self.agent_calls.append((role, phase))
        if schema == development.contracts.PLAN:
            result = self.plan
        elif schema == development.contracts.REVIEW:
            result = fixtures.REVIEW
        elif phase == 'RED':
            result = {'summary': '모의 테스트 작성', 'files': [{'path': 'tests/test_calc.py', 'content': '# simulated contract\n'}],
                      'flows': [], 'decision': None}
        elif phase == 'IMPLEMENT':
            result = {'summary': '모의 구현 응답', 'files': [{'path': 'src/calc.py', 'content': 'def add(a, b):\n    return a + b\n'}],
                      'flows': [{'id': 'add', 'feature': '합산', 'entry': 'add(a, b)',
                                 'chain': [{'file': 'src/calc.py', 'function': 'add'}], 'reads': ['입력'], 'writes': []}],
                      'decision': None}
        else:
            self.fail('Unexpected simulated phase: ' + phase)
        report = {'result': copy.deepcopy(result), 'run_id': Path(directory).name, 'usage': None}
        self.agent_results[report['run_id']] = copy.deepcopy(report)
        return report

    def verify_agent(self, directory):
        return copy.deepcopy(self.agent_results[Path(directory).name])

    def readiness(self, app, problem, plan, directory, guard=None):
        if guard:
            guard()
        directory = str(directory)
        self.readiness_calls.append(directory)
        if self.unknown_execution:
            raise ValueError('Execution started without an exit receipt')
        report = {'passed': self.ready, 'status': 'PASS' if self.ready else 'BLOCKED',
                  'reason': 'simulated prerequisites ready' if self.ready else 'simulated missing Docker engine',
                  'run_dir': directory, 'simulation': True}
        self.readiness_results[directory] = copy.deepcopy(report)
        return report

    def verify_readiness(self, report):
        self.verified_readiness.append(report['run_dir'])
        if report != self.readiness_results[report['run_dir']]:
            raise ValueError('Readiness evidence changed')
        return report

    def plan_gate(self):
        self.assertEqual(self.controller.cycle(), development.WAITING)
        self.assertEqual(self.controller.state['gate']['kind'], 'PLAN')

    def approve_plan(self):
        self.controller.approve(self.controller.state['gate']['token'], 'OK')
        self.assertEqual(self.controller.state['phase'], 'RED')

    def test_model_context_preserves_csv_error_stage_legacy_reason_and_zero_counts(self):
        self.controller.state['assurance_runs'] = {'latest': {'reports': [{
            'kind': 'reconciliation', 'status': 'BLOCKED', 'id': 'rows', 'task_id': 'T1',
            'details': {'error': 'missing data/input.csv', 'counts': {'contracts': 0}, 'checks': [
                {'id': 'input', 'status': 'BLOCKED', 'stage': 'input', 'error': 'missing data/input.csv', 'counts': {'input_rows': 0}},
                {'id': 'artifact', 'status': 'FAIL', 'stage': 'artifact', 'error': 'missing used.csv', 'counts': {'output_rows': 0}},
                {'id': 'old', 'status': 'FAIL', 'stage': 'artifact', 'reason': 'legacy explanation', 'error': 'fallback only'}]}}]}}
        self.controller.state['last_assurance_key'] = 'latest'
        summary = self.controller.context()['assurance_summary'][0]
        self.assertEqual(summary['reason'], 'missing data/input.csv')
        self.assertEqual(summary['counts'], {'contracts': 0})
        self.assertEqual(summary['checks'][0]['reason'], 'missing data/input.csv')
        self.assertEqual(summary['checks'][0]['stage'], 'input')
        self.assertEqual(summary['checks'][0]['counts']['input_rows'], 0)
        self.assertEqual(summary['checks'][1]['reason'], 'missing used.csv')
        self.assertEqual(summary['checks'][1]['stage'], 'artifact')
        self.assertEqual(summary['checks'][1]['counts']['output_rows'], 0)
        self.assertEqual(summary['checks'][2]['reason'], 'legacy explanation')

    def test_plan_approval_precedes_readiness_and_implementation(self):
        self.ready = True
        self.plan_gate()
        calls = self.controller.state['calls']
        self.assertEqual(self.controller.cycle(), development.WAITING)
        self.assertEqual(self.readiness_calls, [])
        self.assertEqual(self.controller.state['calls'], calls)
        self.assertFalse(any(role == 'implementer' for role, phase in self.agent_calls))

    def test_blocked_readiness_keeps_red_and_calls_then_reopens_after_environment_ready(self):
        self.plan_gate()
        self.approve_plan()
        calls = self.controller.state['calls']
        self.assertEqual(self.controller.cycle(), 2)
        self.assertEqual(self.controller.state['phase'], 'RED')
        self.assertEqual(self.controller.state['calls'], calls)
        self.assertEqual(self.controller.state['tasks'], {})
        self.assertEqual(len(self.controller.state['readiness_reports']), 1)
        self.reopen()
        self.ready = True
        self.assertEqual(self.controller.cycle(), development.WAITING)
        self.assertEqual(self.controller.state['gate']['kind'], 'IMPLEMENTATION')
        self.assertIn(('implementer', 'IMPLEMENT'), self.agent_calls)
        self.assertEqual([Path(path).name for path in self.readiness_calls], ['1', '2'])
        self.assertEqual(self.controller.state['approvals'][0]['kind'], 'PLAN')
        self.assertEqual(development.clock.load(self.problem), self.receipt)

    def test_unknown_readiness_reuses_saved_directory_and_does_not_start_new_attempt(self):
        self.plan_gate()
        self.approve_plan()
        calls = self.controller.state['calls']
        self.unknown_execution = True
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'without an exit receipt'):
                self.controller.cycle()
            self.assertEqual(self.controller.state['active_readiness']['number'], 1)
            self.assertEqual(self.controller.state['calls'], calls)
            self.assertEqual(self.controller.state.get('readiness_reports', []), [])
            self.reopen()
        self.unknown_execution, self.ready = False, True
        self.assertEqual(self.controller.cycle(), development.WAITING)
        self.assertEqual(len(set(self.readiness_calls)), 1)
        self.assertEqual(Path(self.readiness_calls[0]).name, '1')
        self.assertIsNone(self.controller.state['active_readiness'])

    def test_pass_receipt_is_verified_and_reused_without_fresh_environment_execution(self):
        self.plan_gate()
        self.approve_plan()
        self.ready = True
        self.assertTrue(self.controller.ensure_readiness())
        self.reopen()
        verified_before = len(self.verified_readiness)
        self.assertTrue(self.controller.ensure_readiness())
        self.assertEqual(len(self.readiness_calls), 1)
        self.assertEqual(len(self.verified_readiness), verified_before + 1)
        self.assertEqual(self.controller.state['phase'], 'RED')

    def test_expired_clock_blocks_resume_even_when_readiness_passed(self):
        self.plan_gate()
        self.approve_plan()
        self.ready = True
        self.assertTrue(self.controller.ensure_readiness())
        calls = self.controller.state['calls']
        self.reopen()
        with mock.patch.object(development.clock, '_now', return_value=self.receipt['deadline_at']), \
                self.assertRaisesRegex(development.clock.DeadlineError, 'expired'):
            self.controller.cycle()
        self.assertEqual(self.controller.state['calls'], calls)
        self.assertEqual(len(self.readiness_calls), 1)
        self.assertEqual(self.controller.state['phase'], 'RED')

    def test_changed_pass_evidence_blocks_reuse_without_another_attempt(self):
        self.plan_gate()
        self.approve_plan()
        self.ready = True
        self.assertTrue(self.controller.ensure_readiness())
        self.controller.state['readiness_reports'][-1]['report']['reason'] = 'edited evidence'
        self.controller.save()
        self.reopen()
        with self.assertRaisesRegex(ValueError, 'Readiness evidence changed'):
            self.controller.ensure_readiness()
        self.assertEqual(len(self.readiness_calls), 1)
        self.assertIsNone(self.controller.state['active_readiness'])


if __name__ == '__main__':
    unittest.main()
