"""Controller integration: simulated agents/Docker, real RED/GREEN processes.

These tests do not claim that a live model or Docker engine was exercised.
"""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('test_development_integrated', SOURCE / 'development.py')
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


class DevelopmentIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='development-integration-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.problem, self.app = self.root / 'problem-project', self.root / 'app-project'
        self.problem.mkdir()
        (self.problem / 'config.json').write_text(json.dumps({'model': 'simulated-test-model',
            'problem_time_limit_seconds': 9000}), encoding='utf-8')
        (self.problem / 'CONFIRMED_REQUIREMENTS.md').write_text('# Requirements\nR-01: add two numbers.\n', encoding='utf-8')
        (self.problem / 'FUNCTIONAL_SPEC.md').write_text('# Functional specification\nadd(2, 3) returns 5.\n', encoding='utf-8')
        receipt = dev.clock.begin(self.problem)
        self.handoff = {'clock': receipt, 'requirements': {'revision': 1, 'document_sha256': 'test-req'},
                        'functional': {'revision': 1, 'document_sha256': 'test-functional'}}
        self.agent_calls, self.bad_implementation, self.direction_requested = [], False, False
        self.model_results = {}
        self.mutate_during_test = False
        self.docker_calls, self.docker_blocked = 0, False
        self.plan = self.make_plan()
        self.verify_real_report = dev.checks.verify_report
        self.verify_real_agent = dev.runtime.verify_agent_result
        self.patches = [mock.patch.object(dev, 'handoff', side_effect=lambda root: copy.deepcopy(self.handoff)),
            mock.patch.object(dev.runtime, 'run_agent', side_effect=self.agent),
            mock.patch.object(dev.runtime, 'verify_agent_result', side_effect=self.verify_agent),
            mock.patch.object(dev.checks, 'verify_report', side_effect=self.verify_report),
            mock.patch.object(dev.checks, 'preflight', return_value={
                'passed': False, 'status': 'BLOCKED', 'simulation': True, 'reason': 'Docker is simulated in this test'}),
            mock.patch.object(dev.checks, 'readiness', return_value={
                'passed': True, 'status': 'PASS', 'kind': 'readiness', 'simulation': True,
                'reason': 'Simulated environment readiness; no engine or browser was exercised'}),
            mock.patch.object(dev.checks, 'verify_readiness', side_effect=self.verify_readiness),
            mock.patch.object(dev.checks, 'run_docker', side_effect=self.docker)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.controller = self.reopen()

    def verify_agent(self, directory):
        # Model calls are deliberately simulated; subprocess test proof is real.
        return copy.deepcopy(self.model_results[Path(directory).name])

    def verify_readiness(self, report):
        self.assertTrue(report.get('simulation'))
        self.assertEqual(report.get('reason'), 'Simulated environment readiness; no engine or browser was exercised')
        return report

    def verify_report(self, report):
        if report.get('simulation'):
            self.assertEqual(report.get('reason'), 'Simulated Docker driver; no image/engine was exercised')
            return True
        return self.verify_real_report(report)

    def contract(self, identifier='unit-add', framework='unittest', argv=None):
        return dict(id=identifier, argv=argv or [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'],
            cwd='.', timeout_seconds=30, test_framework=framework, min_tests=1,
            requirement='R-01', oracle='The confirmed specification supplies the independent example 2 + 3 = 5')

    def make_plan(self):
        return dict(summary='Implement the confirmed addition behavior',
            architecture={'framework': 'Python standard library', 'ports': [], 'storage': 'none', 'algorithm': 'addition'},
            decisions=[{'decision': 'Use the standard library', 'reason': 'No framework needed for a small function', 'discarded': ['Web server']}],
            tasks=[{'id': 'ADD', 'title': 'Implement addition', 'requirements': ['R-01'], 'depends_on': [],
                'files': ['calc.py'], 'test_files': ['tests/test_calc.py'], 'checks': [self.contract()], 'expected_minutes': 5}],
            docker={'dockerfile': 'Dockerfile', 'context': '.', 'command': ['python', '-c', 'import time;time.sleep(300)'],
                'checks': [self.contract('container-add', 'command', ['python', 'check_container.py'])],
                'container_port': 0, 'host_port': 0, 'compose_file': None, 'service': None},
            docker_files=['Dockerfile', '.dockerignore', 'check_container.py'],
            run_commands=[[sys.executable, '-m', 'unittest', 'discover', '-s', 'tests']], limitations=[],
            stage_minutes={'plan': 5, 'implementation': 10, 'docker': 5}, assurance=dev.assurance.defaults())

    def reopen(self):
        self.controller = dev.Controller(self.problem, self.app, {'model': 'simulated-test-model'})
        self.controller.recover_mutation()
        self.controller.verify_source()
        return self.controller

    def patch(self, files, flows=None, decision=None):
        return {'summary': 'Simulated model response; real code and tests are checked by Python',
                'files': [{'path': path, 'content': content} for path, content in files.items()],
                'flows': flows or [], 'decision': decision}

    def agent(self, role, prompt, schema, app_root, problem_root, run_dir, config, timeout=600):
        phase = self.controller.state['phase']
        self.agent_calls.append((role, phase))
        if schema == dev.contracts.PLAN:
            self.assertEqual(role, 'coordinator')
            result = copy.deepcopy(self.plan)
        elif schema == dev.contracts.REVIEW:
            self.assertEqual(role, 'reviewer')
            result = {'approved': True, 'findings': [], 'decision': None,
                'verified_claims': [{'claim': 'Fixture review only', 'method': 'Simulated reviewer for controller integration',
                                     'result': 'Live reviewer behavior is not established by this test'}],
                'limitations': ['Integration test uses simulated model and Docker responses']}
        elif phase == 'RED':
            self.assertEqual(role, 'implementer')
            source = '''import importlib.util
from pathlib import Path
import unittest

class AdditionTests(unittest.TestCase):
    def test_confirmed_sample(self):
        filename = Path(__file__).resolve().parents[1] / 'calc.py'
        self.assertTrue(filename.is_file(), 'R-01 requires an implementation file')
        spec = importlib.util.spec_from_file_location('calc_under_test', filename)
        calc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(calc)
        # Independent expected value supplied in the confirmed specification.
        self.assertEqual(calc.add(2, 3), 5)
'''
            if self.mutate_during_test:
                source = source.replace("        filename = Path(__file__)",
                    "        target = Path('node_modules/hidden.txt')\n"
                    "        target.parent.mkdir(exist_ok=True)\n"
                    "        target.write_text('unauthorized test mutation')\n"
                    "        filename = Path(__file__)")
            result = self.patch({'tests/test_calc.py': source})
        elif phase == 'IMPLEMENT':
            self.assertEqual(role, 'implementer')
            if self.direction_requested:
                result = self.patch({}, decision={'before': 'addition', 'after': 'new algorithm',
                    'reason': 'Fixture direction change', 'discarded': ['Keep existing'], 'expected_minutes': 5})
            else:
                expression = 'a - b' if self.bad_implementation else 'a + b'
                result = self.patch({'calc.py': 'def add(a, b):\n    return ' + expression + '\n'},
                    flows=[{'id': 'addition', 'feature': 'Add numbers', 'entry': 'calc.add(2, 3)',
                        'chain': [{'file': 'calc.py', 'function': 'add'}], 'reads': ['two input numbers'], 'writes': []}])
        elif phase == 'DOCKER':
            self.assertEqual(role, 'implementer')
            result = self.patch({'Dockerfile': 'FROM python:3.9-slim\nWORKDIR /app\nCOPY . .\n',
                '.dockerignore': '.git\n.harness\n', 'check_container.py':
                'from calc import add\nassert add(2,3) == 5\nprint(\'HARNESS_TEST_RESULT {"tests":1,"failures":0,"errors":0,"skipped":0}\')\n'})
        else:
            self.fail('Unexpected simulated agent phase: ' + phase)
        returned = {'result': result, 'run_id': Path(run_dir).name, 'usage': {'input_tokens': 0, 'output_tokens': 0}}
        self.model_results[Path(run_dir).name] = copy.deepcopy(returned)
        return returned

    def docker(self, app_root, problem_root, config, run_dir, guard=None):
        if guard:
            guard()
        self.docker_calls += 1
        return {'passed': not self.docker_blocked, 'status': 'BLOCKED' if self.docker_blocked else 'PASS',
                'simulation': True, 'reason': 'Simulated Docker driver; no image/engine was exercised',
                'stages': [], 'source_before': dev.runtime.snapshot(app_root), 'source_after': dev.runtime.snapshot(app_root)}

    def approve(self):
        token = self.controller.state['gate']['token']
        self.controller.approve(token, 'OK')

    def until_implementation_gate(self):
        self.assertEqual(self.controller.cycle(), dev.WAITING)
        self.assertEqual(self.controller.state['gate']['kind'], 'PLAN')
        self.approve()
        self.assertEqual(self.controller.cycle(), dev.WAITING)
        self.assertEqual(self.controller.state['gate']['kind'], 'IMPLEMENTATION')

    def fill_explanation(self):
        path = self.app / 'doc/UNDERSTANDING.md'
        lines = path.read_text(encoding='utf-8').splitlines()
        lines = [line.rsplit('|', 2)[0] + '| 두 입력 숫자를 더한 값을 반환한다. |'
                 if '<!-- flow:addition -->' in line else line for line in lines]
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self.reopen()

    def test_full_flow_real_red_green_gates_review_notes_and_delivery(self):
        self.until_implementation_gate()
        self.assertEqual(self.docker_calls, 0)
        roles = [role for role, phase in self.agent_calls]
        self.assertEqual(roles[:2], ['coordinator', 'reviewer'])
        self.assertIn(('reviewer', 'REVIEW'), self.agent_calls)
        red = next(item for item in self.controller.state['checks'] if item['label'] == 'ADD RED')
        self.assertTrue(red['checks'][0]['observed_failure'])
        self.assertEqual(red['checks'][0]['measurement']['failures'], 1)
        green = next(item for item in self.controller.state['checks'] if item['label'] == 'ADD regression')
        self.assertTrue(green['passed'])
        self.approve()
        self.assertEqual(self.controller.cycle(), dev.WAITING)
        self.assertEqual(self.controller.state['phase'], 'UNDERSTANDING')
        self.assertEqual(self.docker_calls, 1)
        before = len(self.agent_calls)
        self.assertEqual(self.controller.cycle(), dev.WAITING)
        self.assertEqual(len(self.agent_calls), before)
        self.fill_explanation()
        self.assertEqual(self.controller.cycle(), dev.WAITING)
        self.assertEqual(self.controller.state['gate']['kind'], 'FINAL')
        self.assertIn(('reviewer', 'UNDERSTANDING'), self.agent_calls)
        self.approve()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.controller.cycle(), 0)
        artifact = self.app / '.harness/deliveries/completed-project.zip'
        with zipfile.ZipFile(artifact) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn('app-project/calc.py', archive.namelist())
            self.assertIn('app-project/doc/CHANGELOG.md', archive.namelist())
            self.assertIn('app-project/doc/UNDERSTANDING.md', archive.namelist())
            self.assertFalse(any('/.harness/' in name for name in archive.namelist()))
        self.assertEqual([gate['kind'] for gate in self.controller.state['approvals']], ['PLAN', 'IMPLEMENTATION', 'FINAL'])

    def test_reopening_waiting_gate_never_spawns_implementation_without_approval(self):
        self.assertEqual(self.controller.cycle(), dev.WAITING)
        calls = len(self.agent_calls)
        old_token = self.controller.state['gate']['token']
        self.reopen()
        self.assertEqual(self.controller.cycle(), dev.WAITING)
        self.assertEqual(len(self.agent_calls), calls)
        with self.assertRaises(ValueError):
            self.controller.approve(old_token, '')
        with self.assertRaises(ValueError):
            self.controller.approve('another-stage', 'OK')
        self.assertEqual(self.controller.state['phase'], 'GATE')
        self.assertFalse((self.app / 'calc.py').exists())

    def test_three_failed_implementations_stop_before_a_fourth_and_restore_source(self):
        self.bad_implementation = True
        self.controller.cycle()
        self.approve()
        self.assertEqual(self.controller.cycle(), dev.WAITING)
        self.assertEqual(self.controller.state['gate']['kind'], 'RETRY')
        self.assertEqual(self.controller.state['failures']['ADD'], 3)
        self.assertEqual(self.agent_calls.count(('implementer', 'IMPLEMENT')), 3)
        self.assertFalse((self.app / 'calc.py').exists())
        self.reopen()
        self.assertEqual(self.controller.cycle(), dev.WAITING)
        self.assertEqual(self.agent_calls.count(('implementer', 'IMPLEMENT')), 3)

    def test_direction_change_is_recorded_and_blocks_the_patch(self):
        self.direction_requested = True
        self.controller.cycle()
        self.approve()
        self.assertEqual(self.controller.cycle(), dev.WAITING)
        self.assertEqual(self.controller.state['gate']['kind'], 'DIRECTION')
        self.assertEqual(self.controller.state['decisions'][-1]['status'], 'pending')
        self.assertFalse((self.app / 'calc.py').exists())
        self.assertIn('Fixture direction change', (self.app / 'doc/DECISIONS.md').read_text(encoding='utf-8'))

    def test_unauthorized_test_mutation_stops_without_implementation_or_retry(self):
        self.mutate_during_test = True
        self.controller.cycle()
        self.approve()
        with self.assertRaises(ValueError):
            self.controller.cycle()
        self.assertEqual(self.agent_calls.count(('implementer', 'RED')), 1)
        self.assertEqual(self.agent_calls.count(('implementer', 'IMPLEMENT')), 0)
        self.assertFalse(self.controller.state['tasks']['ADD']['complete'])

    def test_docker_blocked_never_reaches_final_or_repeats_implementation(self):
        self.until_implementation_gate()
        calls = self.agent_calls.count(('implementer', 'IMPLEMENT'))
        self.docker_blocked = True
        self.approve()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.controller.cycle(), 2)
        self.assertEqual(self.controller.state['phase'], 'DOCKER_CHECK')
        self.assertIsNone(self.controller.state['complete_at'])
        self.assertFalse((self.app / '.harness/deliveries/completed-project.zip').exists())
        self.assertEqual(self.agent_calls.count(('implementer', 'IMPLEMENT')), calls)
        self.docker_blocked = False
        self.assertEqual(self.controller.cycle(), dev.WAITING)
        self.assertEqual(self.controller.state['phase'], 'UNDERSTANDING')
        self.assertEqual(self.agent_calls.count(('implementer', 'IMPLEMENT')), calls)

    def test_deleted_successful_test_log_blocks_delivery_after_human_approval(self):
        self.until_implementation_gate()
        self.approve()
        self.controller.cycle()
        self.fill_explanation()
        self.controller.cycle()
        self.assertEqual(self.controller.state['gate']['kind'], 'FINAL')
        self.approve()
        green = next(item for item in self.controller.state['checks'] if item['label'] == 'ADD regression')
        Path(green['checks'][0]['outcome']['stdout_path']).unlink()
        with self.assertRaises(ValueError):
            self.controller.cycle()
        self.assertFalse((self.app / '.harness/deliveries/completed-project.zip').exists())


if __name__ == '__main__':
    unittest.main()
