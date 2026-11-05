"""Real assurance producer processes; browser/model doubles are explicitly scoped."""
import contextlib
import copy
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1]
loader = importlib.util.spec_from_file_location('assurance_integration_subject', SOURCE / 'assurance.py')
assurance = importlib.util.module_from_spec(loader)
loader.loader.exec_module(assurance)


PRODUCER = '''import csv
import hashlib
import json
from pathlib import Path
import sys

out, problem = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
counter = out / 'calls.txt'
counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1')
input_file = problem / 'input.csv'
rows = list(csv.DictReader(input_file.open(encoding='utf-8')))
for name in ('used', 'excluded', 'quarantined'):
    with (out / (name + '.csv')).open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['id'])
        if name == 'used':
            writer.writerows([[row['id']] for row in rows])
total = sum(int(row['load']) for row in rows)
identity = hashlib.sha256(input_file.read_bytes()).hexdigest()
measurements = {'results': [
    {'case_id': 'small', 'candidate_id': 'base', 'input_sha256': identity, 'objective': total + 1, 'metrics': {'load': total}},
    {'case_id': 'small', 'candidate_id': 'selected', 'input_sha256': identity, 'objective': total, 'metrics': {'load': total}}]}
if len(sys.argv) > 3 and sys.argv[3] == 'bad_ledger':
    (out / 'used.csv').write_text('id\\nUNKNOWN\\n', encoding='utf-8')
if len(sys.argv) > 3 and sys.argv[3] == 'bad_algorithm':
    measurements['results'][1]['objective'] = total + 10
if len(sys.argv) > 3 and sys.argv[3] == 'missing_ledger':
    (out / 'quarantined.csv').unlink()
if len(sys.argv) > 3 and sys.argv[3] == 'malformed_ledger':
    (out / 'used.csv').write_text('id,id\\nA,A\\n', encoding='utf-8')
(out / 'measurements.json').write_text(json.dumps(measurements), encoding='utf-8')
print('Produced input ledgers and measured candidate values')
'''


class AssuranceIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='assurance-integration-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app, self.problem = self.root / 'app', self.root / 'problem'
        self.app.mkdir()
        self.problem.mkdir()
        (self.app / 'probe.py').write_text(PRODUCER, encoding='utf-8')
        (self.problem / 'config.json').write_text('{"problem_time_limit_seconds":9000}', encoding='utf-8')
        (self.problem / 'input.csv').write_text('id,load\nA,2\nB,3\n', encoding='utf-8')
        self.receipt = assurance.clock.begin(self.problem)

    def probe(self, mode=None):
        argv = [sys.executable, 'probe.py', '{artifacts}', '{problem}']
        if mode:
            argv.append(mode)
        return {'argv': argv, 'cwd': '.', 'timeout_seconds': 30}

    def reconciliation(self, owner='T1', mode=None):
        return {'after_task': owner, 'contract': {'id': 'ledger', 'input': 'input.csv', 'keys': ['id'],
            'parts': [{'file': kind + '.csv', 'kind': kind} for kind in ('used', 'excluded', 'quarantined')]},
            'probe': self.probe(mode)}

    def algorithm(self, owner='T1', mode=None):
        return {'after_task': owner, 'measurements': 'measurements.json', 'probe': self.probe(mode),
            'contract': {'id': 'cost', 'direction': 'minimize', 'baseline': 'base', 'selected': 'selected',
                'candidates': ['base', 'selected'], 'cases': [{'id': 'small', 'input': 'input.csv', 'exact': 5, 'max_relative_gap': 0}],
                'constraints': [{'name': 'load', 'min': 0, 'max': 10}], 'max_regression': 0}}

    def plan(self, reconciliations=None, algorithms=None):
        config = assurance.defaults()
        config.update(reconciliations=reconciliations or [], algorithms=algorithms or [])
        return {'tasks': [{'id': 'T1'}, {'id': 'T2'}], 'assurance': config}

    def directory(self, name='run'):
        return self.app / '.harness' / 'assurance' / name

    def browser_config(self):
        return {'browser': 'chromium', 'server': {'argv': [sys.executable, 'server.py'], 'cwd': '.',
            'ready_url': 'http://127.0.0.1:18271/', 'timeout_seconds': 30},
            'cases': [{'id': 'home', 'path': '/', 'width': 1280, 'height': 720, 'steps': [],
                       'expect': [{'selector': 'h1', 'text': 'Fixture'}]}]}

    def controller_fixture(self, ledger_mode=None):
        # Compose the existing fixture instead of inheriting/discovering its tests twice.
        module = importlib.import_module('test_development_integration')
        fixture = module.DevelopmentIntegrationTests(
            methodName='test_full_flow_real_red_green_gates_review_notes_and_delivery')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        (fixture.problem / 'input.csv').write_text('id,load\nA,2\nB,3\n', encoding='utf-8')
        fixture.plan['assurance'] = module.dev.assurance.defaults()
        fixture.plan['tasks'][0]['test_files'].append('tests/probe.py')
        ledger = self.reconciliation(owner='ADD', mode=ledger_mode)
        algorithm = self.algorithm(owner='ADD')
        for item in (ledger, algorithm):
            item['probe']['argv'][1] = 'tests/probe.py'
        fixture.plan['assurance']['reconciliations'] = [ledger]
        fixture.plan['assurance']['algorithms'] = [algorithm]
        producer = PRODUCER.replace(
            "total = sum(int(row['load']) for row in rows)",
            "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
            "from calc import add\n"
            "total = add(int(rows[0]['load']), int(rows[1]['load']))")
        original = fixture.agent

        def worker(*args, **kwargs):
            result = original(*args, **kwargs)
            if fixture.controller.state['phase'] == 'RED':
                result['result']['files'].append({'path': 'tests/probe.py', 'content': producer})
                fixture.model_results[result['run_id']] = copy.deepcopy(result)
            return result

        patch = mock.patch.object(module.dev.runtime, 'run_agent', side_effect=worker)
        patch.start()
        self.addCleanup(patch.stop)
        return module.dev, fixture

    def test_real_subprocess_creates_ledgers_and_algorithm_measurements(self):
        plan = self.plan([self.reconciliation()], [self.algorithm()])
        assurance.validate(plan['assurance'], plan)
        with mock.patch.object(assurance.browser, 'run_browser', side_effect=AssertionError('non-web must not launch browser')) as browser:
            report = assurance.run(self.app, self.problem, plan, self.directory(), ['T1'], final=True)
        browser.assert_not_called()
        self.assertTrue(report['passed'])
        self.assertEqual([item['kind'] for item in report['reports']], ['reconciliation', 'algorithm'])
        for item in report['reports']:
            self.assertEqual(item['outcome']['exit_code'], 0)
            self.assertTrue(item['outcome']['cleanup']['completed'])
            self.assertTrue(assurance.verify(item))
        ledger, algorithm = report['reports']
        self.assertEqual(ledger['details']['checks'][0]['counts']['input_rows'], 2)
        self.assertEqual(algorithm['details']['cases'][0]['exact_status'], 'matches_exact_on_case')

    def test_only_completed_owner_tasks_are_checked(self):
        plan = self.plan([self.reconciliation(owner='T2')], [self.algorithm(owner='T1')])
        report = assurance.run(self.app, self.problem, plan, self.directory(), ['T1'])
        self.assertEqual([item['id'] for item in report['reports']], ['cost'])
        self.assertFalse((self.directory() / 'ledger').exists())

    def test_unconfigured_or_not_yet_applicable_checks_are_not_reported_as_test_pass(self):
        for index, plan in enumerate((self.plan(), self.plan([self.reconciliation(owner='T2')]))):
            with self.subTest(index=index), mock.patch.object(assurance.browser, 'run_browser', side_effect=AssertionError('not applicable')):
                report = assurance.run(self.app, self.problem, plan, self.directory(str(index)), ['T1'])
            self.assertEqual(report['reports'], [])
            self.assertEqual(report['status'], 'NOT_APPLICABLE')

    def test_wrong_ids_fail_and_return_the_responsible_task(self):
        plan = self.plan([self.reconciliation(mode='bad_ledger')], [self.algorithm()])
        report = assurance.run(self.app, self.problem, plan, self.directory(), ['T1'])
        self.assertFalse(report['passed'])
        self.assertEqual(report['task_id'], 'T1')
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(len(report['reports']), 1)
        self.assertFalse((self.directory() / 'cost').exists())

    def test_bad_algorithm_result_fails_even_with_valid_producer_exit(self):
        report = assurance.run_probe(self.app, self.problem, 'algorithm', self.algorithm(mode='bad_algorithm'), self.directory())
        self.assertEqual(report['outcome']['exit_code'], 0)
        self.assertFalse(report['passed'])
        self.assertEqual(report['status'], 'FAIL')

    def test_missing_or_malformed_generated_ledgers_request_implementation_fix(self):
        for mode in ('missing_ledger', 'malformed_ledger'):
            with self.subTest(mode=mode):
                report = assurance.run_probe(self.app, self.problem, 'reconciliation',
                    self.reconciliation(mode=mode), self.directory(mode))
                self.assertEqual(report['outcome']['exit_code'], 0)
                self.assertEqual(report['details']['checks'][0]['stage'], 'artifact')
                self.assertEqual(report['details']['counts']['failed'], 1)
                self.assertEqual(report['details']['counts']['blocked'], 0)
                self.assertFalse(report['passed'])
                self.assertEqual(report['status'], 'FAIL')

    def test_successful_probe_reuse_does_not_launch_a_second_process(self):
        item = self.reconciliation()
        report = assurance.run_probe(self.app, self.problem, 'reconciliation', item, self.directory())
        with mock.patch.object(assurance.runtime, 'run_command', side_effect=AssertionError('completed producer must not repeat')):
            resumed = assurance.run_probe(self.app, self.problem, 'reconciliation', item, self.directory())
        self.assertEqual(report, resumed)
        self.assertEqual((Path(report['run_dir']) / 'artifacts/calls.txt').read_text(), '1')

    def test_artifact_tampering_prevents_reuse_and_evidence_verification(self):
        item = self.reconciliation()
        report = assurance.run_probe(self.app, self.problem, 'reconciliation', item, self.directory())
        (Path(report['run_dir']) / 'artifacts/used.csv').write_text('id\nOTHER\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            assurance.verify(report)
        with mock.patch.object(assurance.runtime, 'run_command', side_effect=AssertionError('tampered result must not be silently repeated')):
            with self.assertRaises(ValueError):
                assurance.run_probe(self.app, self.problem, 'reconciliation', item, self.directory())

    def test_changed_source_cannot_borrow_prior_measurement(self):
        item = self.algorithm()
        report = assurance.run_probe(self.app, self.problem, 'algorithm', item, self.directory())
        with (self.app / 'probe.py').open('a', encoding='utf-8') as stream:
            stream.write('\n# new implementation revision\n')
        with self.assertRaises(ValueError):
            assurance.run_probe(self.app, self.problem, 'algorithm', item, self.directory())
        self.assertEqual((Path(report['run_dir']) / 'artifacts/calls.txt').read_text(), '1')

    def test_missing_exit_evidence_blocks_reexecution(self):
        item = self.reconciliation()
        report = assurance.run_probe(self.app, self.problem, 'reconciliation', item, self.directory())
        (Path(report['run_dir']) / 'producer.exit.json').unlink()
        with self.assertRaises(ValueError):
            assurance.run_probe(self.app, self.problem, 'reconciliation', item, self.directory())
        self.assertEqual((Path(report['run_dir']) / 'artifacts/calls.txt').read_text(), '1')

    def test_original_deadline_blocks_new_and_cached_stage_progress(self):
        item = self.reconciliation()
        report = assurance.run_probe(self.app, self.problem, 'reconciliation', item, self.directory())
        with mock.patch.object(assurance.clock.time, 'time', return_value=self.receipt['deadline_at'] + 1):
            with self.assertRaises(ValueError):
                assurance.run_probe(self.app, self.problem, 'reconciliation', item, self.directory('new'))
            with self.assertRaises(ValueError):
                assurance.run_probe(self.app, self.problem, 'reconciliation', item, self.directory())
            self.assertTrue(assurance.verify(report))
        self.assertFalse((self.directory('new') / 'artifacts/calls.txt').exists())

    def test_validation_rejects_missing_owner_escape_and_unbounded_probe(self):
        for change in ('owner', 'timeout', 'artifact_argument', 'measurement_path'):
            plan = self.plan(algorithms=[self.algorithm()])
            entry = plan['assurance']['algorithms'][0]
            if change == 'owner':
                entry['after_task'] = 'UNKNOWN'
            elif change == 'timeout':
                entry['probe']['timeout_seconds'] = 301
            elif change == 'artifact_argument':
                entry['probe']['argv'] = [sys.executable, 'probe.py']
            else:
                entry['measurements'] = '../outside.json'
            with self.subTest(change=change), self.assertRaises(ValueError):
                assurance.validate(plan['assurance'], plan)

    def test_new_model_schema_requires_explicit_assurance_classification(self):
        loader = importlib.util.spec_from_file_location('assurance_integration_contracts', SOURCE / 'development_contracts.py')
        contracts = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(contracts)
        config = assurance.defaults()
        schema = contracts.PLAN['properties']['assurance']
        contracts.validate(config, schema)
        for changed in ({key: value for key, value in config.items() if key != 'project_type'},
                        dict(config, project_type='maybe'), dict(config, unexpected=True)):
            with self.assertRaises(ValueError):
                contracts.validate(changed, schema)

    def test_web_requires_browser_and_nonweb_cannot_enable_one(self):
        plan = self.plan()
        plan['assurance']['project_type'] = 'web'
        with self.assertRaises(ValueError):
            assurance.validate(plan['assurance'], plan)
        plan['assurance']['browser'] = {'after_task': 'T1', 'config': self.browser_config()}
        assurance.validate(plan['assurance'], plan)
        plan['assurance']['project_type'] = 'non_web'
        with self.assertRaises(ValueError):
            assurance.validate(plan['assurance'], plan)

    def test_web_browser_is_called_only_at_final_and_blocking_result_is_preserved(self):
        plan = self.plan()
        plan['assurance'].update(project_type='web', browser={'after_task': 'T1', 'config': self.browser_config()})
        assurance.validate(plan['assurance'], plan)
        simulated = {'passed': False, 'status': 'BLOCKED', 'reason': 'SIMULATED browser unavailable'}
        with mock.patch.object(assurance.browser, 'run_browser', return_value=simulated) as browser:
            assurance.run(self.app, self.problem, plan, self.directory('task'), ['T1'], final=False)
            browser.assert_not_called()
            result = assurance.run(self.app, self.problem, plan, self.directory('final'), ['T1'], final=True)
        browser.assert_called_once()
        self.assertFalse(result['passed'])
        self.assertEqual((result['status'], result['task_id']), ('BLOCKED', 'T1'))

    def test_browser_blocked_retry_uses_new_attempt_then_reuses_successful_driver_evidence(self):
        plan = self.plan()
        plan['assurance'].update(project_type='web', browser={'after_task': 'T1', 'config': self.browser_config()})
        launches = []

        def simulated_driver(app, problem, config, run_dir, guard=None):
            directory = Path(run_dir)
            saved = assurance.runtime.read_record(directory / 'report.json')
            if saved:
                return saved
            launches.append(directory)
            passed = len(launches) > 1
            report = {'passed': passed, 'status': 'PASS' if passed else 'BLOCKED',
                'run_dir': str(directory), 'simulation': 'SIMULATED browser driver; no browser engine exercised',
                'server': {'cleanup': {'completed': True}}}
            assurance.runtime.write_record(directory / 'report.json', report)
            return report

        def verify_simulated(report):
            self.assertIn('SIMULATED', report['simulation'])
            self.assertTrue(report['server']['cleanup']['completed'])
            self.assertEqual(assurance.runtime.read_record(Path(report['run_dir']) / 'report.json'), report)
            return report

        with mock.patch.object(assurance.browser, 'run_browser', side_effect=simulated_driver), \
                mock.patch.object(assurance.browser, 'verify_browser', side_effect=verify_simulated) as verify:
            blocked = assurance.run(self.app, self.problem, plan, self.directory(), ['T1'], final=True)
            passed = assurance.run(self.app, self.problem, plan, self.directory(), ['T1'], final=True)
            reused = assurance.run(self.app, self.problem, plan, self.directory(), ['T1'], final=True)
        self.assertEqual(blocked['status'], 'BLOCKED')
        self.assertTrue(passed['passed'])
        self.assertEqual(passed, reused)
        self.assertEqual([directory.name for directory in launches], ['attempt-1', 'attempt-2'])
        verify.assert_called_once()

    def test_browser_unproven_cleanup_does_not_start_another_attempt(self):
        plan = self.plan()
        plan['assurance'].update(project_type='web', browser={'after_task': 'T1', 'config': self.browser_config()})
        base = self.directory() / 'browser'
        assurance.runtime.write_record(base / 'active.json', {'number': 1})
        assurance.runtime.write_record(base / 'attempt-1/report.json',
            {'passed': False, 'status': 'BLOCKED', 'simulation': 'SIMULATED incomplete cleanup'})
        with mock.patch.object(assurance.browser, 'verify_browser', side_effect=ValueError('cleanup not proven')), \
                mock.patch.object(assurance.browser, 'run_browser', side_effect=AssertionError('must not launch')) as driver:
            with self.assertRaises(ValueError):
                assurance.run(self.app, self.problem, plan, self.directory(), ['T1'], final=True)
        driver.assert_not_called()
        self.assertEqual(assurance.runtime.read_record(base / 'active.json'), {'number': 1})

    def test_controller_preserves_approvals_and_delivers_only_after_real_assurance(self):
        dev, fixture = self.controller_fixture()
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(
                dev.assurance.browser, 'run_browser', side_effect=AssertionError('non-web browser launch')) as browser:
            self.assertEqual(fixture.controller.cycle(), dev.WAITING)
            self.assertEqual(fixture.controller.state['gate']['kind'], 'PLAN')
            calls = len(fixture.agent_calls)
            self.assertEqual(fixture.controller.cycle(), dev.WAITING)
            self.assertEqual(len(fixture.agent_calls), calls)
            self.assertFalse((fixture.app / 'calc.py').exists())
            fixture.approve()
            self.assertEqual(fixture.controller.cycle(), dev.WAITING)
            self.assertEqual(fixture.controller.state['gate']['kind'], 'IMPLEMENTATION')
            reports = list(fixture.controller.state['assurance_runs'].values())
            self.assertTrue(reports and all(item['passed'] for item in reports))
            self.assertIn(('reviewer', 'REVIEW'), fixture.agent_calls)
            self.assertEqual(fixture.docker_calls, 0)
            fixture.approve()
            self.assertEqual(fixture.controller.cycle(), dev.WAITING)
            self.assertEqual(fixture.controller.state['phase'], 'UNDERSTANDING')
            final_key = fixture.controller.state['final_assurance_key']
            final = fixture.controller.state['assurance_runs'][final_key]
            self.assertEqual([item['kind'] for item in final['reports']], ['reconciliation', 'algorithm'])
            self.assertTrue(all(dev.assurance.verify(item) for item in final['reports']))
            fixture.fill_explanation()
            self.assertEqual(fixture.controller.cycle(), dev.WAITING)
            self.assertEqual(fixture.controller.state['gate']['kind'], 'FINAL')
            self.assertFalse((fixture.app / '.harness/deliveries/completed-project.zip').exists())
            fixture.approve()
            self.assertEqual(fixture.controller.cycle(), 0)
        browser.assert_not_called()
        self.assertTrue((fixture.app / '.harness/deliveries/completed-project.zip').is_file())
        self.assertEqual([item['kind'] for item in fixture.controller.state['approvals']],
                         ['PLAN', 'IMPLEMENTATION', 'FINAL'])

    def test_controller_assurance_failure_blocks_review_and_stops_after_three_attempts(self):
        dev, fixture = self.controller_fixture(ledger_mode='bad_ledger')
        with contextlib.redirect_stdout(io.StringIO()):
            fixture.controller.cycle()
            fixture.approve()
            self.assertEqual(fixture.controller.cycle(), dev.WAITING)
            self.assertEqual(fixture.controller.state['gate']['kind'], 'RETRY')
            self.assertEqual(fixture.controller.state['failures']['ADD'], 3)
            self.assertEqual(fixture.agent_calls.count(('implementer', 'IMPLEMENT')), 3)
            self.assertNotIn(('reviewer', 'REVIEW'), fixture.agent_calls)
            self.assertFalse(fixture.controller.state['tasks']['ADD']['complete'])
            self.assertIsNone(fixture.controller.state['complete_at'])
            self.assertEqual(fixture.docker_calls, 0)
            attempts = list(fixture.controller.state['assurance_runs'].values())
            self.assertEqual(len(attempts), 3)
            self.assertTrue(all(item['task_id'] == 'ADD' and item['status'] == 'FAIL' for item in attempts))
            fixture.reopen()
            self.assertEqual(fixture.controller.cycle(), dev.WAITING)
        self.assertEqual(fixture.agent_calls.count(('implementer', 'IMPLEMENT')), 3)
        self.assertFalse((fixture.app / '.harness/deliveries/completed-project.zip').exists())


if __name__ == '__main__':
    unittest.main()
