"""Interview intake, explicit approvals and clocks, using only local fake workers."""
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import test_boundary
import test_requirements as fixtures


KIT = Path(__file__).resolve().parents[2]
INTERVIEW = """# Interview

## Actual operator answers
- Missing readings must return unavailable without a numeric value.

## Confirmed source facts
- The source contains measured values and timestamps.

## Success criteria
- R-01: Return the measured value unchanged. Confirmed by the operator.
- R-02: A response-time target is still undecided.

## AI proposals and assumptions (not confirmed)
- AI proposed a 0.1-second target. The operator has not agreed to it.

## Unresolved
- The response-time target remains unknown.
"""
SPEC = "Return measured values. Read INTERVIEW.md for actual operator answers and unconfirmed proposals.\n"


class InterviewFixture(fixtures.RequirementsFixture):
    def setUp(self):
        super().setUp()
        self.fixture.write('SPEC.md', SPEC)
        self.fixture.write('INTERVIEW.md', INTERVIEW)
        self.set_config(interview='INTERVIEW.md', problem_time_limit_seconds=9000)
        self.interview_scenario()

    def set_config(self, **changes):
        path = self.root / 'config.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value.update(changes)
        path.write_text(json.dumps(value), encoding='utf-8')

    def interview_scenario(self, report=None, **options):
        report = dict(test_boundary.EMPTY if report is None else report)
        report['references'] = ['INTERVIEW.md']
        self.scenario(report=report, **options)

    def frontend(self, *arguments, root=None):
        project = self.root if root is None else root
        return subprocess.run([sys.executable, '-B', str(KIT / 'run_harness.py'),
                               *arguments, '--root', str(project)], cwd=str(self.base),
                              capture_output=True, text=True, encoding='utf-8', timeout=60,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8'))

    def assert_code(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def functional_return(self):
        path = self.base / 'returned functional.md'
        path.write_bytes((self.root / self.requirements.FUNCTIONAL_DOCUMENT).read_bytes())
        return path

    def confirm_both(self):
        self.assert_code(self.frontend('requirements', '--confirm-file', str(self.returned_document())), 3)
        self.assertEqual(self.state()['status'], 'CONFIRMED')
        self.assertEqual(self.state(stage='functional')['status'], 'WAITING_CONFIRMATION')
        result = self.assert_code(self.frontend('requirements', '--confirm-functional', str(self.functional_return())), 0)
        self.assertIn('COMPLETE', result.stdout)


class InterviewTests(InterviewFixture, unittest.TestCase):
    def test_start_before_input_files_and_time_status_never_call_the_model(self):
        (self.root / 'SPEC.md').unlink()
        (self.root / 'INTERVIEW.md').unlink()
        received = datetime.now(timezone.utc) - timedelta(minutes=2)
        result = self.assert_code(self.frontend('start', '--received-at', received.isoformat()), 0)
        info = json.loads(result.stdout)
        self.assertTrue(info['started'])
        self.assertAlmostEqual(info['received_at'], received.timestamp(), places=5)
        self.assertEqual(info['deadline_at'] - info['received_at'], 9000)
        receipt = self.root / self.requirements.problem_time.RECEIPT
        original = receipt.read_bytes()
        self.assert_code(self.frontend('start'), 0)
        self.assertEqual(receipt.read_bytes(), original)
        before = {path.relative_to(self.root).as_posix(): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        status = self.assert_code(self.frontend('time-status'), 0)
        self.assertEqual(json.loads(status.stdout)['received_at'], info['received_at'])
        after = {path.relative_to(self.root).as_posix(): path.read_bytes()
                 for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(after, before)
        self.assert_code(self.frontend('start', '--received-at', (received + timedelta(seconds=1)).isoformat()), 2)
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.root / self.requirements.boundary.STATE).exists())

    def test_default_root_api_and_unstarted_status_are_read_only(self):
        loader = importlib.util.spec_from_file_location('interview_frontend', KIT / 'run_harness.py')
        module = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(module)
        before = {path.relative_to(self.root).as_posix(): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = module.main(['time-status'], default_root=self.root)
        self.assertEqual(result, 0)
        self.assertFalse(json.loads(output.getvalue())['started'])
        self.assertEqual(before, {path.relative_to(self.root).as_posix(): path.read_bytes()
                                  for path in self.root.rglob('*') if path.is_file()})
        self.assertEqual(self.calls(), [])

    def test_missing_interview_or_missing_source_reference_stops_before_model(self):
        interview = self.root / 'INTERVIEW.md'
        interview.unlink()
        self.assert_code(self.frontend('requirements'), 2)
        self.assertEqual(self.calls(), [])
        interview.write_text(INTERVIEW, encoding='utf-8')
        self.fixture.write('SPEC.md', 'Return measured values.\n')
        self.assert_code(self.frontend('requirements'), 2)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.document().exists())

    def test_generated_output_and_original_spec_cannot_be_used_as_interview(self):
        boundary = self.requirements.boundary
        for name in (self.requirements.DOCUMENT, self.requirements.FUNCTIONAL_DOCUMENT,
                     self.requirements.STATE, boundary.STATE, boundary.ANSWERS, 'SPEC.md'):
            with self.subTest(name=name):
                if name != 'SPEC.md':
                    self.fixture.write(name, 'Existing generated content\n')
                self.set_config(interview=name)
                with self.assertRaises(ValueError):
                    boundary.create_state(self.root, 'SPEC.md')
        self.assertEqual(self.calls(), [])

    def test_initial_state_hashes_interview_and_model_must_report_using_it(self):
        boundary = self.requirements.boundary
        initial = boundary.create_state(self.root, 'SPEC.md')
        self.assertIn('INTERVIEW.md', initial['references'])
        self.assertEqual(initial['input_hashes']['INTERVIEW.md'],
                         boundary.digest((self.root / 'INTERVIEW.md').read_bytes()))
        self.scenario(report=test_boundary.EMPTY)
        self.assert_code(self.frontend('requirements'), 2)
        self.assertEqual(self.kinds(), ['clarify'])
        self.assertFalse(self.document().exists())

    def test_dot_relative_interview_link_resolves_to_one_bound_source(self):
        boundary = self.requirements.boundary
        self.fixture.write('SPEC.md', 'Return measured values. Read [the interview](./INTERVIEW.md).\n')
        state = boundary.create_state(self.root, 'SPEC.md')
        self.assertEqual(state['references'], ['INTERVIEW.md'])
        self.assertEqual(state['input_hashes']['INTERVIEW.md'],
                         boundary.digest((self.root / 'INTERVIEW.md').read_bytes()))
        self.fixture.write('SPEC.md', 'Return measured values. Read [another interview](archive/INTERVIEW.md).\n')
        with self.assertRaises(ValueError):
            boundary.create_state(self.root, 'SPEC.md')
        self.assertEqual(self.calls(), [])

    def test_actual_answer_and_two_explicit_approvals_finish_without_development(self):
        self.interview_scenario(test_boundary.QUESTION)
        self.assert_code(self.frontend('requirements'), 3)
        self.assertEqual(self.kinds(), ['clarify'])
        self.assertFalse(self.document().exists())
        answer = self.base / 'operator answer.txt'
        answer.write_text('Return unavailable without a numeric value.', encoding='utf-8')
        self.interview_scenario()
        self.assert_code(self.frontend('requirements', '--answer', 'Q-001', '--answer-file', str(answer)), 3)
        self.assertIn('INTERVIEW.md', self.state()['input_hashes'])
        self.assertIn('INTERVIEW.md', self.document().read_text(encoding='utf-8'))
        self.assertEqual(self.requirements.boundary.actual_answers(
            self.requirements.boundary.load_state(self.root))['Q-001'], answer.read_text(encoding='utf-8'))
        self.assertFalse((self.root / self.requirements.FUNCTIONAL_DOCUMENT).exists())
        self.confirm_both()
        kinds = self.kinds()
        self.assertEqual(kinds, ['clarify', 'clarify', 'requirements', 'functional'])
        # Verify the policy reaches each worker; this is not a live-model semantic test.
        for call in self.calls():
            for term in ('ai proposals', 'undecided', 'unmeasured'):
                self.assertIn(term, call['prompt'].lower())
        self.assert_code(self.frontend(), 0)
        self.assertEqual(self.kinds(), kinds)
        self.assert_no_implementation()

    def test_changed_interview_invalidates_both_approvals_and_requires_regeneration(self):
        self.assert_code(self.frontend('requirements'), 3)
        self.confirm_both()
        previous = self.state()['revision']
        old_functional = self.functional_return()
        calls = self.kinds()
        self.fixture.write('INTERVIEW.md', INTERVIEW + '\nActual operator update: include a measurement timestamp.\n')
        status = self.assert_code(self.frontend('requirements', '--status'), 0)
        info = json.loads(status.stdout)
        self.assertEqual(info['requirements_status'], 'STALE')
        self.assertEqual(info['functional_status'], 'STALE')
        self.assert_code(self.frontend('requirements', '--confirm-functional', str(old_functional)), 2)
        self.assertEqual(self.kinds(), calls)
        self.interview_scenario(markdown=fixtures.DOCUMENT + 'Include the timestamp.\n',
                                functional=fixtures.FUNCTIONAL + 'F-02: Return the timestamp.\n')
        self.assert_code(self.frontend('requirements'), 3)
        self.assertEqual(self.state()['revision'], previous + 1)
        self.assertEqual(self.state()['status'], 'WAITING_CONFIRMATION')
        self.assertEqual(self.state(current=False, stage='functional')['status'], 'STALE')
        self.confirm_both()
        self.assertEqual(self.kinds(), calls + ['clarify', 'requirements', 'functional'])

    def test_separate_project_needs_inputs_and_config_but_no_copied_runtime(self):
        project = self.base / 'separate problem'
        project.mkdir()
        for name in ('SPEC.md', 'INTERVIEW.md', 'config.json', self.requirements.boundary.TEMPLATE):
            shutil.copy2(self.root / name, project / name)
        self.assertFalse((project / '.harness').exists())
        self.assert_code(self.frontend('start', root=project), 0)
        self.assert_code(self.frontend('requirements', root=project), 3)
        state = self.requirements.load(project)
        self.assertEqual(state['status'], 'WAITING_CONFIRMATION')
        self.assertIn('INTERVIEW.md', state['input_hashes'])
        self.assertTrue((project / self.requirements.DOCUMENT).is_file())
        self.assertEqual(list((project / '.harness').glob('*.py')), [])
        self.assertFalse(self.document().exists())
        self.assertEqual(self.kinds(), ['clarify', 'requirements'])


if __name__ == '__main__':
    unittest.main()
