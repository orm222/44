"""Two explicit document approvals, with real local workers and no external API."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import test_requirements as fixtures


class FunctionalTests(fixtures.RequirementsFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ready_boundary()

    def functional_document(self):
        return self.root / self.requirements.FUNCTIONAL_DOCUMENT

    def functional_state(self, current=True):
        return self.state(current=current, stage="functional")

    def returned_functional(self):
        returned = self.base / "operator returned functional 한글.md"
        returned.write_bytes(self.functional_document().read_bytes())
        return returned

    def functional_pending(self):
        self.pending()
        result = self.cli("--confirm-requirements", str(self.returned_document()))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.state()["status"], "CONFIRMED")
        self.assertEqual(self.functional_state()["status"], "WAITING_CONFIRMATION")
        self.assertEqual(self.kinds(), ["requirements", "functional"])
        return result

    def test_requirements_approval_is_required_before_functional_generation_or_confirmation(self):
        self.pending()
        returned = self.returned_document()
        result = self.cli("--confirm-functional", str(returned))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(self.functional_document().exists())
        self.assertFalse((self.root / self.requirements.FUNCTIONAL_STATE).exists())
        self.assertEqual(self.state()["status"], "WAITING_CONFIRMATION")
        self.assertEqual(self.kinds(), ["requirements"])

    def test_two_explicit_approvals_end_without_implementation_or_more_models(self):
        self.functional_pending()
        calls = self.calls()
        context = json.loads(calls[-1]["prompt"].split("REQUIREMENTS_CONTEXT\n", 1)[1])
        self.assertEqual(context["requirements_document"], self.requirements.DOCUMENT)
        self.assertEqual(context["input_hashes"][self.requirements.DOCUMENT],
                         self.requirements.boundary.digest(self.document().read_bytes()))
        self.assertEqual(context["upstream"]["confirmation_token"], self.state()["confirmation"]["token"])
        self.assertIn(fixtures.FUNCTIONAL, self.functional_document().read_text(encoding="utf-8"))
        state_path = self.root / self.requirements.FUNCTIONAL_STATE
        waiting = state_path.read_bytes()
        self.assertEqual(self.cli().returncode, 3)
        self.assertEqual(state_path.read_bytes(), waiting)
        returned = self.returned_functional()
        result = self.cli("--confirm-functional", str(returned))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("COMPLETE", result.stdout)
        self.assertEqual(self.functional_state()["status"], "CONFIRMED")
        complete = state_path.read_bytes()
        for arguments in ((), ("--status",), ("--confirm-functional", str(returned)),
                          ("--confirm-requirements", str(self.returned_document()))):
            with self.subTest(arguments=arguments):
                again = self.cli(*arguments)
                self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
                self.assertEqual(state_path.read_bytes(), complete)
        self.assertEqual(self.kinds(), ["requirements", "functional"])
        self.assert_no_implementation()
        for call in calls:
            self.assertEqual(call["argv"][call["argv"].index("--sandbox") + 1], "read-only")

    def test_changed_input_requires_new_requirements_approval_before_new_functional(self):
        self.functional_pending()
        old_returned = self.returned_functional()
        old_revision = self.functional_state()["revision"]
        self.fixture.write("SPEC.md", "Return measured values with timestamps.\n")
        result = self.cli("--confirm-functional", str(old_returned))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.kinds(), ["requirements", "functional"])
        self.scenario(markdown=fixtures.DOCUMENT + "Return timestamps.\n",
                      functional=fixtures.FUNCTIONAL + "F-02: Include the timestamp.\n")
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.state()["status"], "WAITING_CONFIRMATION")
        self.assertEqual(self.kinds(), ["requirements", "functional", "clarify", "requirements"])
        self.assertEqual(self.cli("--confirm-functional", str(old_returned)).returncode, 2)
        result = self.cli("--confirm-requirements", str(self.returned_document()))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.functional_state()["revision"], old_revision + 1)
        self.assertEqual(self.cli("--confirm-functional", str(old_returned)).returncode, 2)
        self.assertEqual(self.kinds(), ["requirements", "functional", "clarify", "requirements", "functional"])

    def test_returned_mismatch_and_internal_file_cannot_confirm_functional(self):
        self.functional_pending()
        original = self.functional_document().read_bytes()
        returned = self.returned_functional()
        returned.write_bytes(original + b"Unapproved feature\n")
        for path in (returned, self.functional_document()):
            with self.subTest(path=path):
                result = self.cli("--confirm-functional", str(path))
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.functional_state()["status"], "WAITING_CONFIRMATION")
                self.assertEqual(self.functional_document().read_bytes(), original)
        self.assertEqual(self.kinds(), ["requirements", "functional"])

    def test_functional_document_tampering_is_not_adopted_or_overwritten(self):
        self.functional_pending()
        self.functional_document().write_bytes(b"Changed manually\n")
        result = self.cli()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.functional_document().read_bytes(), b"Changed manually\n")
        self.assertEqual(self.kinds(), ["requirements", "functional"])

    def test_functional_worker_source_mutation_prevents_publication(self):
        self.pending()
        original = self.document().read_bytes()
        self.scenario(mutate="functional")
        result = self.cli("--confirm-requirements", str(self.returned_document()))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(self.functional_document().exists())
        self.assertEqual(self.document().read_bytes(), original)
        self.assertEqual(self.kinds(), ["requirements", "functional"])

    def test_completed_functional_worker_is_not_repeated_after_interruption(self):
        self.pending()
        execute = self.requirements.lifecycle.execute
        def stop_after_success(*args, **kwargs):
            result = execute(*args, **kwargs)
            self.assertEqual(result, 0)
            raise fixtures.AbruptStop()
        with mock.patch.object(self.requirements.lifecycle, "execute", side_effect=stop_after_success):
            with self.assertRaises(fixtures.AbruptStop):
                self.local("--confirm-requirements", str(self.returned_document()))
        self.assertEqual(self.kinds(), ["requirements", "functional"])
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.functional_state()["status"], "WAITING_CONFIRMATION")
        self.assertEqual(self.kinds(), ["requirements", "functional"])

    def test_actual_process_exit_after_functional_markdown_recovers_without_model(self):
        self.pending()
        returned = self.returned_document()
        script = self.base / "crash_functional.py"
        script.write_text('''import importlib.util, os, sys
from pathlib import Path
root = Path(sys.argv[1])
loader = importlib.util.spec_from_file_location("crash_functional", root / ".harness/requirements.py")
module = importlib.util.module_from_spec(loader)
loader.loader.exec_module(module)
original = os.replace
def replace(source, destination):
    result = original(source, destination)
    if Path(destination) == root / module.FUNCTIONAL_DOCUMENT:
        os._exit(86)
    return result
os.replace = replace
raise SystemExit(module.main(["--root", str(root), "--confirm-requirements", sys.argv[2]]))
''', encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(script), str(self.root), str(returned)],
            cwd=str(self.root), capture_output=True, text=True, encoding="utf-8", timeout=45,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"))
        self.assertEqual(result.returncode, 86, result.stdout + result.stderr)
        original = self.functional_document().read_bytes()
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.functional_document().read_bytes(), original)
        self.assertEqual(self.functional_state()["status"], "WAITING_CONFIRMATION")
        self.assertEqual(self.kinds(), ["requirements", "functional"])

    def test_failed_functional_worker_retries_without_losing_parent_approval(self):
        self.pending()
        self.scenario(functional_exit=7)
        result = self.cli("--confirm-requirements", str(self.returned_document()))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        outcomes = list((self.root / ".harness/work").glob("functional-*/model.exit.json"))
        self.assertEqual(len(outcomes), 1)
        outcome = json.loads(outcomes[0].read_text(encoding="utf-8"))
        self.assertEqual(outcome["exit_code"], 7)
        self.assertTrue(outcome["cleanup"]["completed"])
        self.assertFalse(self.functional_document().exists())
        parent = self.state()["confirmation"]
        original = outcomes[0].read_bytes()
        again = self.cli()
        self.assertEqual(again.returncode, 2, again.stdout + again.stderr)
        self.assertEqual(self.kinds(), ["requirements", "functional", "functional"])
        self.assertEqual(self.functional_state(current=False)["revision"], 2)
        self.scenario()
        completed = self.cli()
        self.assertEqual(completed.returncode, 3, completed.stdout + completed.stderr)
        self.assertEqual(self.functional_state()["revision"], 3)
        self.assertEqual(self.state()["confirmation"], parent)
        self.assertEqual(outcomes[0].read_bytes(), original)
        confirmed = self.cli("--confirm-functional", str(self.returned_functional()))
        self.assertEqual(confirmed.returncode, 0, confirmed.stdout + confirmed.stderr)

    def test_deadline_during_functional_confirmation_scan_prevents_complete(self):
        self.functional_pending()
        returned = self.returned_functional()
        state = self.functional_state()
        state_path = self.root / self.requirements.FUNCTIONAL_STATE
        before = state_path.read_bytes()
        clock = self.requirements.problem_time
        timing = clock.load(self.root, required=True)
        current = [timing["received_at"] + 1]
        verified = self.requirements._verify_finished
        expired = []
        def expire_after_scan(*args, **kwargs):
            result = verified(*args, **kwargs)
            if (self.root / state["directory"] / "confirmation.json").is_file():
                expired.append(True)
                current[0] = timing["deadline_at"] + 1
            return result
        with mock.patch.object(clock, "_now", side_effect=lambda value=None: current[0]), \
                mock.patch.object(self.requirements, "_verify_finished", side_effect=expire_after_scan):
            result, output = self.local("--confirm-functional", str(returned))
        self.assertTrue(expired)
        self.assertEqual(result, 2, output)
        self.assertIn("expired", output)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(self.functional_state()["status"], "WAITING_CONFIRMATION")
        self.assertEqual(self.kinds(), ["requirements", "functional"])

    def test_interrupted_functional_confirmation_reuses_exact_receipt(self):
        self.functional_pending()
        returned = self.returned_functional()
        state = self.functional_state()
        receipt = self.root / state["directory"] / "confirmation.json"
        write = self.requirements._write
        def interrupt(root, name, data, directory):
            if name == self.requirements.FUNCTIONAL_STATE and json.loads(data.decode("utf-8"))["status"] == "CONFIRMED":
                self.assertTrue(receipt.is_file())
                raise fixtures.AbruptStop()
            return write(root, name, data, directory)
        with mock.patch.object(self.requirements, "_write", side_effect=interrupt):
            with self.assertRaises(fixtures.AbruptStop):
                self.local("--confirm-functional", str(returned))
        saved = receipt.read_bytes()
        result = self.cli("--confirm-functional", str(returned))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(receipt.read_bytes(), saved)
        self.assertEqual(self.functional_state()["confirmation"], json.loads(saved)["confirmation"])
        self.assertEqual(self.kinds(), ["requirements", "functional"])
