"""Requirements intake with real local worker processes, never a live model."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

import test_boundary


SOURCE = Path(__file__).resolve().parents[1]
DOCUMENT = "# Confirmed requirements\n\nReturn measured values. Missing values are unavailable.\n"
FUNCTIONAL = "# Functional specification\n\nF-01: Return measured values and report unavailable measurements.\n"
QUESTIONS = dict(test_boundary.QUESTION, questions=[
    test_boundary.QUESTION["questions"][0],
    {"template": "T06", "question": "How many measurements may a request contain?",
     "decides": "Maximum request size", "follow_up_to": None}],
    unresolved=["Missing response", "Maximum request size"])
FAKE = r'''
import json, sys
from pathlib import Path
scenario = json.loads(Path(__file__).with_suffix('.json').read_text(encoding='utf-8'))
arguments = sys.argv[1:]
prompt = sys.stdin.read()
schema = Path(arguments[arguments.index('--output-schema') + 1]).name
kind = {'requirements.schema.json': 'requirements', 'functional.schema.json': 'functional'}.get(schema, 'clarify')
with Path(__file__).with_suffix('.calls.jsonl').open('a', encoding='utf-8') as stream:
    stream.write(json.dumps({'kind': kind, 'argv': arguments, 'prompt': prompt}) + '\n')
if scenario.get('mutate') == kind:
    Path('app.py').write_text('unauthorized worker edit', encoding='utf-8')
if scenario.get('mutate') == 'analysis_pointer' and kind == 'requirements':
    pointer = Path('.harness/work/analysis-pending.json')
    assert pointer.is_file(), 'fixture must reach a completed clarification first'
    pointer.write_text('{"forged": true}', encoding='utf-8')
result = ({'requirements_markdown': scenario['markdown']} if kind == 'requirements' else
          {'functional_spec_markdown': scenario['functional']} if kind == 'functional' else scenario['report'])
Path(arguments[arguments.index('--output-last-message') + 1]).write_text(json.dumps(result), encoding='utf-8')
print('offline requirements fixture')
raise SystemExit(scenario.get(kind + '_exit', 0))
'''


class AbruptStop(BaseException):
    pass


class RequirementsFixture:
    def setUp(self):
        self.fixture = test_boundary.BoundaryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.base = self.fixture.root, self.fixture.base
        for name in ("requirements.py", "requirements.schema.json", "requirements_prompt.md",
                     "functional.schema.json", "functional_prompt.md"):
            shutil.copy2(SOURCE / name, self.root / ".harness" / name)
        self.fixture.fake.write_text(FAKE, encoding="utf-8")
        loader = importlib.util.spec_from_file_location("fixture_requirements", self.root / ".harness/requirements.py")
        self.requirements = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(self.requirements)
        self.scenario()

    def scenario(self, report=None, markdown=DOCUMENT, functional=FUNCTIONAL, **options):
        self.fixture.fake.with_suffix(".json").write_text(json.dumps(dict(
            report=test_boundary.EMPTY if report is None else report, markdown=markdown,
            functional=functional, **options)), encoding="utf-8")

    def cli(self, *arguments):
        return subprocess.run([sys.executable, "-B", str(self.root / ".harness/requirements.py"),
            "--root", str(self.root), *arguments], cwd=str(self.root), capture_output=True,
            text=True, encoding="utf-8", timeout=45,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"))

    def local(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = self.requirements.main(["--root", str(self.root)] + list(arguments))
        return result, output.getvalue()

    def calls(self):
        return self.fixture.calls()

    def kinds(self):
        return [call["kind"] for call in self.calls()]

    def state(self, current=True, stage="requirements"):
        return self.requirements.load(self.root, current=current, stage=stage)

    def document(self):
        return self.root / self.requirements.DOCUMENT

    def answer(self, question, text):
        path = self.base / (question + " answer 한글.txt")
        path.write_text(text, encoding="utf-8")
        return self.cli("--answer", question, "--answer-file", str(path))

    def pending(self):
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.state()["status"], "WAITING_CONFIRMATION")
        return result

    def returned_document(self):
        path = self.base / "operator returned requirements 한글.md"
        path.write_bytes(self.document().read_bytes())
        return path

    def assert_no_implementation(self):
        for name in ("automation/codex-loop/tasks.json", ".harness/acceptance.json", "app.py"):
            self.assertFalse((self.root / name).exists(), name)
        self.assertTrue(all(kind in ("clarify", "requirements", "functional") for kind in self.kinds()))

    def ready_boundary(self):
        boundary = self.fixture_module_boundary()
        boundary.complete_analysis(self.root, boundary.create_state(self.root, "SPEC.md"), test_boundary.EMPTY)

    def fixture_module_boundary(self):
        return self.requirements.boundary


class RequirementsTests(RequirementsFixture, unittest.TestCase):
    def test_multiple_answers_wait_then_last_answer_generates_document_and_stops(self):
        self.scenario(QUESTIONS)
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.kinds(), ["clarify"])
        self.assertFalse(self.document().exists())
        self.assertEqual(self.cli().returncode, 3)
        status = self.cli("--status")
        self.assertEqual(status.returncode, 0, status.stderr)
        view = json.loads(status.stdout)
        self.assertEqual(view["status"], "WAITING_ANSWERS")
        self.assertEqual(view["unanswered_question_ids"], ["Q-001", "Q-002"])
        first = self.answer("Q-001", "Return unavailable without a numeric value.")
        self.assertEqual(first.returncode, 3, first.stdout + first.stderr)
        self.assertEqual(self.kinds(), ["clarify"])
        boundary = self.fixture_module_boundary().load_state(self.root)
        self.assertEqual(boundary["answers"], [{"question_id": "Q-001", "text": "Return unavailable without a numeric value."}])
        self.scenario(markdown=DOCUMENT + "At most 20 measurements per request.\n")
        last = self.answer("Q-002", "At most 20 measurements per request.")
        self.assertEqual(last.returncode, 3, last.stdout + last.stderr)
        self.assertEqual(self.kinds(), ["clarify", "clarify", "requirements"])
        self.assertEqual(self.state()["status"], "WAITING_CONFIRMATION")
        self.assertIn("At most 20 measurements per request.", self.document().read_text(encoding="utf-8"))
        for call in self.calls()[1:]:
            self.assertIn("Return unavailable without a numeric value.", call["prompt"])
            self.assertIn("At most 20 measurements per request.", call["prompt"])
        state_path = self.root / self.requirements.STATE
        pending = state_path.read_bytes()
        again = self.answer("Q-002", "At most 20 measurements per request.")
        self.assertEqual(again.returncode, 3, again.stdout + again.stderr)
        self.assertEqual(state_path.read_bytes(), pending)
        returned = self.returned_document()
        confirmed = self.cli("--confirm-file", str(returned))
        self.assertEqual(confirmed.returncode, 3, confirmed.stdout + confirmed.stderr)
        saved = state_path.read_bytes()
        again = self.answer("Q-002", "At most 20 measurements per request.")
        self.assertEqual(again.returncode, 3, again.stdout + again.stderr)
        self.assertEqual(state_path.read_bytes(), saved)
        self.assertEqual(self.kinds(), ["clarify", "clarify", "requirements", "functional"])
        functional_path = self.root / self.requirements.FUNCTIONAL_DOCUMENT
        returned_functional = self.base / "returned functional.md"
        returned_functional.write_bytes(functional_path.read_bytes())
        confirmed = self.cli("--confirm-functional", str(returned_functional))
        self.assertEqual(confirmed.returncode, 0, confirmed.stdout + confirmed.stderr)
        functional_state = self.root / self.requirements.FUNCTIONAL_STATE
        final_bytes = functional_state.read_bytes()
        again = self.answer("Q-002", "At most 20 measurements per request.")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(state_path.read_bytes(), saved)
        self.assertEqual(functional_state.read_bytes(), final_bytes)
        self.assertEqual(self.kinds(), ["clarify", "clarify", "requirements", "functional"])
        self.assert_no_implementation()

    def test_no_questions_and_identical_reruns_need_no_more_models_or_automatic_confirmation(self):
        self.pending()
        self.assertEqual(self.kinds(), ["clarify", "requirements"])
        before = self.document().read_bytes()
        self.assertEqual(self.cli().returncode, 3)
        self.assertEqual(self.cli("--status").returncode, 0)
        self.assertEqual(self.document().read_bytes(), before)
        self.assertEqual(self.state()["status"], "WAITING_CONFIRMATION")
        returned = self.returned_document()
        result = self.cli("--confirm-file", str(returned))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.state()["status"], "CONFIRMED")
        self.assertEqual(self.state(stage="functional")["status"], "WAITING_CONFIRMATION")
        self.assertEqual(self.cli().returncode, 3)
        self.assertEqual(self.cli("--status").returncode, 0)
        self.assertEqual(self.kinds(), ["clarify", "requirements", "functional"])
        for call in self.calls():
            self.assertEqual(call["argv"][call["argv"].index("--sandbox") + 1], "read-only")
        self.assert_no_implementation()

    def test_changed_returned_document_cannot_confirm_or_replace_generated_document(self):
        self.pending()
        before = self.document().read_bytes()
        returned = self.returned_document()
        returned.write_bytes(before + b"\nUnagreed extra requirement.\n")
        result = self.cli("--confirm-file", str(returned))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.state()["status"], "WAITING_CONFIRMATION")
        self.assertEqual(self.document().read_bytes(), before)
        self.assertEqual(self.kinds(), ["clarify", "requirements"])

    def test_internal_answer_and_confirmation_files_are_refused(self):
        self.scenario(test_boundary.QUESTION)
        first = self.cli()
        self.assertEqual(first.returncode, 3, first.stdout + first.stderr)
        result = self.cli("--answer", "Q-001", "--answer-file", str(self.root / "SPEC.md"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("outside", result.stderr.lower())
        self.assertEqual(self.fixture_module_boundary().load_state(self.root)["answers"], [])
        self.assertEqual(self.kinds(), ["clarify"])
        self.scenario()
        result = self.answer("Q-001", "Return unavailable without a numeric value.")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        result = self.cli("--confirm-file", str(self.document()))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("outside", result.stderr.lower())
        self.assertEqual(self.state()["status"], "WAITING_CONFIRMATION")
        self.assertEqual(self.kinds(), ["clarify", "clarify", "requirements"])

    def test_selected_spec_cannot_confirm_or_reuse_another_specs_document(self):
        self.fixture.write("B.md", "A different problem that returns event counts.\n")
        self.pending()
        document = self.document().read_bytes()
        state_path = self.root / self.requirements.STATE
        before = state_path.read_bytes()
        returned = self.returned_document()
        for arguments in (("--spec", "B.md", "--confirm-file", str(returned)), ("--spec", "B.md")):
            with self.subTest(arguments=arguments):
                result = self.cli(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.document().read_bytes(), document)
                self.assertEqual(state_path.read_bytes(), before)
                self.assertEqual(self.kinds(), ["clarify", "requirements"])

    def test_existing_document_and_spec_output_collision_are_preserved_without_model(self):
        self.document().write_text("Existing operator document", encoding="utf-8")
        for args in ((), ("--spec", self.requirements.DOCUMENT)):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.document().read_text(encoding="utf-8"), "Existing operator document")
                self.assertEqual(self.calls(), [])

    def test_input_change_invalidates_confirmation_and_old_returned_document(self):
        self.pending()
        revision = self.state()["revision"]
        old = self.returned_document()
        self.assertEqual(self.cli("--confirm-file", str(old)).returncode, 3)
        self.fixture.write("SPEC.md", "Return measured values and their timestamps.\n")
        result = self.cli("--confirm-file", str(old))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        with self.assertRaises(ValueError):
            self.state()
        self.assertEqual(self.kinds(), ["clarify", "requirements", "functional"])
        self.scenario(markdown=DOCUMENT + "Return the measurement timestamp.\n")
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.state()["revision"], revision + 1)
        self.assertEqual(self.cli("--confirm-file", str(old)).returncode, 2)
        current = self.returned_document()
        result = self.cli("--confirm-file", str(current))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.kinds(), ["clarify", "requirements", "functional", "clarify", "requirements", "functional"])

    def test_document_tampering_is_not_repaired_or_adopted_on_rerun(self):
        self.pending()
        self.document().write_text("Manual replacement", encoding="utf-8")
        result = self.cli()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.document().read_text(encoding="utf-8"), "Manual replacement")
        self.assertEqual(self.kinds(), ["clarify", "requirements"])

    def test_worker_source_mutation_never_publishes_a_document(self):
        self.ready_boundary()
        self.scenario(mutate="requirements")
        result = self.cli()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(self.document().exists())
        self.assertEqual(self.kinds(), ["requirements"])

    def test_forged_confirmed_state_is_rejected_without_model_or_document_change(self):
        self.pending()
        path = self.root / self.requirements.STATE
        value = json.loads(path.read_text(encoding="utf-8"))
        value["status"] = "CONFIRMED"
        path.write_text(json.dumps(value), encoding="utf-8")
        document = self.document().read_bytes()
        result = self.cli("--status")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.document().read_bytes(), document)
        self.assertEqual(self.kinds(), ["clarify", "requirements"])

    def test_resigned_invalid_paths_and_document_hash_are_rejected(self):
        self.pending()
        path = self.root / self.requirements.STATE
        original = self.state()
        document = self.document().read_bytes()
        outside = self.base / "outside.md"
        outside.write_text("Preserve operator file", encoding="utf-8")
        for replacement in ({"directory": "../outside"},
                            {"input_hashes": {"../outside.md": "a" * 64}},
                            {"document_sha256": "a" * 64}):
            with self.subTest(replacement=replacement):
                path.write_bytes(self.requirements._signed(dict(original, **replacement)))
                result = self.cli("--status")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.document().read_bytes(), document)
                self.assertEqual(outside.read_text(encoding="utf-8"), "Preserve operator file")
                self.assertEqual(self.kinds(), ["clarify", "requirements"])

    def test_expired_confirmation_is_refused_and_status_still_calls_no_model(self):
        self.pending()
        returned = self.returned_document()
        clock = self.requirements.problem_time
        timing = clock.load(self.root, required=True)
        state_path = self.root / self.requirements.STATE
        before = state_path.read_bytes()
        with mock.patch.object(clock, "_now", return_value=timing["deadline_at"] + 1):
            result, output = self.local("--confirm-file", str(returned))
            self.assertEqual(result, 2, output)
            self.assertIn("expired", output)
            result, output = self.local("--status")
            self.assertEqual(result, 0, output)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(self.state()["status"], "WAITING_CONFIRMATION")
        self.assertEqual(self.kinds(), ["clarify", "requirements"])

    def test_deadline_expiring_during_final_confirmation_scan_cannot_publish_confirmed(self):
        self.pending()
        returned = self.returned_document()
        state = self.state()
        state_path = self.root / self.requirements.STATE
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
            result, output = self.local("--confirm-file", str(returned))
        self.assertTrue(expired, "fixture must expire only after the final full evidence scan")
        self.assertEqual(result, 2, output)
        self.assertIn("expired", output)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(self.state()["status"], "WAITING_CONFIRMATION")
        self.assertEqual(self.kinds(), ["clarify", "requirements"])

    def test_interrupted_confirmation_reuses_saved_receipt_without_model(self):
        self.pending()
        returned = self.returned_document()
        state = self.state()
        state_path = self.root / self.requirements.STATE
        before = state_path.read_bytes()
        receipt = self.root / state["directory"] / "confirmation.json"
        write = self.requirements._write
        stopped = []
        def interrupt(root, name, data, directory):
            if name == self.requirements.STATE and json.loads(data.decode("utf-8"))["status"] == "CONFIRMED":
                self.assertTrue(receipt.is_file())
                stopped.append(True)
                raise AbruptStop()
            return write(root, name, data, directory)
        with mock.patch.object(self.requirements, "_write", side_effect=interrupt):
            with self.assertRaises(AbruptStop):
                self.local("--confirm-file", str(returned))
        self.assertEqual(stopped, [True])
        self.assertEqual(state_path.read_bytes(), before)
        saved_receipt = receipt.read_bytes()
        result = self.cli("--confirm-file", str(returned))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(receipt.read_bytes(), saved_receipt)
        self.assertEqual(self.state()["status"], "CONFIRMED")
        saved = json.loads(saved_receipt.decode("utf-8"))
        self.assertEqual(self.state()["confirmation"], saved["confirmation"])
        self.assertEqual(self.kinds(), ["clarify", "requirements", "functional"])

    def test_worker_cannot_change_the_nonowned_clarification_journal(self):
        self.scenario(mutate="analysis_pointer")
        result = self.cli()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("source changed", result.stderr.lower())
        self.assertFalse(self.document().exists())
        self.assertEqual(self.kinds(), ["clarify", "requirements"])
        pointer = self.root / self.requirements.clarify.ANALYSIS_PENDING
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), {"forged": True})

    def test_successful_worker_exit_before_validation_recovers_without_relaunch(self):
        self.ready_boundary()
        execute = self.requirements.lifecycle.execute
        def stop_after_success(*args, **kwargs):
            result = execute(*args, **kwargs)
            self.assertEqual(result, 0)
            raise AbruptStop()
        with mock.patch.object(self.requirements.lifecycle, "execute", side_effect=stop_after_success):
            with self.assertRaises(AbruptStop):
                self.local()
        self.assertEqual(self.kinds(), ["requirements"])
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.state()["status"], "WAITING_CONFIRMATION")
        self.assertEqual(self.kinds(), ["requirements"])

    def stop_after_prepared_result(self):
        self.ready_boundary()
        prepare = self.requirements._prepare_result
        reached = []
        def stop(*args, **kwargs):
            result = prepare(*args, **kwargs)
            reached.append(True)
            raise AbruptStop()
        with mock.patch.object(self.requirements, "_prepare_result", side_effect=stop):
            with self.assertRaises(AbruptStop):
                self.local()
        self.assertEqual(reached, [True])
        self.assertEqual(self.kinds(), ["requirements"])

    def test_validated_result_before_publication_recovers_without_relaunch(self):
        self.stop_after_prepared_result()
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.state()["status"], "WAITING_CONFIRMATION")
        self.assertIn(DOCUMENT, self.document().read_text(encoding="utf-8"))
        self.assertEqual(self.kinds(), ["requirements"])

    def test_actual_process_exit_after_markdown_publication_recovers_remaining_state(self):
        self.ready_boundary()
        script = self.base / "crash_requirements.py"
        script.write_text('''import importlib.util, os, sys
from pathlib import Path
root = Path(sys.argv[1])
loader = importlib.util.spec_from_file_location("crash_requirements", root / ".harness/requirements.py")
module = importlib.util.module_from_spec(loader)
loader.loader.exec_module(module)
original = os.replace
def replace(source, destination):
    result = original(source, destination)
    if Path(destination) == root / module.DOCUMENT:
        os._exit(86)
    return result
os.replace = replace
raise SystemExit(module.main(["--root", str(root)]))
''', encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(script), str(self.root)],
            cwd=str(self.root), capture_output=True, text=True, encoding="utf-8", timeout=45)
        self.assertEqual(result.returncode, 86, result.stdout + result.stderr)
        document = self.document().read_bytes()
        self.assertIn(DOCUMENT.encode("utf-8"), document)
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.document().read_bytes(), document)
        self.assertEqual(self.state()["status"], "WAITING_CONFIRMATION")
        self.assertEqual(self.kinds(), ["requirements"])

    def test_missing_completed_execution_evidence_never_relaunches_automatically(self):
        self.stop_after_prepared_result()
        outcomes = list((self.root / ".harness/work").glob("requirements-*/model.exit.json"))
        self.assertEqual(len(outcomes), 1)
        outcomes[0].unlink()
        result = self.cli()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(self.document().exists())
        self.assertEqual(self.kinds(), ["requirements"])

    def test_failed_worker_keeps_evidence_and_retries_once_per_invocation(self):
        self.ready_boundary()
        self.scenario(requirements_exit=7)
        result = self.cli()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        outcomes = list((self.root / ".harness/work").glob("requirements-*/model.exit.json"))
        self.assertEqual(len(outcomes), 1)
        outcome = json.loads(outcomes[0].read_text(encoding="utf-8"))
        self.assertEqual(outcome["exit_code"], 7)
        self.assertTrue(outcome["cleanup"]["completed"])
        self.assertFalse(self.document().exists())
        self.assertEqual(self.kinds(), ["requirements"])
        original = outcomes[0].read_bytes()
        repeated = self.cli()
        self.assertEqual(repeated.returncode, 2, repeated.stdout + repeated.stderr)
        self.assertEqual(self.kinds(), ["requirements", "requirements"])
        self.assertEqual(self.state(current=False)["revision"], 2)
        self.scenario()
        completed = self.cli()
        self.assertEqual(completed.returncode, 3, completed.stdout + completed.stderr)
        self.assertEqual(self.state()["revision"], 3)
        self.assertEqual(self.state()["status"], "WAITING_CONFIRMATION")
        self.assertEqual(outcomes[0].read_bytes(), original)

