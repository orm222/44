"""Real document receipts feed development; source separation preserves them."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock
import test_requirements as fixtures

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('handoff_development', BASE / 'development.py')
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


class DevelopmentHandoffTests(fixtures.RequirementsFixture, unittest.TestCase):
    def test_real_confirmations_required_and_app_writes_do_not_invalidate_them(self):
        self.ready_boundary()
        self.pending()
        with self.assertRaises(ValueError):
            dev.handoff(self.root)
        result = self.cli('--confirm-file', str(self.returned_document()))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        with self.assertRaises(ValueError):
            dev.handoff(self.root)
        returned = self.base / 'confirmed-functional.md'
        returned.write_bytes((self.root / 'FUNCTIONAL_SPEC.md').read_bytes())
        result = self.cli('--confirm-functional', str(returned))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        before = dev.handoff(self.root)
        app = self.base / 'app-project'
        controller = dev.Controller(self.root, app, {'model': 'fixture'})
        controller.mutate([{'path': 'calc.py', 'content': 'def add(a, b):\n    return a+b\n'}], ['calc.py'], 'sample')
        self.assertEqual(dev.handoff(self.root), before)
        self.assertEqual(controller.state['handoff']['clock'], before['clock'])
        (self.root / 'SPEC.md').write_text('Changed actual problem\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            dev.handoff(self.root)


class LauncherRoutingTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('handoff_launcher', BASE.parent / 'run_harness.py')
        self.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launcher)

    def test_run_does_not_develop_before_document_approval(self):
        with mock.patch.object(self.launcher._module, 'main', return_value=3) as documents:
            self.assertEqual(self.launcher.main(['run'], default_root='problem-project'), 3)
            documents.assert_called_once()

    def test_legacy_requirements_remains_document_only(self):
        with mock.patch.object(self.launcher._module, 'main', return_value=0) as documents:
            self.assertEqual(self.launcher.main(['requirements'], default_root='problem-project'), 0)
            documents.assert_called_once()

    def test_run_query_never_launches_model_or_development(self):
        with mock.patch.object(self.launcher._module, 'main', return_value=0) as documents:
            self.assertEqual(self.launcher.main(['run', '--status'], default_root='problem-project'), 0)
            documents.assert_called_once()

    def test_new_commands_reject_old_python_before_files_are_created(self):
        with mock.patch.object(self.launcher.sys, 'version_info', (3, 8, 0)):
            for command in ('develop', 'run'):
                self.assertEqual(self.launcher.main([command]), 2)


if __name__ == '__main__':
    unittest.main()
