"""Plan rejection consumes durable attempts; step clocks span internal phases."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


loader = importlib.util.spec_from_file_location('plan_error_controller', Path(__file__).resolve().parents[1] / 'development.py')
development = importlib.util.module_from_spec(loader)
loader.loader.exec_module(development)


def plan():
    check = dict(id='test', argv=['python', '-m', 'unittest'], cwd='.', timeout_seconds=30,
                 test_framework='unittest', min_tests=1, requirement='R-1', oracle='원본의 독립 표본')
    return dict(summary='확정 명세 구현', architecture=dict(framework='stdlib', ports=[], storage='없음', algorithm='합산'),
                decisions=[dict(decision='표준 라이브러리', reason='기능에 충분함', discarded=['외부 프레임워크'])],
                tasks=[dict(id='T1', title='합계', requirements=['R-1'], depends_on=[], files=['src/calc.py'],
                            test_files=['tests/test_calc.py'], checks=[check], expected_minutes=10)],
                docker=dict(dockerfile='Dockerfile', context='.', command=[], checks=[check],
                            container_port=0, host_port=0, compose_file=None, service=None),
                docker_files=['Dockerfile', '.dockerignore'], run_commands=[['python', 'src/calc.py']],
                limitations=[], stage_minutes=dict(plan=10, implementation=60, docker=20),
                assurance=development.assurance.defaults())


REVIEW = dict(approved=True, findings=[], decision=None, verified_claims=[], limitations=[])


class ControllerPlanErrorsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.problem, self.app = self.base / 'problem', self.base / 'app'
        self.problem.mkdir()
        for name in ('CONFIRMED_REQUIREMENTS.md', 'FUNCTIONAL_SPEC.md'):
            (self.problem / name).write_text('확정 문서\n', encoding='utf-8')
        self.bound = {'clock': {'received_at': 0}}
        patcher = mock.patch.object(development, 'handoff', return_value=self.bound)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(development.clock, 'status', return_value={'remaining_minutes': 100, 'received_at': 0, 'limit_seconds': 9000})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controller = self.new_controller()

    def new_controller(self):
        instance = development.Controller(self.problem, self.app, {}, max_calls=20, max_plan_attempts=10)
        instance.deadline = lambda: 60
        instance.state['preflight'] = {'status': 'PASS'}
        instance.publish = lambda: None
        return instance

    def test_semantically_invalid_plan_is_retired_then_retried_with_new_key(self):
        invalid = plan()
        invalid['tasks'] = []
        coordinator_calls = []
        def execute(role, prompt, schema, *args, **kwargs):
            if role == 'coordinator':
                coordinator_calls.append(prompt)
                result = invalid if len(coordinator_calls) == 1 else plan()
            else:
                result = REVIEW
            return {'result': copy.deepcopy(result), 'usage': None, 'run_id': 'run-' + str(len(coordinator_calls))}
        with mock.patch.object(development.runtime, 'run_agent', side_effect=execute):
            self.assertEqual(self.controller.cycle(), development.WAITING)
        self.assertEqual(len(coordinator_calls), 2)
        self.assertEqual(self.controller.state['plan_attempts'], 2)
        self.assertEqual(self.controller.state['gate']['kind'], 'PLAN')
        self.assertIn('1-plan-0', self.controller.state['responses'])
        self.assertIn('1-plan-1', self.controller.state['responses'])
        self.assertIn('Plan needs tasks', coordinator_calls[-1])

    def test_three_invalid_plans_gate_and_resume_never_replays_old_cached_result(self):
        invalid = plan()
        invalid['tasks'] = []
        coordinator_calls = []
        def execute(role, prompt, schema, *args, **kwargs):
            if role == 'coordinator':
                coordinator_calls.append(prompt)
                result = invalid if len(coordinator_calls) <= 3 else plan()
            else:
                result = REVIEW
            return {'result': copy.deepcopy(result), 'usage': None, 'run_id': 'run-' + str(len(coordinator_calls))}
        with mock.patch.object(development.runtime, 'run_agent', side_effect=execute):
            self.assertEqual(self.controller.cycle(), development.WAITING)
            self.assertEqual(len(coordinator_calls), 3)
            self.assertEqual(self.controller.state['gate']['kind'], 'RETRY')
            resumed = self.new_controller()
            self.assertEqual(resumed.state['plan_round'], 3)
            resumed.approve(resumed.state['gate']['token'], '진행해')
            self.assertEqual(resumed.cycle(), development.WAITING)
        self.assertEqual(len(coordinator_calls), 4)
        self.assertEqual(resumed.state['plan_attempts'], 4)
        self.assertEqual(resumed.state['gate']['kind'], 'PLAN')

    def test_unknown_execution_is_not_retired_as_invalid_plan(self):
        with mock.patch.object(development.runtime, 'run_agent', side_effect=ValueError('Execution started without an exit receipt')):
            with self.assertRaisesRegex(ValueError, 'without an exit receipt'):
                self.controller.cycle()
        self.assertEqual(self.controller.state['plan_round'], 0)
        self.assertEqual(self.controller.state['failures'], {})
        self.assertIsNotNone(self.controller.state['active_call'])

    def test_gate_elapsed_spans_plan_review_and_all_implementation_phases(self):
        controller = self.controller
        controller.state.update(phase='PLAN', phase_started=10, active_stage='plan', stage_started={'plan': 10})
        with mock.patch.object(development.time, 'time', return_value=40):
            controller.transition('PLAN_REVIEW')
        with mock.patch.object(development.time, 'time', return_value=100):
            controller.gate('PLAN', 'RED', '계획 승인')
        self.assertEqual(controller.state['gate']['phase_elapsed_seconds'], 90)
        with mock.patch.object(development.time, 'time', return_value=120):
            controller.approve(controller.state['gate']['token'], 'OK')
        with mock.patch.object(development.time, 'time', return_value=150):
            controller.transition('IMPLEMENT')
        with mock.patch.object(development.time, 'time', return_value=200):
            controller.transition('REVIEW')
        with mock.patch.object(development.time, 'time', return_value=240):
            controller.gate('IMPLEMENTATION', 'DOCKER', '구현 승인')
        self.assertEqual(controller.state['stage_started']['implementation'], 120)
        self.assertEqual(controller.state['gate']['phase_elapsed_seconds'], 120)


if __name__ == '__main__':
    unittest.main()
