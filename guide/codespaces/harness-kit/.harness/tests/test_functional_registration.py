"""Recover a saved functional intent before the first state-pointer publication."""
import copy
import json
import os
import subprocess
import sys
import unittest

import test_requirements as fixtures


class FunctionalRegistrationTests(fixtures.RequirementsFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ready_boundary()

    def interrupt_registration(self):
        script = self.base / 'interrupt_registration.py'
        script.write_text('''import importlib.util, os, sys
from pathlib import Path
root = Path(sys.argv[1])
loader = importlib.util.spec_from_file_location("registration_subject", root / ".harness/requirements.py")
module = importlib.util.module_from_spec(loader)
loader.loader.exec_module(module)
original = module._write
def write(root, name, data, directory):
    if name == module.FUNCTIONAL_STATE and directory.startswith(".harness/work/functional-"):
        os._exit(86)
    return original(root, name, data, directory)
module._write = write
raise SystemExit(module.main(["--root", str(root), "--confirm-requirements", sys.argv[2]]))
''', encoding='utf-8')
        result = subprocess.run([sys.executable, '-B', str(script), str(self.root), str(self.returned_document())],
            cwd=str(self.root), capture_output=True, text=True, encoding='utf-8', timeout=45,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8'))
        self.assertEqual(result.returncode, 86, result.stdout + result.stderr)
        candidates = [path for path in (self.root / '.harness/work').glob('functional-*/transaction.json')
                      if json.loads(path.read_text(encoding='utf-8'))['stage'] == 'starting']
        self.assertEqual(len(candidates), 1)
        return candidates[0], json.loads(candidates[0].read_text(encoding='utf-8'))

    def test_first_registration_after_real_exit_resumes_same_intent_with_one_model_call(self):
        self.pending()
        path, journal = self.interrupt_registration()
        self.assertFalse((self.root / self.requirements.FUNCTIONAL_STATE).exists())
        self.assertEqual(self.kinds(), ['requirements'])
        before = path.read_bytes()
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        state = self.state(stage='functional')
        self.assertEqual(state['directory'], journal['directory'])
        self.assertEqual(state['revision'], 1)
        self.assertEqual(state['status'], 'WAITING_CONFIRMATION')
        self.assertNotEqual(path.read_bytes(), before)
        self.assertEqual(self.kinds(), ['requirements', 'functional'])
        self.assertEqual(self.cli().returncode, 3)
        self.assertEqual(self.kinds(), ['requirements', 'functional'])

    def test_registration_replaces_only_the_saved_stale_pointer_after_real_exit(self):
        self.pending()
        self.assertEqual(self.cli('--confirm-requirements', str(self.returned_document())).returncode, 3)
        old = self.state(stage='functional')
        self.fixture.write('SPEC.md', 'Return measured values and their timestamps.\n')
        self.scenario(markdown=fixtures.DOCUMENT + 'Include timestamps.\n')
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        state_path = self.root / self.requirements.FUNCTIONAL_STATE
        stale = state_path.read_bytes()
        self.assertEqual(self.state(current=False, stage='functional')['status'], 'STALE')
        path, journal = self.interrupt_registration()
        self.assertEqual(state_path.read_bytes(), stale)
        calls = self.kinds()
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        state = self.state(stage='functional')
        self.assertEqual(state['directory'], journal['directory'])
        self.assertEqual(state['revision'], old['revision'] + 1)
        self.assertEqual(self.kinds(), calls + ['functional'])
        self.assertTrue((self.root / old['directory'] / 'transaction.json').is_file())

    def test_unregistered_intent_with_extra_execution_or_changed_source_stays_unpublished(self):
        self.pending()
        path, _ = self.interrupt_registration()
        original = path.read_bytes()
        calls = self.kinds()
        launch = path.parent / 'model.launch.json'
        launch.write_text('{}', encoding='utf-8')
        result = self.cli()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse((self.root / self.requirements.FUNCTIONAL_STATE).exists())
        self.assertEqual(self.kinds(), calls)
        self.assertEqual(path.read_bytes(), original)
        launch.unlink()
        self.fixture.write('unexpected.txt', 'Unrelated user change\n')
        result = self.cli()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse((self.root / self.requirements.FUNCTIONAL_STATE).exists())
        self.assertEqual(self.kinds(), calls)
        self.assertEqual(path.read_bytes(), original)

    def test_self_checksummed_command_or_answers_cannot_be_registered(self):
        self.pending()
        path, _ = self.interrupt_registration()
        original = self.requirements._read_signed(path)
        calls = self.kinds()
        for field in ('command', 'answers'):
            with self.subTest(field=field):
                forged = copy.deepcopy(original)
                if field == 'command':
                    forged['argv'][forged['argv'].index('--model') + 1] = 'untrusted-model'
                else:
                    forged['context']['actual_answers'] = [{'question_id': 'Q-001', 'text': 'Fabricated human answer'}]
                    forged['answers_markdown'] += '\nFabricated human answer\n'
                    forged['prompt'] += '\nFabricated human answer\n'
                    forged['prompt_sha256'] = self.requirements.boundary.digest(forged['prompt'].encode('utf-8'))
                state = self.requirements.boundary.decode(self.requirements.clarify._decoded(
                    forged['start_files'][self.requirements.FUNCTIONAL_STATE]).decode('utf-8'))
                state.pop('_integrity')
                state['invocation_sha256'] = self.requirements._binding(forged)
                forged['start_files'][self.requirements.FUNCTIONAL_STATE] = self.requirements.clarify._encoded(
                    self.requirements._signed(state))
                path.write_bytes(self.requirements._signed(forged))
                result = self.cli()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse((self.root / self.requirements.FUNCTIONAL_STATE).exists())
                self.assertEqual(self.kinds(), calls)
                self.assertEqual(path.read_bytes(), self.requirements._signed(forged))


if __name__ == '__main__':
    unittest.main()
