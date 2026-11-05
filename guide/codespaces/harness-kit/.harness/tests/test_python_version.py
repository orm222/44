"""Every public command rejects old interpreters before starting a project clock."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('version_launcher', Path(__file__).resolve().parents[2] / 'run_harness.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class PythonVersionTests(unittest.TestCase):
    def test_old_python_is_rejected_before_all_commands(self):
        with tempfile.TemporaryDirectory(prefix='python-version-') as folder:
            for command in ('start', 'time-status', 'requirements'):
                with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()) as stderr:
                    with mock.patch.object(launcher.sys, 'version_info', (3, 8, 20)):
                        self.assertEqual(launcher.main([command], default_root=folder), 2)
                    self.assertIn('3.9 or later', stderr.getvalue())
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
