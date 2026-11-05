"""Analysis response and two-file publication recovery with a local model fixture."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import test_boundary


class AbruptStop(BaseException):
    pass


class AnalysisRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_boundary.BoundaryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        loader = importlib.util.spec_from_file_location("recovery_clarify", self.root / ".harness/clarify.py")
        self.clarify = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(self.clarify)
        self.boundary = self.clarify.boundary
        state = self.boundary.create_state(self.root, "SPEC.md")
        self.boundary.complete_analysis(self.root, state, test_boundary.QUESTION)
        self.boundary.submit_answer(self.root, "Q-001", "Return unavailable without a numeric value.")
        self.followup = {"version": 1, "questions": [{"template": "T12", "question": "Should the unavailable response include a timestamp?",
                         "decides": "Missing measurement timestamp", "follow_up_to": "Q-001"}],
                         "unresolved": ["Timestamp remains unclear"], "references": []}
        self.fixture.scenario(self.followup)
        self.context = {"run_id": "run-1", "request_id": "decision-1", "question_id": "Q-001",
                        "answer_sha256": self.boundary.json_digest("Return unavailable without a numeric value.")}

    def analyze(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.clarify.analyze(self.root, "SPEC.md", self.fixture.config, "fake-analysis", 30,
                                        analysis_context=self.context)

    def assert_recovered_once(self):
        self.assertEqual(self.analyze(), 2)
        state = self.boundary.load_state(self.root)
        self.assertEqual(state["status"], "needs_answers")
        self.assertEqual([q["id"] for q in state["questions"]], ["Q-001", "Q-002"])
        self.assertEqual(state["answers"], [{"question_id": "Q-001", "text": "Return unavailable without a numeric value."}])
        self.assertEqual(len(self.fixture.calls()), 1)
        self.assertEqual(self.clarify._load_analysis(self.root)["stage"], "completed")
        self.clarify.list_analysis_files(self.root)

    def stop_when_preparing(self, after=False):
        original = self.clarify._save_analysis
        stopped = []
        def save(root, journal):
            if journal["stage"] == "publishing" and not stopped:
                stopped.append(True)
                if after:
                    original(root, journal)
                raise AbruptStop()
            return original(root, journal)
        with mock.patch.object(self.clarify, "_save_analysis", side_effect=save):
            with self.assertRaises(AbruptStop):
                self.analyze()
        self.assertEqual(len(self.fixture.calls()), 1)

    def test_response_before_prepared_journal_reuses_completed_execution(self):
        self.stop_when_preparing()
        self.assertEqual(self.clarify._load_analysis(self.root)["stage"], "running")
        self.assert_recovered_once()

    def test_prepared_journal_before_publication_reuses_validated_target(self):
        self.stop_when_preparing(after=True)
        self.assertEqual(self.clarify._load_analysis(self.root)["stage"], "publishing")
        self.assert_recovered_once()

    def stop_at_file(self, name, after=False, partial_temporary=False):
        original = self.clarify._atomic_runtime
        stopped = []
        def write(path, data, temporary=None):
            journal = self.clarify._load_analysis(self.root)
            if path == self.root / name and journal and journal["stage"] == "publishing" and not stopped:
                stopped.append(True)
                if partial_temporary:
                    temporary.write_bytes(data[:37])
                elif after:
                    original(path, data, temporary)
                raise AbruptStop()
            return original(path, data, temporary)
        with mock.patch.object(self.clarify, "_atomic_runtime", side_effect=write):
            with self.assertRaises(AbruptStop):
                self.analyze()
        self.assertTrue(stopped)

    def test_before_markdown_write(self):
        self.stop_at_file(self.boundary.ANSWERS)
        self.assert_recovered_once()

    def test_after_markdown_before_json(self):
        self.stop_at_file(self.boundary.ANSWERS, after=True)
        with self.assertRaisesRegex(ValueError, "differs"):
            self.boundary.load_state(self.root)
        self.assert_recovered_once()

    def test_before_json_write(self):
        self.stop_at_file(self.boundary.STATE)
        self.assert_recovered_once()

    def test_after_json_before_completion(self):
        self.stop_at_file(self.boundary.STATE, after=True)
        self.assert_recovered_once()

    def test_partial_temporary_file_survives_abrupt_process_exit(self):
        self.stop_at_file(self.boundary.STATE, partial_temporary=True)
        self.assert_recovered_once()

    def test_completion_history_and_publication_boundaries(self):
        original = self.clarify._save_analysis
        stopped = []
        def save(root, journal):
            if journal["stage"] == "completed" and not stopped:
                stopped.append(True)
                raise AbruptStop()
            return original(root, journal)
        with mock.patch.object(self.clarify, "_save_analysis", side_effect=save):
            with self.assertRaises(AbruptStop):
                self.analyze()
        self.assert_recovered_once()

    def test_completed_pointer_before_caller_acknowledgment_does_not_resample(self):
        original = self.clarify._save_analysis
        def save(root, journal):
            original(root, journal)
            if journal["stage"] == "completed":
                raise AbruptStop()
        with mock.patch.object(self.clarify, "_save_analysis", side_effect=save):
            with self.assertRaises(AbruptStop):
                self.analyze()
        self.assert_recovered_once()

    def test_unrelated_boundary_change_is_refused_without_overwriting(self):
        self.stop_when_preparing(after=True)
        path = self.root / self.boundary.ANSWERS
        path.write_text("Unexpected operator edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unrelated boundary"):
            self.analyze()
        self.assertEqual(path.read_text(encoding="utf-8"), "Unexpected operator edit")
        self.assertEqual(len(self.fixture.calls()), 1)

    def test_request_or_source_change_cannot_adopt_an_old_result(self):
        self.stop_when_preparing()
        original = dict(self.context)
        self.context["request_id"] = "different-request"
        with self.assertRaisesRegex(ValueError, "another specification or request"):
            self.analyze()
        self.context = original
        (self.root / "SPEC.md").write_text("Changed input", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            self.analyze()
        self.assertEqual(len(self.fixture.calls()), 1)

    def test_response_without_successful_cleanup_evidence_is_not_adopted(self):
        self.stop_when_preparing()
        journal = self.clarify._load_analysis(self.root)
        path = self.root / journal["directory"] / "model.exit.json"
        outcome = self.boundary.decode(path.read_text(encoding="utf-8"))
        outcome["cleanup"]["completed"] = False
        path.write_text(self.clarify._json_bytes(outcome).decode("utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cleanup evidence"):
            self.analyze()
        self.assertEqual(len(self.fixture.calls()), 1)

    def test_saved_response_evidence_cannot_change_after_validation(self):
        self.stop_when_preparing(after=True)
        journal = self.clarify._load_analysis(self.root)
        (self.root / journal["directory"] / "answer.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            self.analyze()
        self.assertEqual(len(self.fixture.calls()), 1)

    def test_raw_completed_response_cannot_change_before_validation(self):
        self.stop_when_preparing()
        journal = self.clarify._load_analysis(self.root)
        (self.root / journal["directory"] / "answer.json").write_text(json.dumps(test_boundary.EMPTY), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "response changed"):
            self.analyze()
        self.assertEqual(len(self.fixture.calls()), 1)

    def test_pending_journal_edit_is_rejected(self):
        self.stop_when_preparing(after=True)
        path = self.root / self.clarify.ANALYSIS_PENDING
        value = json.loads(path.read_text(encoding="utf-8"))
        value["context"]["request_id"] = "different-request"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.analyze()
        self.assertEqual(len(self.fixture.calls()), 1)

    def test_actual_process_exit_between_markdown_and_json_recovers(self):
        script = self.fixture.base / "crash_controller.py"
        script.write_text('''import importlib.util, json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("crash_clarify", root / ".harness/clarify.py")
clarify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clarify)
original = clarify._atomic_runtime
def write(path, data, temporary=None):
    result = original(path, data, temporary)
    journal = clarify._load_analysis(root)
    if path == root / clarify.boundary.ANSWERS and journal["stage"] == "publishing":
        os._exit(91)
    return result
clarify._atomic_runtime = write
clarify.analyze(root, "SPEC.md", json.loads(sys.argv[2]), "fake-analysis", 30,
                analysis_context=json.loads(sys.argv[3]))
''', encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(script), str(self.root), json.dumps(self.fixture.config), json.dumps(self.context)],
                                capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 91, result.stderr.decode("utf-8", errors="replace"))
        with self.assertRaisesRegex(ValueError, "differs"):
            self.boundary.load_state(self.root)
        self.assert_recovered_once()

    def test_prelaunch_prompt_keeps_ownership_across_repeated_resume(self):
        def stopped(*args, **kwargs):
            raise AbruptStop()
        with mock.patch.object(self.clarify.lifecycle, 'execute', side_effect=stopped):
            with self.assertRaises(AbruptStop):
                self.analyze()
            original = self.clarify._load_analysis(self.root)
            expected_directory = original['directory']
            for unused in range(2):
                self.assertIsNone(self.clarify.recover_analysis(self.root, 'SPEC.md', self.context))
                self.assertIn(expected_directory + '/prompt.txt', self.clarify.list_analysis_files(self.root))
                with self.assertRaises(AbruptStop):
                    self.analyze()
                self.assertEqual(self.clarify._load_analysis(self.root)['directory'], expected_directory)
        self.assertEqual(self.fixture.calls(), [])
        self.assert_recovered_once()
        self.assertEqual(self.clarify._load_analysis(self.root)['directory'], expected_directory)

    def test_prelaunch_before_prompt_publication_resumes_same_intent(self):
        original = self.clarify._atomic_runtime
        def stop(path, data, temporary=None):
            if path.name == 'prompt.txt':
                raise AbruptStop()
            return original(path, data, temporary)
        with mock.patch.object(self.clarify, '_atomic_runtime', side_effect=stop):
            with self.assertRaises(AbruptStop):
                self.analyze()
        directory = self.clarify._load_analysis(self.root)['directory']
        self.assert_recovered_once()
        self.assertEqual(self.clarify._load_analysis(self.root)['directory'], directory)

    def test_prelaunch_runtime_preamble_is_validated_and_budget_only_shrinks(self):
        with mock.patch.object(self.clarify.lifecycle, 'execute', side_effect=AbruptStop()):
            with self.assertRaises(AbruptStop):
                self.analyze()
        journal = self.clarify._load_analysis(self.root)
        folder = self.root / journal['directory']
        (folder / 'model.pycache').mkdir()
        (folder / 'model.input.txt').write_text(journal['prompt'], encoding='utf-8')
        (folder / 'model.stdout.log').write_bytes(b'')
        (folder / 'model.stderr.log').write_bytes(b'')
        (folder / 'model.command.json').write_text(json.dumps({'argv': journal['argv'], 'cwd': str(self.root),
            'timeout': journal['timeout'], 'python_environment': {'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONPYCACHEPREFIX': str((folder / 'model.pycache').resolve())}}), encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.clarify.analyze(self.root, 'SPEC.md', self.fixture.config, 'fake-analysis', 15,
                              analysis_context=self.context), 2)
        self.assertEqual(self.clarify._load_analysis(self.root)['timeout'], 15)
        self.assert_recovered_once()

    def test_unexpected_prelaunch_prompt_edit_is_refused(self):
        with mock.patch.object(self.clarify.lifecycle, 'execute', side_effect=AbruptStop()):
            with self.assertRaises(AbruptStop):
                self.analyze()
        journal = self.clarify._load_analysis(self.root)
        (self.root / journal['directory'] / 'prompt.txt').write_text('Unrelated operator edit', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'prelaunch prompt/input'):
            self.analyze()
        self.assertEqual(self.fixture.calls(), [])

    def test_unconfirmed_process_is_never_relaunched(self):
        self.stop_when_preparing()
        journal = self.clarify._load_analysis(self.root)
        (self.root / journal["directory"] / "model.exit.json").unlink()
        with self.assertRaisesRegex(ValueError, "cleanup is unconfirmed"):
            self.analyze()
        self.assertEqual(len(self.fixture.calls()), 1)


class ClarificationSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="clarification-snapshot-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / ".venv").mkdir()
        loader = importlib.util.spec_from_file_location("snapshot_clarify", test_boundary.SOURCE / "clarify.py")
        self.clarify = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(self.clarify)

    def fake_metadata(self, path, mode, attributes=0):
        original = Path.lstat
        info = original(path)
        value = SimpleNamespace(**{key: getattr(info, key) for key in
            ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")},
            st_mode=mode, st_file_attributes=attributes)
        def lstat(candidate, *args, **kwargs):
            return value if candidate == path else original(candidate, *args, **kwargs)
        return mock.patch.object(Path, "lstat", lstat)

    def test_unchanged_venv_link_is_allowed_and_create_remove_retarget_are_visible(self):
        link = self.root / ".venv/python"
        before = self.clarify.source_snapshot(self.root)
        link.write_bytes(b"filesystem link placeholder")
        with self.fake_metadata(link, stat.S_IFLNK | 0o777), mock.patch.object(os, "readlink", return_value="/usr/bin/python3") as readlink:
            first = self.clarify.source_snapshot(self.root)
            self.assertNotEqual(first, before)
            self.assertEqual(first, self.clarify.source_snapshot(self.root))
            self.assertEqual(first[".venv/python"]["kind"], "symlink")
            self.assertEqual(first[".venv/python"]["target"], "/usr/bin/python3")
            readlink.return_value = "/opt/python3"
            self.assertNotEqual(self.clarify.source_snapshot(self.root), first)
            link.unlink()
            self.assertEqual(self.clarify.source_snapshot(self.root), before)


    def test_link_retarget_during_snapshot_is_rejected(self):
        link = self.root / ".venv/python"
        link.write_bytes(b"filesystem link placeholder")
        with self.fake_metadata(link, stat.S_IFLNK | 0o777), mock.patch.object(os, "readlink", side_effect=["/usr/bin/python3", "/opt/python3"]):
            with self.assertRaisesRegex(ValueError, "link changed"):
                self.clarify.source_snapshot(self.root)


    def test_special_file_is_rejected_before_reading(self):
        path = self.root / "pipe"
        path.write_bytes(b"filesystem FIFO placeholder")
        with self.fake_metadata(path, stat.S_IFIFO | 0o600), mock.patch.object(Path, "read_bytes") as read:
            with self.assertRaisesRegex(ValueError, "Special or redirected"):
                self.clarify.source_snapshot(self.root)
            read.assert_not_called()

    def test_walk_permission_error_is_not_treated_as_an_empty_directory(self):
        def walk(*args, **kwargs):
            kwargs["onerror"](PermissionError("unreadable dependency directory"))
            return iter(())
        with mock.patch.object(os, "walk", side_effect=walk):
            with self.assertRaisesRegex(ValueError, "Cannot enumerate clarification source"):
                self.clarify.source_snapshot(self.root)


if __name__ == "__main__":
    unittest.main()
