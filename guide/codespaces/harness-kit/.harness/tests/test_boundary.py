import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
loader = importlib.util.spec_from_file_location("test_boundary_core", SOURCE / "boundary.py")
boundary = importlib.util.module_from_spec(loader)
loader.loader.exec_module(boundary)

EMPTY = {"version": 1, "questions": [], "unresolved": [], "references": []}
QUESTION = {"version": 1, "questions": [{"template": "T03", "question": "Which state should a missing measurement return?",
    "decides": "The response for unavailable measurements", "follow_up_to": None}],
    "unresolved": ["Missing measurement response"], "references": []}

FAKE = r'''
import json, sys, time
from pathlib import Path
scenario=json.loads(Path(__file__).with_suffix('.json').read_text(encoding='utf-8'))
arguments=sys.argv[1:]
prompt=sys.stdin.read()
context=json.loads(prompt.split('BOUNDARY_CLARIFICATION_CONTEXT\n',1)[1])
capture=Path(__file__).with_suffix('.calls.jsonl')
with capture.open('a',encoding='utf-8') as stream:
    stream.write(json.dumps({'argv':arguments,'context':context})+'\n')
if scenario.get('mutate'):
    Path('app.py').write_text('changed by fake model',encoding='utf-8')
time.sleep(scenario.get('sleep',0))
answer=Path(arguments[arguments.index('--output-last-message')+1])
answer.write_text(json.dumps(scenario['report']),encoding='utf-8')
print('fake clarification output only')
raise SystemExit(scenario.get('exit',0))
'''


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="boundary-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project spaces 한글"
        self.root.mkdir()
        self.write("SPEC.md", "Return a measured value. Missing measurement behavior is unspecified. API: /tenants/{tenant_id}/ask\n")
        self.write(boundary.TEMPLATE, "Choose T01 through T15. Replace {대상} and {조건A}; preserve source API variables.\n")
        for name in ("boundary.py", "clarify.py", "data_checks.py", "processes.py", "operation_lock.py", "lifecycle.py",
                     "codex_protocol.py", "problem_time.py", "clarify_prompt.md", "clarify.schema.json"):
            target = self.root / ".harness" / name
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(str(SOURCE / name), str(target))
        self.fake = self.base / "fake_codex.py"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.config = {"project": ".", "spec": "SPEC.md", "model": "fake-scope-only",
                       "codex_command": [sys.executable, str(self.fake)]}
        self.write("config.json", json.dumps(self.config))
        self.scenario(EMPTY)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def scenario(self, report, **extra):
        self.fake.with_suffix(".json").write_text(json.dumps(dict(report=report, **extra)), encoding="utf-8")

    def cli(self, *arguments):
        return subprocess.run([sys.executable, "-B", str(self.root / ".harness/clarify.py"), "--root", str(self.root), *arguments],
            cwd=str(self.root), capture_output=True, text=True, encoding="utf-8", timeout=30,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))

    def calls(self):
        path = self.fake.with_suffix(".calls.jsonl")
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def answer(self, question, value):
        path = self.base / "user-answer.txt"
        path.write_text(value, encoding="utf-8")
        result = self.cli("--answer", question, "--answer-file", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_no_questions_becomes_ready_without_tasks_acceptance_or_model_confirmation(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        info = boundary.require_ready(self.root, "SPEC.md")
        self.assertEqual(info["answered_question_ids"], [])
        self.assertIn(self.root / boundary.STATE, info["protected_files"])
        self.assertEqual(self.cli("--check-ready").returncode, 0)
        self.assertEqual(self.cli("--status").returncode, 0)
        self.assertEqual(len(self.calls()), 1)
        call = self.calls()[0]
        self.assertEqual(call["argv"][call["argv"].index("--sandbox")+1], "read-only")
        self.assertEqual(call["context"]["actual_answers"], [])
        self.assertFalse((self.root / ".harness/acceptance.json").exists())
        self.assertFalse((self.root / "automation/codex-loop/tasks.json").exists())

    def test_actual_answers_require_reanalysis_and_ambiguous_answers_get_t12(self):
        self.scenario(QUESTION)
        self.assertEqual(self.cli().returncode, 2)
        self.assertEqual(boundary.load_state(self.root)["status"], "needs_answers")
        self.assertEqual(self.cli("--check-ready").returncode, 2)
        self.answer("Q-001", "Handle it as appropriate.")
        self.assertEqual(self.cli("--check-ready").returncode, 2)
        self.assertEqual(boundary.load_state(self.root)["status"], "needs_analysis")
        follow = {"version": 1, "questions": [{"template": "T12", "question": "Should missing measurements return unavailable without a numeric value?",
                 "decides": "The missing response", "follow_up_to": "Q-001"}], "unresolved": ["Response remains unclear"], "references": []}
        self.scenario(follow)
        self.assertEqual(self.cli().returncode, 2)
        self.answer("Q-002", "Return unavailable with no numeric value.")
        self.scenario(EMPTY)
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        info = boundary.require_ready(self.root)
        self.assertEqual(info["answered_question_ids"], ["Q-001", "Q-002"])
        self.assertIn("Handle it as appropriate.", info["context"])
        self.assertIn("Return unavailable with no numeric value.", info["context"])
        self.assertEqual(self.calls()[-1]["context"]["actual_answers"][-1]["text"], "Return unavailable with no numeric value.")

    def test_unanswered_questions_cannot_be_erased_by_a_zero_question_report(self):
        self.scenario(QUESTION)
        self.assertEqual(self.cli().returncode, 2)
        self.scenario(EMPTY)
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(self.calls()), 1)
        with self.assertRaises(boundary.BoundaryError):
            boundary.complete_analysis(self.root, boundary.load_state(self.root), EMPTY)

    def test_nine_unknowns_take_two_question_rounds_then_separate_ready_analysis(self):
        schema = json.loads((SOURCE / "clarify.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["questions"]["maxItems"], boundary.MAX_QUESTIONS)
        self.assertEqual(boundary.MAX_QUESTIONS, 8)
        questions = [{"template": "T06", "question": "What is the limit and unit for parameter P%d?" % number,
                      "decides": "The permitted P%d range" % number, "follow_up_to": None}
                     for number in range(1, 10)]
        self.write("SPEC.md", "Parameters P1 through P9 each need a separately agreed limit and unit.\n")
        self.scenario({**EMPTY, "questions": questions[:8],
                       "unresolved": ["P%d limit and unit" % number for number in range(1, 10)]})
        self.assertEqual(self.cli().returncode, 2)
        state = boundary.load_state(self.root)
        self.assertEqual(len(state["questions"]), 8)
        self.assertEqual(state["answers"], [])
        self.assertIn("P9 limit and unit", state["analysis"]["report"]["unresolved"])
        for number in range(1, 9):
            self.answer("Q-%03d" % number, "P%d must be at most %d seconds." % (number, number))
        self.assertEqual(self.cli("--check-ready").returncode, 2)
        self.scenario({**EMPTY, "questions": questions[8:], "unresolved": ["P9 limit and unit"]})
        self.assertEqual(self.cli().returncode, 2)
        state = boundary.load_state(self.root)
        self.assertEqual(len(state["questions"]), 9)
        self.assertEqual(len(state["answers"]), 8)
        self.assertNotIn("Q-009", boundary.actual_answers(state))
        self.assertEqual(len(self.calls()), 2, "Only the two question-generating analyses are counted here")
        self.scenario(EMPTY)
        self.assertEqual(self.cli().returncode, 2, "An unanswered ninth question cannot be erased")
        self.assertEqual(len(self.calls()), 2)
        self.answer("Q-009", "P9 must be at most 9 seconds.")
        self.assertEqual(self.cli("--check-ready").returncode, 2)
        self.assertEqual(self.cli().returncode, 0)
        self.assertEqual(len(self.calls()), 3, "Readiness requires a separate analysis after all actual answers")
        self.assertEqual(len(boundary.require_ready(self.root)["answered_question_ids"]), 9)
        self.assertEqual(len(self.calls()[-1]["context"]["actual_answers"]), 9)

    def test_model_cannot_write_answers_or_leave_unactionable_unresolved_items(self):
        for bad in ({**EMPTY, "answers": [{"question_id": "Q-001", "text": "invented"}]},
                    {**EMPTY, "ready": True}, {**EMPTY, "unresolved": ["unknown"]},
                    {**QUESTION, "questions": QUESTION["questions"] * 9}):
            with self.subTest(report=bad):
                self.scenario(bad)
                self.assertEqual(self.cli().returncode, 2)
                state = boundary.load_state(self.root)
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["answers"], [])
                with self.assertRaises(boundary.BoundaryError):
                    boundary.require_ready(self.root)

    def test_spec_template_and_referenced_document_changes_invalidate_ready(self):
        self.write("docs/reference.md", "Explicit supplementary requirement")
        report = {**EMPTY, "references": ["docs/reference.md"]}
        for name in ("SPEC.md", boundary.TEMPLATE, "docs/reference.md"):
            with self.subTest(name=name):
                self.scenario(report)
                self.assertEqual(self.cli().returncode, 0)
                self.write(name, (self.root / name).read_text(encoding="utf-8") + " changed")
                with self.assertRaisesRegex(boundary.BoundaryError, "changed"):
                    boundary.require_ready(self.root)
                self.assertEqual(self.cli("--check-ready").returncode, 2)
        self.assertEqual(self.cli().returncode, 0)
        self.write("OTHER.md", "different specification")
        with self.assertRaisesRegex(boundary.BoundaryError, "different specification"):
            boundary.require_ready(self.root, "OTHER.md")

    def test_answer_document_and_metadata_tampering_fail_closed(self):
        self.assertEqual(self.cli().returncode, 0)
        self.write(boundary.ANSWERS, "Invented confirmed answer")
        self.assertEqual(self.cli("--check-ready").returncode, 2)
        state = boundary.create_state(self.root, "SPEC.md")
        boundary.complete_analysis(self.root, state, EMPTY)
        state["status"], state["analysis"] = "ready", None
        boundary.save_state(self.root, state)
        with self.assertRaises(boundary.BoundaryError):
            boundary.require_ready(self.root)

    def test_nonzero_cli_and_readonly_source_mutation_invalidate_prior_ready(self):
        for scenario in ({"exit": 7}, {"mutate": True}):
            with self.subTest(scenario=scenario):
                self.scenario(EMPTY)
                self.assertEqual(self.cli().returncode, 0)
                self.scenario(EMPTY, **scenario)
                self.assertEqual(self.cli().returncode, 2)
                self.assertEqual(boundary.load_state(self.root)["status"], "failed")
                with self.assertRaises(boundary.BoundaryError):
                    boundary.require_ready(self.root)

    def test_timeout_marks_failed_and_stops_child(self):
        self.scenario(EMPTY, sleep=10)
        result = self.cli("--timeout", "1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("timed out", result.stderr)
        self.assertEqual(boundary.load_state(self.root)["status"], "failed")

    def test_question_placeholders_and_source_locations_rejected_but_api_variables_preserved(self):
        state = boundary.create_state(self.root, "SPEC.md")
        for question in ("What should {대상} do?", "See page 3: what happens?", "질문\n근거: PROBLEM.md 4절"):
            report = json.loads(json.dumps(QUESTION))
            report["questions"][0]["question"] = question
            with self.subTest(question=question), self.assertRaises(boundary.BoundaryError):
                boundary.validate_report(self.root, report, state)
        report = json.loads(json.dumps(QUESTION))
        report["questions"][0]["question"] = "What should /tenants/{tenant_id}/ask return for missing measurements?"
        boundary.validate_report(self.root, report, state)

    def test_unsafe_paths_are_rejected(self):
        for name in ("../outside.md", ".harness/work/answer.json", "work/spec.md", ".git/config", "D:/outside.md"):
            with self.subTest(name=name), self.assertRaises(boundary.BoundaryError):
                boundary.project_path(self.root, name, exists=False)

    def test_check_ready_resolves_the_config_actual_project(self):
        self.assertEqual(self.cli().returncode, 0)
        other = self.base / "another project"
        other.mkdir()
        (other / "SPEC.md").write_text("another", encoding="utf-8")
        alternate = self.base / "alternate.json"
        alternate.write_text(json.dumps({"project": "another project", "spec": "SPEC.md"}), encoding="utf-8")
        result = self.cli("--config", str(alternate), "--check-ready")
        self.assertEqual(result.returncode, 2)
        self.assertIn("boundary.json", result.stderr)

    def test_unknown_and_blank_answers_do_not_change_state(self):
        self.scenario(QUESTION)
        self.assertEqual(self.cli().returncode, 2)
        before = (self.root / boundary.STATE).read_bytes()
        for question, answer in (("Q-999", "actual"), ("Q-001", " \n")):
            with self.assertRaises(boundary.BoundaryError):
                boundary.submit_answer(self.root, question, answer)
            self.assertEqual((self.root / boundary.STATE).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
