"""Initial actual-answer publication survives process loss without resampling."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_boundary


CRASH = '''import json, os, sys
from pathlib import Path
root, answer, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.path.insert(0, str(root / ".harness"))
import clarify
original = clarify._atomic_runtime
replace = os.replace
def replace_then_stop(source, destination):
    result = replace(source, destination)
    if ((mode == "markdown" and Path(destination) == root / clarify.boundary.ANSWERS)
            or (mode == "json" and Path(destination) == root / clarify.boundary.STATE)):
        os._exit(91)
    return result
os.replace = replace_then_stop
def write(path, data, temporary=None):
    if mode == "partial" and path == root / clarify.boundary.STATE:
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(data[:37])
        os._exit(91)
    if mode == "intent" and path == root / clarify.ANSWER_PENDING:
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(data[:37])
        os._exit(91)
    result = original(path, data, temporary)
    if (mode == "completed" and path == root / clarify.ANSWER_PENDING
                and json.loads(data)["stage"] == "completed"):
        os._exit(91)
    return result
clarify._atomic_runtime = write
raise SystemExit(clarify.main(["--root", str(root), "--answer", "Q-001", "--answer-file", answer]))
'''


class AnswerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_boundary.BoundaryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.boundary = test_boundary.boundary
        state = self.boundary.create_state(self.root, "SPEC.md")
        self.boundary.complete_analysis(self.root, state, test_boundary.QUESTION)
        self.text = "Return unavailable without a numeric value."
        self.answer = self.fixture.base / "actual-answer.txt"
        self.answer.write_text(self.text, encoding="utf-8")
        loader = importlib.util.spec_from_file_location("initial_answer_clarify", self.root / ".harness/clarify.py")
        self.clarify = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(self.clarify)

    def crash(self, mode):
        script = self.fixture.base / "crash_answer.py"
        script.write_text(CRASH, encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(script), str(self.root), str(self.answer), mode],
            cwd=str(self.root), capture_output=True, text=True, encoding="utf-8", timeout=30,
            env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(result.returncode, 91, result.stdout + result.stderr)

    def submit(self, text=None):
        if text is not None:
            self.answer.write_text(text, encoding="utf-8")
        return self.fixture.cli("--answer", "Q-001", "--answer-file", str(self.answer))

    def assert_one_answer(self):
        state = self.boundary.load_state(self.root, current=False)
        self.assertEqual(state["answers"], [{"question_id": "Q-001", "text": self.text}])
        self.assertEqual(self.clarify._load_answer(self.root)["stage"], "completed")
        self.assertEqual(self.fixture.calls(), [])
        return state

    def test_actual_exit_between_markdown_and_json_recovers_without_duplicate(self):
        self.crash("markdown")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.boundary.load_state(self.root)
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.assert_one_answer()["status"], "needs_analysis")
        self.assertEqual(self.submit().returncode, 0)
        self.assert_one_answer()

    def test_actual_exit_after_json_before_completion_recovers(self):
        self.crash("json")
        self.assertEqual(self.clarify._load_answer(self.root)["stage"], "publishing")
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_one_answer()

    def test_actual_exit_after_completed_journal_reuses_exact_submission(self):
        self.crash("completed")
        self.assertEqual(self.clarify._load_answer(self.root)["stage"], "completed")
        self.assertEqual(self.submit().returncode, 0)
        self.assert_one_answer()

    def test_actual_exit_with_partial_json_temporary_recovers(self):
        self.crash("partial")
        temporary = self.root / self.clarify.ANSWER_DIRECTORY / "boundary.json.tmp"
        self.assertEqual(temporary.stat().st_size, 37)
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(temporary.exists())
        self.assert_one_answer()

    def test_partial_intent_has_not_changed_boundary_and_same_retry_recovers(self):
        before = (self.root / self.boundary.STATE).read_bytes()
        self.crash("intent")
        self.assertIsNone(self.clarify._load_answer(self.root))
        self.assertEqual((self.root / self.boundary.STATE).read_bytes(), before)
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_one_answer()

    def test_status_recovers_without_config_or_any_model_call(self):
        self.crash("markdown")
        (self.root / "config.json").unlink()
        result = self.fixture.cli("--status")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Boundary status: needs_analysis", result.stdout)
        self.assertNotIn("differs", result.stderr)
        self.assert_one_answer()

    def test_reanalysis_recovers_answer_and_runs_only_requested_analysis(self):
        self.crash("markdown")
        self.fixture.scenario(test_boundary.EMPTY)
        result = self.fixture.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.boundary.require_ready(self.root)
        self.assertEqual(state["answered_question_ids"], ["Q-001"])
        self.assertEqual(len(self.fixture.calls()), 1)
        self.assertEqual(len(self.boundary.load_state(self.root)["answers"]), 1)

    def test_unexpected_user_edit_is_not_overwritten(self):
        self.crash("markdown")
        path = self.root / self.boundary.ANSWERS
        path.write_text("Operator edit must remain", encoding="utf-8")
        before = (self.root / self.boundary.STATE).read_bytes()
        result = self.submit()
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unrelated boundary change", result.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), "Operator edit must remain")
        self.assertEqual((self.root / self.boundary.STATE).read_bytes(), before)
        self.assertEqual(self.fixture.calls(), [])

    def test_pending_journal_tampering_is_rejected(self):
        self.crash("markdown")
        path = self.root / self.clarify.ANSWER_PENDING
        value = json.loads(path.read_text(encoding="utf-8"))
        value["answer_text"] = "Invented answer"
        path.write_text(json.dumps(value), encoding="utf-8")
        before = (self.root / self.boundary.STATE).read_bytes()
        result = self.submit()
        self.assertEqual(result.returncode, 2)
        self.assertIn("checksum", result.stderr)
        self.assertEqual((self.root / self.boundary.STATE).read_bytes(), before)

    def test_distinct_answer_and_post_analysis_resubmission_remain_revisions(self):
        self.assertEqual(self.submit().returncode, 0)
        replacement = "Return an explicit error instead."
        self.assertEqual(self.submit(replacement).returncode, 0)
        state = self.boundary.load_state(self.root)
        self.assertEqual([r["text"] for r in state["answers"]], [self.text, replacement])
        self.assertEqual(self.submit(replacement).returncode, 0)
        self.assertEqual(len(self.boundary.load_state(self.root)["answers"]), 2)
        self.fixture.scenario(test_boundary.EMPTY)
        result = self.fixture.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.submit(replacement).returncode, 0)
        state = self.boundary.load_state(self.root)
        self.assertEqual(state["status"], "needs_analysis")
        self.assertEqual(len(state["answers"]), 3)


if __name__ == "__main__":
    unittest.main()
