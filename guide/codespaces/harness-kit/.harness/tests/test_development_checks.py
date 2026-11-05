"""Real local test commands and simulated Docker command/evidence boundaries."""
import hashlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('test_development_checks_module', SOURCE / 'development_checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class RuntimeFixture:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.docker_failure = None
        self.compose = {'services': {'app': {'build': '.'}}}

    def read_record(self, path):
        return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else None

    def write_record(self, path, value):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(value), encoding='utf-8')

    def snapshot(self, root, exclude=('.harness',)):
        return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in Path(root).rglob('*') if path.is_file() and not any(
                    path.relative_to(root).as_posix() == value or path.relative_to(root).as_posix().startswith(value + '/')
                    for value in exclude)}

    def run_command(self, argv, cwd, prefix, problem_root, timeout, guard=None):
        if guard:
            guard()
        if str(prefix) in self.results:
            return self.results[str(prefix)]
        self.calls.append(argv)
        if argv[0] == 'docker':
            if self.docker_failure == 'missing':
                raise FileNotFoundError('Docker CLI unavailable')
            code, stdout, stderr = 0, '', ''
            if argv[1] == 'info':
                stdout = '27.0.0\n'
            elif 'config' in argv:
                stdout = json.dumps(self.compose)
            elif 'exec' in argv:
                stdout = 'HARNESS_TEST_RESULT {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}\n'
            else:
                stdout = 'fixture success\n'
            if self.docker_failure in argv:
                code, stderr = 1, 'fixture failure'
        else:
            execution = subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=timeout,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8'))
            code = execution.returncode
            stdout, stderr = execution.stdout.decode('utf-8'), execution.stderr.decode('utf-8')
        Path(prefix).parent.mkdir(parents=True, exist_ok=True)
        out, err = Path(str(prefix) + '.stdout.log'), Path(str(prefix) + '.stderr.log')
        out.write_text(stdout, encoding='utf-8')
        err.write_text(stderr, encoding='utf-8')
        value = dict(exit_code=code, cleanup={'completed': True}, termination_reason='completed', error=None,
            stdout_path=str(out), stderr_path=str(err))
        self.results[str(prefix)] = value
        if guard:
            guard()
        return value


class DevelopmentCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='development-checks-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app, self.problem = self.root / 'app', self.root / 'problem'
        self.app.mkdir()
        self.problem.mkdir()
        self.runtime = RuntimeFixture()
        patch = mock.patch.object(checks, 'runtime', self.runtime)
        patch.start()
        self.addCleanup(patch.stop)

    def contract(self, identifier='unit', framework='unittest', argv=None):
        return dict(id=identifier, argv=argv or [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'],
            cwd='.', timeout_seconds=30, test_framework=framework, min_tests=1,
            requirement='R-01', oracle='The specification requires 1 + 1 = 2, independently specified')

    def write_test(self, body):
        folder = self.app / 'tests'
        folder.mkdir(exist_ok=True)
        (folder / 'test_sample.py').write_text('import unittest\nclass Sample(unittest.TestCase):\n'
            '    def test_example(self):\n        ' + body + '\n', encoding='utf-8')

    def run_checks(self, definitions=None, expected='pass', name='one'):
        return checks.run_checks(self.app, self.problem, definitions or [self.contract()],
            self.app / '.harness' / name, expected=expected)

    def docker_config(self, compose=False):
        (self.app / 'Dockerfile').write_text('FROM python:3.9-slim\nWORKDIR /app\n', encoding='utf-8')
        (self.app / '.dockerignore').write_text('.git\n.harness\n', encoding='utf-8')
        value = dict(checks=[self.contract('business', 'command', ['python', 'check_business.py'])])
        if compose:
            (self.app / 'compose.yaml').write_text('services:\n  app:\n    build: .\n', encoding='utf-8')
            value.update(compose_file='compose.yaml', service='app')
        return value

    def cleanup(self, argv, cwd, directory):
        value = dict(completed=True, argv=argv, exit_code=0)
        self.runtime.write_record(directory / 'cleanup.json', value)
        return value

    def test_contract_rejects_escape_duplicates_and_zero_minimum(self):
        for bad in [dict(self.contract(), cwd='../escape'), dict(self.contract(), cwd='D:/escape'),
                    dict(self.contract(), argv=[]), dict(self.contract(), min_tests=0),
                    dict(self.contract(), timeout_seconds=True), dict(self.contract(), oracle='')]:
            with self.assertRaises(ValueError):
                checks.validate_checks([bad])
        with self.assertRaises(ValueError):
            checks.validate_checks([self.contract(), self.contract()])

    def test_real_red_green_and_cached_command(self):
        self.write_test('self.assertEqual(1 + 1, 3)')
        red = self.run_checks(expected='fail', name='red')
        self.assertTrue(red['passed'])
        self.assertTrue(red['checks'][0]['observed_failure'])
        self.write_test('self.assertEqual(1 + 1, 2)')
        green = self.run_checks(name='green')
        self.assertTrue(green['passed'])
        calls = len(self.runtime.calls)
        self.assertTrue(self.run_checks(name='green')['passed'])
        self.assertEqual(len(self.runtime.calls), calls)
        self.assertTrue((self.app / '.harness/green/report.json').exists())

    def test_all_green_does_not_count_as_observed_red(self):
        self.write_test('self.assertTrue(True)')
        self.assertFalse(self.run_checks(expected='fail')['passed'])

    def test_same_run_cannot_relabel_old_logs_as_new_source_results(self):
        self.write_test('self.assertTrue(True)')
        self.assertTrue(self.run_checks()['passed'])
        self.write_test('self.assertTrue(False)')
        calls = len(self.runtime.calls)
        with self.assertRaisesRegex(ValueError, 'source or verification contract changed'):
            self.run_checks()
        self.assertEqual(len(self.runtime.calls), calls)

    def test_red_allows_other_checks_to_pass(self):
        self.write_test('self.assertEqual(2, 3)')
        green = self.contract('independent', 'command', [sys.executable, '-c',
            'print(\'HARNESS_TEST_RESULT {"tests":1,"failures":0,"errors":0,"skipped":0}\')'])
        self.assertTrue(self.run_checks([green, self.contract()], expected='fail')['passed'])

    def test_import_error_is_not_a_red_behavior_test(self):
        self.write_test('import certainly_missing_dependency_for_harness_test')
        report = self.run_checks(expected='fail')
        self.assertFalse(report['passed'])
        self.assertEqual(report['checks'][0]['measurement']['errors'], 1)

    def test_zero_tests_and_all_skipped_are_not_pass(self):
        (self.app / 'tests').mkdir()
        self.assertFalse(self.run_checks(name='empty')['passed'])
        self.write_test('self.skipTest("not implemented")')
        self.assertFalse(self.run_checks(name='skipped')['passed'])

    def test_command_needs_real_measurement_contract(self):
        contract = self.contract(framework='command', argv=[sys.executable, '-c', 'print("OK")'])
        self.assertFalse(self.run_checks([contract])['passed'])

    def test_ignored_file_mutation_invalidates_result(self):
        (self.app / '.gitignore').write_text('node_modules/\n', encoding='utf-8')
        (self.app / 'node_modules').mkdir()
        argv = [sys.executable, '-c', 'from pathlib import Path; Path("node_modules/changed").write_text("x"); '
            'print(\'HARNESS_TEST_RESULT {"tests":1,"failures":0,"errors":0,"skipped":0}\')']
        report = self.run_checks([self.contract(framework='command', argv=argv)])
        self.assertFalse(report['passed'])
        self.assertNotEqual(report['source_before'], report['source_after'])
        self.assertTrue(report['protected_changed'])
        self.assertEqual(report['status'], 'BLOCKED')

    def test_test_command_cannot_change_controller_state_or_prior_evidence(self):
        target = self.app / '.harness/development.json'
        target.parent.mkdir()
        target.write_text('original-state', encoding='utf-8')
        argv = [sys.executable, '-c', 'from pathlib import Path; Path(".harness/development.json").write_text("changed"); '
            'print(\'HARNESS_TEST_RESULT {"tests":1,"failures":0,"errors":0,"skipped":0}\')']
        report = self.run_checks([self.contract(framework='command', argv=argv)])
        self.assertFalse(report['passed'])
        self.assertIn('controller state', report['checks'][0]['reason'])
        self.assertTrue(report['protected_changed'])

    def test_pytest_and_node_counts_and_errors(self):
        pytest = checks.measured_result('pytest', '================ 4 passed, 1 skipped in 0.12s ================\n')
        self.assertEqual((pytest['tests'], pytest['skipped']), (4, 1))
        node = checks.measured_result('node', '# tests 3\n# pass 2\n# fail 1\n# skipped 0\n')
        self.assertEqual((node['tests'], node['failures']), (3, 1))
        self.assertFalse(checks.measured_result('unittest', 'Ran 0 tests in 0s\n') ['measured'])

    def test_pytest_runtime_error_is_not_an_assertion_failure(self):
        contract = self.contract('pytest', 'pytest')
        stdout, stderr = self.root / 'pytest.stdout', self.root / 'pytest.stderr'
        stdout.write_text('E TypeError: unexpected input\n=== 1 failed in 0.12s ===\n', encoding='utf-8')
        stderr.write_text('', encoding='utf-8')
        outcome = dict(exit_code=1, cleanup={'completed': True}, termination_reason='completed', error=None,
                       stdout_path=str(stdout), stderr_path=str(stderr))
        result = checks._check_result(contract, outcome, 'fail')
        self.assertFalse(result['passed'])
        stdout.write_text('E assert 3 == 2\n=== 1 failed in 0.12s ===\n', encoding='utf-8')
        self.assertTrue(checks._check_result(contract, outcome, 'fail')['passed'])

    def test_real_runtime_uses_original_receipt_and_recovers_finished_check(self):
        # This integration check uses the actual durable subprocess helper.
        previous = checks.runtime
        checks.runtime = None
        try:
            checks._runtime().problem_time.begin(self.problem)
            self.write_test('self.assertEqual(1 + 1, 2)')
            self.assertTrue(self.run_checks([self.contract('unit.real')])['passed'])
            resumed = self.run_checks([self.contract('unit.real')])
            self.assertTrue(resumed['checks'][0]['outcome']['recovered'])
            self.assertTrue(resumed['passed'])
            self.assertTrue(checks.verify_report(resumed))
            stdout = Path(resumed['checks'][0]['outcome']['stdout_path'])
            stdout.write_text('tampered', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'hashes differ'):
                checks.verify_report(resumed)
        finally:
            checks.runtime = previous

    def test_final_evidence_rejects_missing_successful_command_or_docker_stages(self):
        with self.assertRaisesRegex(ValueError, 'no original verification contract'):
            checks.verify_report({'passed': True, 'expected': 'pass', 'checks': [{'passed': True}]})
        with self.assertRaisesRegex(ValueError, 'missing stages'):
            checks.verify_report({'passed': True, 'stages': []})

    def test_docker_config_and_workdir_validation(self):
        config = self.docker_config()
        with self.assertRaises(ValueError):
            checks.validate_docker(dict(config, context='../outside'))
        with self.assertRaises(ValueError):
            checks.validate_docker(dict(config, host_port=8080))
        bad = self.contract('business', 'command', ['python', 'check.py'])
        bad['cwd'] = 'tests'
        with self.assertRaises(ValueError):
            checks.validate_docker(dict(config, checks=[bad]))

    def test_docker_order_completion_reuse_and_evidence_tampering(self):
        config = self.docker_config()
        run_dir = self.app / '.harness/docker'
        with mock.patch.object(checks, '_cleanup', side_effect=self.cleanup) as cleanup:
            report = checks.run_docker(self.app, self.problem, config, run_dir)
            self.assertTrue(report['passed'], report)
            self.assertEqual([stage['stage'] for stage in report['stages']], ['config', 'build', 'start', 'verify'])
            self.assertEqual(cleanup.call_args[0][0][:3], ['docker', 'rm', '--force'])
            self.assertEqual(cleanup.call_args[0][0][3], report['resource_name'])
            calls = len(self.runtime.calls)
            self.assertTrue(checks.run_docker(self.app, self.problem, config, run_dir)['passed'])
            self.assertEqual(len(self.runtime.calls), calls)
            self.assertEqual(cleanup.call_count, 1)
            evidence = run_dir / next(iter(report['evidence']))
            evidence.write_text('changed', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'evidence changed'):
                checks.run_docker(self.app, self.problem, config, run_dir)

    def test_docker_build_failure_blocks_start(self):
        config = self.docker_config()
        self.runtime.docker_failure = 'build'
        with mock.patch.object(checks, '_cleanup', side_effect=self.cleanup) as cleanup:
            report = checks.run_docker(self.app, self.problem, config, self.app / '.harness/docker')
        self.assertFalse(report['passed'])
        self.assertEqual(report['status'], 'FAIL')
        self.assertFalse(any('run' in argv for argv in self.runtime.calls))
        cleanup.assert_not_called()

    def test_docker_missing_is_blocked_and_never_passed(self):
        config = self.docker_config()
        self.runtime.docker_failure = 'missing'
        report = checks.run_docker(self.app, self.problem, config, self.app / '.harness/docker')
        self.assertEqual(report['status'], 'BLOCKED')
        self.assertFalse(report['passed'])
        self.assertEqual(report['stages'], [])

    def test_docker_functional_failure_still_cleans_only_owned_container(self):
        config = self.docker_config()
        self.runtime.docker_failure = 'exec'
        with mock.patch.object(checks, '_cleanup', side_effect=self.cleanup) as cleanup:
            report = checks.run_docker(self.app, self.problem, config, self.app / '.harness/docker')
        self.assertFalse(report['passed'])
        cleanup.assert_called_once()
        self.assertTrue(report['cleanup']['completed'])

    def test_compose_order_and_cleanup_does_not_delete_volumes(self):
        config = self.docker_config(compose=True)
        with mock.patch.object(checks, '_cleanup', side_effect=self.cleanup) as cleanup:
            report = checks.run_docker(self.app, self.problem, config, self.app / '.harness/docker')
        self.assertTrue(report['passed'], report)
        argv = cleanup.call_args[0][0]
        self.assertIn('down', argv)
        self.assertNotIn('-v', argv)
        self.assertNotIn('--volumes', argv)
        self.assertIn(report['resource_name'], argv)

    def test_compose_fixed_names_are_rejected_before_build(self):
        config = self.docker_config(compose=True)
        self.runtime.compose['services']['app']['container_name'] = 'existing-user-container'
        report = checks.run_docker(self.app, self.problem, config, self.app / '.harness/docker')
        self.assertFalse(report['passed'])
        self.assertFalse(any('build' in argv for argv in self.runtime.calls))

    def test_compose_allows_readonly_project_inputs_and_owned_database_volume(self):
        config = self.docker_config(compose=True)
        inputs = self.app / 'inputs'
        inputs.mkdir()
        (inputs / 'data.csv').write_text('value\n1\n', encoding='utf-8')
        data = self.problem / 'source.csv'
        data.write_text('value\n2\n', encoding='utf-8')
        self.runtime.compose = {'services': {
            'app': {'build': '.', 'security_opt': ['no-new-privileges:true'], 'volumes': [
                {'type': 'bind', 'source': str(inputs), 'target': '/input', 'read_only': True},
                {'type': 'bind', 'source': str(data), 'target': '/source.csv', 'read_only': True}]},
            'db': {'image': 'postgres:16', 'volumes': [
                {'type': 'volume', 'source': 'db-data', 'target': '/var/lib/postgresql/data'}]}},
            'volumes': {'db-data': {}}, 'networks': {'default': {}}}
        with mock.patch.object(checks, '_cleanup', side_effect=self.cleanup):
            report = checks.run_docker(self.app, self.problem, config, self.app / '.harness/docker')
        self.assertTrue(report['passed'], report.get('reason'))
        self.assertNotIn('--volumes', report['cleanup']['argv'])

    def test_compose_accepts_only_current_project_generated_resource_names(self):
        self.docker_config(compose=True)
        project = 'harness-' + 'a' * 20
        description = {'services': {'app': {'build': '.'}},
            'volumes': {'db': {'name': project + '_db'}},
            'networks': {'default': {'name': project + '_default'}}}
        checks._validate_compose(description, self.app, self.problem, project, 'app')
        description['volumes']['db']['name'] = 'another-project_db'
        with self.assertRaisesRegex(ValueError, 'unique project'):
            checks._validate_compose(description, self.app, self.problem, project, 'app')

    def test_compose_unsafe_host_resources_are_rejected_before_build_or_start(self):
        config = self.docker_config(compose=True)
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'Dockerfile').write_text('FROM python:3.9-slim\n', encoding='utf-8')
        base = {'services': {'app': {'build': '.'}}}
        cases = []
        for source, read_only in ((str(outside), True), (str(self.app), False)):
            case = copy.deepcopy(base)
            case['services']['app']['volumes'] = [{'type': 'bind', 'source': source, 'target': '/data', 'read_only': read_only}]
            cases.append(case)
        for kind in ('volumes', 'networks'):
            for declaration in ({'external': True}, {'name': 'shared-user-resource'}, {'driver_opts': {'device': str(outside)}}):
                case = copy.deepcopy(base)
                case[kind] = {'shared': declaration}
                cases.append(case)
        for field, value in (('privileged', True), ('network_mode', 'host'), ('pid', 'host'),
                             ('devices', ['/dev/sda:/dev/sda']), ('volumes_from', ['existing-user-container'])):
            case = copy.deepcopy(base)
            case['services']['app'][field] = value
            cases.append(case)
        for build in ({'context': str(outside)}, {'context': '.', 'dockerfile': str(outside / 'Dockerfile')},
                      {'context': 'https://example.invalid/source.git'}):
            case = copy.deepcopy(base)
            case['services']['app']['build'] = build
            cases.append(case)
        for index, description in enumerate(cases):
            with self.subTest(index=index):
                self.runtime.compose = description
                before = len(self.runtime.calls)
                with mock.patch.object(checks, '_cleanup', side_effect=self.cleanup) as cleanup:
                    report = checks.run_docker(self.app, self.problem, config, self.app / '.harness' / ('unsafe-' + str(index)))
                self.assertFalse(report['passed'])
                self.assertFalse(any('build' in argv or 'up' in argv for argv in self.runtime.calls[before:]))
                cleanup.assert_not_called()

    def test_compose_context_must_exclude_harness_execution_evidence(self):
        config = self.docker_config(compose=True)
        (self.app / '.dockerignore').write_text('.git\n', encoding='utf-8')
        report = checks.run_docker(self.app, self.problem, config, self.app / '.harness/docker')
        self.assertFalse(report['passed'])
        self.assertIn('.harness last', report.get('reason', ''))
        self.assertFalse(any('build' in argv for argv in self.runtime.calls))
        (self.app / '.dockerignore').write_text('.harness\n', encoding='utf-8')
        (self.app / 'Dockerfile.dockerignore').write_text('.git\n', encoding='utf-8')
        report = checks.run_docker(self.app, self.problem, config, self.app / '.harness/docker-specific')
        self.assertFalse(report['passed'])
        self.assertIn('.harness last', report.get('reason', ''))
        self.assertFalse(any('build' in argv for argv in self.runtime.calls))

    def test_cleanup_failure_blocks_completion(self):
        config = self.docker_config()
        with mock.patch.object(checks, '_cleanup', return_value={'completed': False}):
            report = checks.run_docker(self.app, self.problem, config, self.app / '.harness/docker')
        self.assertFalse(report['passed'])
        self.assertEqual(report['status'], 'BLOCKED')

    def test_incomplete_execution_blocks_docker_recovery(self):
        directory = self.app / '.harness/docker'
        attempt = directory / 'docker-test'
        attempt.mkdir(parents=True)
        (attempt / 'start.launch.json').write_text('{}', encoding='utf-8')
        self.runtime.write_record(directory / 'docker-state.json', dict(binding='fixed', attempt='docker-test',
            creating=True, finished=False, resource_name='harness-' + 'a' * 20, compose_file=None))
        with mock.patch.object(checks, '_cleanup') as cleanup:
            with self.assertRaisesRegex(ValueError, 'no exit evidence'):
                checks._recover_docker_resources(self.app, directory, 'fixed')
        cleanup.assert_not_called()

    def test_completed_process_after_controller_crash_recovers_owned_resource(self):
        directory = self.app / '.harness/docker'
        attempt = directory / 'docker-test'
        attempt.mkdir(parents=True)
        (attempt / 'start.launch.json').write_text('{}', encoding='utf-8')
        (attempt / 'start.exit.json').write_text('{"cleanup":{"completed":true}}', encoding='utf-8')
        self.runtime.write_record(directory / 'docker-state.json', dict(binding='fixed', attempt='docker-test',
            creating=True, finished=False, resource_name='harness-' + 'a' * 20, compose_file=None))
        with mock.patch.object(checks, '_cleanup', side_effect=self.cleanup) as cleanup:
            checks._recover_docker_resources(self.app, directory, 'fixed')
        self.assertEqual(cleanup.call_args[0][0][-1], 'harness-' + 'a' * 20)
        self.assertTrue(self.runtime.read_record(directory / 'docker-state.json')['finished'])

    def test_failed_launch_with_no_started_process_does_not_strand_docker_recovery(self):
        directory = self.app / '.harness/docker'
        attempt = directory / 'docker-test'
        attempt.mkdir(parents=True)
        (attempt / 'start.launch.json').write_text('{}', encoding='utf-8')
        (attempt / 'start.exit.json').write_text(json.dumps({'termination_reason': 'launch_failed',
            'exit_code': None, 'cleanup': {'completed': False, 'attempted': False}}), encoding='utf-8')
        self.runtime.write_record(directory / 'docker-state.json', dict(binding='fixed', attempt='docker-test',
            creating=True, finished=False, resource_name='harness-' + 'a' * 20, compose_file=None))
        with mock.patch.object(checks, '_cleanup', side_effect=self.cleanup):
            checks._recover_docker_resources(self.app, directory, 'fixed')
        self.assertTrue(self.runtime.read_record(directory / 'docker-state.json')['finished'])


if __name__ == '__main__':
    unittest.main()
