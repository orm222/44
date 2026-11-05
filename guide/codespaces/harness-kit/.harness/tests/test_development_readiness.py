"""Offline prerequisite checks use real lifecycle receipts with simulated tools."""
import importlib.util
import importlib.metadata
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


BASE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('readiness_checks_test', BASE / 'development_checks.py')
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)
runtime = checks._runtime()


class Interrupted(BaseException):
    pass


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='readiness ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / 'app'
        self.problem = self.root / 'problem'
        self.app.mkdir()
        self.problem.mkdir()
        (self.problem / 'PROBLEM.md').write_text('Approved problem', encoding='utf-8')
        (self.app / 'main.py').write_text('print(1)\n', encoding='utf-8')
        runtime.problem_time.begin(self.problem)
        self.run_dir = self.app / '.harness' / 'readiness' / 'r1-1'
        self.plan = {'docker': {'compose_file': None}, 'assurance': {
            'project_type': 'non_web', 'environment': {'executables': [], 'python_packages': [], 'ports': []},
            'browser': None}}
        self.calls = []
        self.docker_code = 0
        self.compose_code = 0
        self.mutation = None
        self.missing_docker = False
        self.browser_result = {'passed': True, 'browser': 'chromium', 'version': 'fixture-1.48',
            'executables': [{'name': 'chromium-headless-shell', 'path': '/simulated/headless_shell'}]}
        original = runtime.processes.start_process
        def simulated_tools(argv, **kwargs):
            self.calls.append(list(argv))
            if self.missing_docker and argv[0] == 'docker':
                raise FileNotFoundError('Explicit simulation: docker is not installed')
            if argv[:2] == ['docker', 'info']:
                output, code = '27.0.0' if not self.docker_code else 'Docker engine unavailable', self.docker_code
            elif argv[:3] == ['docker', 'compose', 'version']:
                output, code = '2.30.0', self.compose_code
            elif len(argv) > 2 and argv[2] == checks._BROWSER_AVAILABILITY:
                output, code = json.dumps(self.browser_result), 0
            else:
                raise AssertionError('Unexpected tool execution: ' + str(argv))
            code_text = 'print(' + repr(output) + ')\n'
            if self.mutation:
                code_text += 'from pathlib import Path\nPath(' + repr(str(self.mutation)) + ').write_text("unexpected edit", encoding="utf-8")\n'
            code_text += 'raise SystemExit(' + str(code) + ')\n'
            return original([sys.executable, '-c', code_text], **kwargs)
        self.addCleanup(patch.stopall)
        patch.object(runtime.processes, 'start_process', side_effect=simulated_tools).start()

    def run_readiness(self, **kwargs):
        return checks.readiness(self.app, self.problem, self.plan, self.run_dir, **kwargs)

    def web(self):
        self.plan['assurance'].update(project_type='web', browser={'after_task': 'one', 'config': {'browser': 'chromium'}})

    def test_non_web_checks_required_docker_without_browser_discovery_and_reuses_success(self):
        result = self.run_readiness()
        self.assertTrue(result['passed'], result)
        self.assertIsNone(result['browser'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][:2], ['docker', 'info'])
        self.assertTrue(checks.verify_readiness(result))
        self.assertEqual(self.run_readiness(), result)
        self.assertEqual(len(self.calls), 1)
        with patch.object(runtime.problem_time, 'budget', side_effect=ValueError('deadline expired')):
            self.assertTrue(checks.verify_readiness(result))

    def test_legacy_plan_still_requires_docker_and_python(self):
        self.plan.pop('assurance')
        self.docker_code = 1
        result = self.run_readiness()
        self.assertFalse(result['passed'])
        self.assertIn('Docker', result['reason'])
        self.assertTrue(result['environment']['python']['supported'])
        self.assertTrue(checks.verify_readiness(result))

    def test_missing_docker_is_proven_not_started_and_blocks_without_installation(self):
        self.missing_docker = True
        result = self.run_readiness()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['docker']['outcome']['termination_reason'], 'launch_failed')
        self.assertFalse(result['docker']['outcome']['cleanup']['attempted'])
        self.assertTrue(checks.verify_readiness(result))
        self.assertEqual(len(self.calls), 1)

    def test_compose_selected_and_required_environment_missing_are_both_reported(self):
        self.plan['docker']['compose_file'] = 'compose.yml'
        self.compose_code = 1
        self.plan['assurance']['environment'] = {
            'executables': ['harness_missing_executable_093928'],
            'python_packages': ['harness-missing-distribution-093928'], 'ports': []}
        result = self.run_readiness()
        self.assertFalse(result['passed'])
        self.assertIn('Compose', result['reason'])
        self.assertIn('harness_missing_executable_093928', result['reason'])
        self.assertIn('harness-missing-distribution-093928', result['reason'])
        self.assertTrue(checks.verify_readiness(result))
        self.assertEqual(len(self.calls), 2)

    def test_web_metadata_is_required_and_bound_to_approved_browser(self):
        self.web()
        result = self.run_readiness()
        self.assertTrue(result['passed'], result)
        self.assertEqual(self.calls[-1][-1], 'chromium')
        self.assertTrue(checks.verify_readiness(result))
        self.assertNotIn('.launch(', checks._BROWSER_AVAILABILITY)
        self.assertNotIn('new_context(', checks._BROWSER_AVAILABILITY)

    def test_web_missing_engine_blocks_and_new_attempt_can_observe_repair(self):
        self.web()
        self.browser_result.update(passed=False, executables=[], reason='Required browser executable is not installed')
        result = self.run_readiness()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertIn('browser executable', result['reason'])
        self.assertTrue(checks.verify_readiness(result))
        first = len(self.calls)
        self.browser_result.update(passed=True, executables=[{'path': '/simulated/engine'}], reason='')
        self.assertFalse(self.run_readiness()['passed'])
        self.assertEqual(len(self.calls), first)
        self.run_dir = self.run_dir.parent / 'r1-2'
        repaired = self.run_readiness()
        self.assertTrue(repaired['passed'], repaired)
        self.assertEqual(len(self.calls), first + 2)

    def test_unknown_exit_never_relaunches_even_when_environment_is_available(self):
        self.run_readiness()
        (self.run_dir / 'report.json').unlink()
        prefix = self.run_dir / 'docker' / 'docker-info'
        prefix.with_suffix('.exit.json').unlink()
        prefix.with_suffix('.receipt.json').unlink()
        with self.assertRaises(ValueError):
            self.run_readiness()
        self.assertEqual(len(self.calls), 1)

    def test_source_and_controller_mutations_raise_after_cleanup(self):
        for relative in ('ignored.txt', '.harness/controller.json'):
            with self.subTest(relative=relative):
                target = self.app / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('original', encoding='utf-8')
                self.mutation = target
                self.run_dir = self.run_dir.parent / ('mutate-' + str(len(self.calls)))
                with self.assertRaisesRegex(ValueError, 'protected'):
                    self.run_readiness()
                outcome = runtime.verify_command(self.run_dir / 'docker' / 'docker-info')
                self.assertTrue(outcome['cleanup']['completed'])
                self.assertFalse((self.run_dir / 'report.json').exists())

    def test_external_guard_and_expired_original_deadline_are_hard_errors(self):
        def guard():
            raise ValueError('Original interview changed')
        with self.assertRaisesRegex(ValueError, 'interview changed'):
            self.run_readiness(guard=guard)
        self.assertFalse(self.calls)
        with patch.object(runtime.problem_time, 'budget', side_effect=runtime.problem_time.DeadlineError('deadline expired')):
            with self.assertRaisesRegex(ValueError, 'deadline expired'):
                self.run_readiness()
        self.assertFalse(self.calls)

    def test_interrupted_report_publication_recovers_without_reexecuting_tools(self):
        write = runtime.write_record
        def crash(path, value):
            if Path(path) == self.run_dir / 'report.json':
                raise Interrupted()
            return write(path, value)
        with patch.object(runtime, 'write_record', side_effect=crash):
            with self.assertRaises(Interrupted):
                self.run_readiness()
        count = len(self.calls)
        result = self.run_readiness()
        self.assertTrue(result['passed'], result)
        self.assertEqual(len(self.calls), count)
        self.assertTrue(checks.verify_readiness(result))

    def test_changed_inputs_or_logs_are_not_reused(self):
        report = self.run_readiness()
        output = Path(report['docker']['outcome']['stdout_path'])
        output.write_text('changed evidence', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'hashes'):
            checks.verify_readiness(report)
        (self.problem / 'PROBLEM.md').write_text('Unexpected change', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'inputs or plan changed'):
            self.run_readiness()
        self.assertEqual(len(self.calls), 1)


class BrowserMetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='metadata ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.engine = self.root / 'headless_shell.exe'
        self.engine.write_bytes(b'fixture; never executed')
        self.engine.chmod(0o700)
        package = types.ModuleType('playwright')
        package.__file__ = str(self.root / '__init__.py')
        api = types.ModuleType('playwright.sync_api')
        def forbidden(*args, **kwargs):
            raise AssertionError('Metadata inspection must not launch a browser or create a page')
        api.BrowserContext = type('BrowserContext', (), {'route_web_socket': forbidden, 'new_page': forbidden})
        api.BrowserType = type('BrowserType', (), {'launch': forbidden})
        api.Page = type('Page', (), {'screenshot': forbidden})
        api.Locator = type('Locator', (), {'fill': forbidden})
        api.expect = forbidden
        driver = types.ModuleType('playwright._impl._driver')
        driver.compute_driver_executable = lambda: ('metadata-node-only', 'cli.js')
        self.modules = {'playwright': package, 'playwright.sync_api': api,
                        'playwright._impl': types.ModuleType('playwright._impl'), 'playwright._impl._driver': driver}
        self.api = api

    def metadata(self):
        response = types.SimpleNamespace(returncode=0,
            stdout=json.dumps({'name': 'chromium-headless-shell', 'path': str(self.engine)}).encode('utf-8'))
        out = io.StringIO()
        with patch.dict(sys.modules, self.modules), patch.object(sys, 'argv', ['probe', 'chromium']), \
                patch('importlib.metadata.version', return_value='1.49.0'), \
                patch('subprocess.run', return_value=response) as command, contextlib.redirect_stdout(out):
            exec(checks._BROWSER_AVAILABILITY, {})
        return json.loads(out.getvalue()), command

    def test_metadata_queries_exact_headless_registry_without_browser_launch(self):
        result, command = self.metadata()
        self.assertTrue(result['passed'], result)
        argv = command.call_args.args[0]
        self.assertEqual(argv[:2], ['metadata-node-only', '-e'])
        self.assertIn("findExecutable('chromium-headless-shell')", argv[2])
        self.assertNotIn('install', argv)
        self.assertIn('later actual browser', result['scope'])

    def test_missing_executable_or_unsupported_browser_api_blocks(self):
        self.engine.unlink()
        result, command = self.metadata()
        self.assertFalse(result['passed'])
        self.assertIn('not installed', result['reason'])
        self.assertEqual(command.call_count, 1)
        delattr(self.api.BrowserContext, 'route_web_socket')
        result, command = self.metadata()
        self.assertFalse(result['passed'])
        self.assertIn('incompatible', result['reason'])
        self.assertEqual(command.call_count, 0)


if __name__ == '__main__':
    unittest.main()
