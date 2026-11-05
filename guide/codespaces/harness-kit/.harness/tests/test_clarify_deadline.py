import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
loader = importlib.util.spec_from_file_location("test_deadline_clarify", SOURCE / "clarify.py")
clarify = importlib.util.module_from_spec(loader)
loader.loader.exec_module(clarify)
clock, boundary = clarify.problem_time, clarify.boundary
EMPTY = {"version": 1, "questions": [], "unresolved": [], "references": []}
QUESTION = {"version": 1, "questions": [{"template": "T03", "question": "What response is required when no value exists?",
    "decides": "Missing value response", "follow_up_to": None}], "unresolved": ["Missing response"], "references": []}
FAKE = '''
import json, sys, time
from pathlib import Path
scenario = json.loads(Path(__file__).with_suffix('.json').read_text(encoding='utf-8'))
with Path(__file__).with_suffix('.calls').open('a') as stream:
    stream.write('call\\n')
sys.stdin.read()
time.sleep(scenario.get('sleep', 0))
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps(scenario['report']), encoding='utf-8')
'''


class ClarifyDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clarify-clock-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "SPEC.md").write_text("Return measured values; missing response is unspecified.", encoding="utf-8")
        (self.root / boundary.TEMPLATE).write_text("Use T01 through T15.", encoding="utf-8")
        self.fake = self.base / "fake.py"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.config = {"project": ".", "spec": "SPEC.md", "model": "fake",
                       "codex_command": [sys.executable, str(self.fake)], "problem_time_limit_seconds": 9000}
        config_file = self.root / "config.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps(self.config), encoding="utf-8")
        self.scenario(EMPTY)
        self.output = io.StringIO()
        self.now = time.time()

    def scenario(self, report, **kwargs):
        self.fake.with_suffix(".json").write_text(json.dumps(dict(report=report, **kwargs)), encoding="utf-8")

    def analyze(self, **kwargs):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return clarify.analyze(self.root, "SPEC.md", self.config, "fake", 30, **kwargs)

    def cli(self, *args):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return clarify.main(["--root", str(self.root)] + list(args))

    def calls(self):
        path = self.fake.with_suffix(".calls")
        return len(path.read_text().splitlines()) if path.exists() else 0

    def test_required_receipt_missing_does_not_implicitly_start_or_launch(self):
        with self.assertRaisesRegex(clock.DeadlineError, "Missing"):
            self.analyze()
        self.assertFalse((self.root / clock.RECEIPT).exists())
        self.assertFalse((self.root / boundary.STATE).exists())
        self.assertEqual(self.calls(), 0)

    def test_existing_receipt_enforced_even_without_config_flag(self):
        clock.begin(self.root, now=self.now)
        self.config.pop("problem_time_limit_seconds")
        with mock.patch.object(clock, "_now", return_value=self.now + 9000), self.assertRaisesRegex(clock.DeadlineError, "expired"):
            self.analyze()
        self.assertEqual(self.calls(), 0)

    def test_staged_analysis_uses_origin_clock_without_copying_receipt(self):
        origin = self.base / "origin"
        origin.mkdir()
        receipt = clock.begin(origin, now=self.now)
        with mock.patch.object(clock, "_now", return_value=receipt['deadline_at']), self.assertRaisesRegex(clock.DeadlineError, "expired"):
            self.analyze(budget_root=origin)
        self.assertFalse((self.root / clock.RECEIPT).exists())
        self.assertEqual(self.calls(), 0)

    def test_ready_check_rejected_after_wait_while_status_is_read_only(self):
        clock.begin(self.root, now=self.now)
        with mock.patch.object(clock, "_now", return_value=self.now + 1):
            self.assertEqual(self.analyze(), 0)
        state_before = (self.root / boundary.STATE).read_bytes()
        with mock.patch.object(clock, "_now", return_value=self.now + 9000), \
                mock.patch.object(clarify, "recover_analysis", side_effect=AssertionError("must not recover")):
            self.assertEqual(self.cli("--check-ready"), 2)
            self.assertEqual(self.cli("--status"), 0)
        self.assertEqual((self.root / boundary.STATE).read_bytes(), state_before)
        self.assertEqual(self.calls(), 1)

    def test_submitted_answer_survives_deadline_but_reanalysis_never_launches(self):
        self.scenario(QUESTION)
        clock.begin(self.root, now=self.now)
        with mock.patch.object(clock, "_now", return_value=self.now + 1):
            self.assertEqual(self.analyze(), 2)
        answer = self.base / "answer.txt"
        answer.write_text("Return unavailable without a numeric value.", encoding="utf-8")
        with mock.patch.object(clock, "_now", return_value=self.now + 9000):
            self.assertEqual(self.cli("--answer", "Q-001", "--answer-file", str(answer)), 0)
            self.assertEqual(self.cli("--check-ready"), 2)
            with self.assertRaisesRegex(clock.DeadlineError, "expired"):
                self.analyze()
        state = boundary.load_state(self.root)
        self.assertEqual(state["status"], "needs_analysis")
        self.assertEqual(state["answers"][-1]["text"], answer.read_text(encoding="utf-8"))
        self.assertEqual(self.calls(), 1)

    def test_expired_status_reads_saved_json_without_repairing_partial_markdown(self):
        clock.begin(self.root, now=self.now)
        boundary.complete_analysis(self.root, boundary.create_state(self.root, "SPEC.md"), QUESTION)
        (self.root / boundary.ANSWERS).write_text("Interrupted Markdown publication", encoding="utf-8")
        before = {name: (self.root / name).read_bytes() for name in (boundary.STATE, boundary.ANSWERS)}
        with mock.patch.object(clock, "_now", return_value=self.now + 9000):
            self.assertEqual(self.cli("--status"), 0)
        self.assertIn("without recovery or ready approval", self.output.getvalue())
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})
        self.assertEqual(self.calls(), 0)

    def test_valid_response_journal_is_preserved_without_publishing_ready_after_expiry(self):
        clock.begin(self.root, now=self.now)
        current = [self.now + 1]
        prepare = clarify._prepare_result
        def expire_after_recording(*args):
            prepare(*args)
            current[0] = self.now + 9000
        with mock.patch.object(clock, "_now", side_effect=lambda value=None: current[0]), \
                mock.patch.object(clarify, "_prepare_result", side_effect=expire_after_recording):
            with self.assertRaisesRegex(clock.DeadlineError, "expired"):
                self.analyze()
            journal_before = (self.root / clarify.ANALYSIS_PENDING).read_bytes()
            self.assertEqual(json.loads(journal_before)["stage"], "publishing")
            self.assertEqual(json.loads(journal_before)["report"], EMPTY)
            self.assertEqual(boundary.load_state(self.root)["status"], "analyzing")
            self.assertEqual(self.cli("--status"), 0)
            with self.assertRaisesRegex(clock.DeadlineError, "expired"):
                self.analyze()
            self.assertEqual((self.root / clarify.ANALYSIS_PENDING).read_bytes(), journal_before)
        self.assertEqual(self.calls(), 1)

    def test_short_real_process_is_stopped_at_remaining_deadline(self):
        self.scenario(EMPTY, sleep=10)
        clock.begin(self.root, now=self.now)
        launched = []
        launched_at = []
        original = clarify.start_process
        def start(*args, **kwargs):
            process = original(*args, **kwargs)
            launched.append(process)
            launched_at.append(time.monotonic())
            return process
        def current(value=None):
            return self.now if not launched_at else self.now + 8999.3 + time.monotonic() - launched_at[0]
        with mock.patch.object(clarify, "start_process", side_effect=start), \
                mock.patch.object(clock, "_now", side_effect=current), self.assertRaisesRegex(clock.DeadlineError, "expired"):
            self.analyze()
        self.assertEqual(len(launched), 1)
        self.assertLess(time.monotonic() - launched_at[0], 5)
        self.assertIsNotNone(launched[0].poll())
        journal = json.loads((self.root / clarify.ANALYSIS_PENDING).read_text(encoding="utf-8"))
        outcome = json.loads((self.root / journal["directory"] / "model.exit.json").read_text(encoding="utf-8"))
        self.assertTrue(outcome["cleanup"]["completed"])
        self.assertIn(outcome["termination_reason"], ("timed_out", "guard_failed"))
        self.assertNotEqual(boundary.load_state(self.root)["status"], "ready")


if __name__ == "__main__":
    unittest.main()
