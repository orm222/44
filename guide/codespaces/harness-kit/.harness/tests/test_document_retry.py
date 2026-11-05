"""Finished failed model attempts can retry without dropping their evidence."""
import json
import unittest
from unittest import mock

import test_requirements as fixtures


class DocumentRetryTests(fixtures.RequirementsFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ready_boundary()

    def assert_code(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def outcome(self, stage='requirements'):
        state = self.state(current=False, stage=stage)
        return self.root / state['directory'] / 'model.exit.json'

    def test_timeout_retries_with_a_new_revision_and_the_same_clock(self):
        script = fixtures.FAKE.replace('import json, sys', 'import json, sys, time')
        script = script.replace('result = (', "time.sleep(scenario.get(kind + '_sleep', 0))\nresult = (")
        self.fixture.fake.write_text(script, encoding='utf-8')
        self.scenario(requirements_sleep=30)
        self.assert_code(self.cli('--timeout', '3'), 2)
        outcome = self.outcome()
        original = outcome.read_bytes()
        self.assertEqual(json.loads(original)['termination_reason'], 'timed_out')
        self.assertTrue(json.loads(original)['cleanup']['completed'])
        clock = self.requirements.problem_time.load(self.root, required=True)
        self.scenario()
        self.assert_code(self.cli(), 3)
        self.assertEqual(self.state()['revision'], 2)
        self.assertEqual(self.requirements.problem_time.load(self.root, required=True), clock)
        self.assertEqual(outcome.read_bytes(), original)
        self.assertEqual(self.kinds(), ['requirements', 'requirements'])

    def test_editing_input_during_requirements_generation_requires_fresh_analysis(self):
        self.fixture.fake.write_text(fixtures.FAKE.replace("Path('app.py')", "Path('SPEC.md')"), encoding='utf-8')
        self.scenario(mutate='requirements')
        self.assert_code(self.cli(), 2)
        outcome = self.outcome()
        self.assertEqual(json.loads(outcome.read_bytes())['termination_reason'], 'guard_failed')
        self.assertFalse(self.document().exists())
        self.scenario()
        self.assert_code(self.cli(), 3)
        self.assertEqual(self.state()['revision'], 2)
        self.assertEqual(self.kinds(), ['requirements', 'clarify', 'requirements'])
        self.assertEqual((self.root / 'SPEC.md').read_text(encoding='utf-8'), 'unauthorized worker edit')

    def test_editing_input_during_functional_generation_requires_new_parent_approval(self):
        self.pending()
        parent_revision = self.state()['revision']
        self.fixture.fake.write_text(fixtures.FAKE.replace("Path('app.py')", "Path('SPEC.md')"), encoding='utf-8')
        self.scenario(mutate='functional')
        self.assert_code(self.cli('--confirm-file', str(self.returned_document())), 2)
        self.scenario()
        self.assert_code(self.cli(), 3)
        self.assertEqual(self.state()['status'], 'WAITING_CONFIRMATION')
        self.assertEqual(self.state()['revision'], parent_revision + 1)
        self.assertEqual(self.kinds(), ['requirements', 'functional', 'clarify', 'requirements'])
        self.assert_code(self.cli('--confirm-file', str(self.returned_document())), 3)
        self.assertEqual(self.state(stage='functional')['revision'], 2)

    def test_incomplete_cleanup_remains_blocked_even_after_an_input_edit(self):
        self.scenario(requirements_exit=7)
        with mock.patch.object(self.requirements, '_retire_failed', side_effect=lambda root, state: state):
            self.assertEqual(self.local()[0], 2)
        path = self.outcome()
        outcome = json.loads(path.read_bytes())
        outcome['cleanup']['completed'] = False
        path.write_text(json.dumps(outcome), encoding='utf-8')
        self.fixture.write('SPEC.md', 'An updated problem input.\n')
        self.scenario()
        self.assert_code(self.cli(), 2)
        self.assertEqual(self.kinds(), ['requirements'])
        self.assertEqual(self.state(current=False)['revision'], 1)

    def test_missing_exit_receipt_remains_blocked(self):
        self.scenario(requirements_exit=7)
        with mock.patch.object(self.requirements, '_retire_failed', side_effect=lambda root, state: state):
            self.assertEqual(self.local()[0], 2)
        self.outcome().unlink()
        self.scenario()
        self.assert_code(self.cli(), 2)
        self.assertEqual(self.kinds(), ['requirements'])

    def test_interrupted_failure_retirement_resumes_without_losing_evidence(self):
        self.scenario(requirements_exit=7)
        write = self.requirements._write
        def stop_before_stale(root, name, data, directory):
            if name == self.requirements.STATE and json.loads(data)['status'] == 'STALE':
                raise fixtures.AbruptStop()
            return write(root, name, data, directory)
        with mock.patch.object(self.requirements, '_write', side_effect=stop_before_stale):
            with self.assertRaises(fixtures.AbruptStop):
                self.local()
        original_outcome = self.outcome()
        original = original_outcome.read_bytes()
        self.assertEqual(self.requirements._journal(self.root, self.state(current=False))['stage'], 'failed')
        self.scenario()
        self.assert_code(self.cli(), 3)
        self.assertEqual(original_outcome.read_bytes(), original)
        self.assertEqual(self.state()['revision'], 2)
        self.assertEqual(self.kinds(), ['requirements', 'requirements'])

    def test_old_functional_failure_evidence_is_still_checked_after_a_retry(self):
        self.pending()
        self.scenario(functional_exit=7)
        self.assert_code(self.cli('--confirm-file', str(self.returned_document())), 2)
        old_outcome = self.outcome('functional')
        self.scenario()
        self.assert_code(self.cli(), 3)
        old_outcome.write_bytes(old_outcome.read_bytes() + b'\n')
        self.assert_code(self.cli(), 2)
        self.assertEqual(self.kinds(), ['requirements', 'functional', 'functional'])

    def test_expired_budget_prevents_another_failed_attempt(self):
        self.scenario(requirements_exit=7)
        self.assert_code(self.cli(), 2)
        timing = self.requirements.problem_time.load(self.root, required=True)
        with mock.patch.object(self.requirements.problem_time, '_now', return_value=timing['deadline_at'] + 1):
            code, output = self.local()
        self.assertEqual(code, 2, output)
        self.assertIn('expired', output)
        self.assertEqual(self.kinds(), ['requirements'])
        self.assertEqual(self.requirements.problem_time.load(self.root, required=True), timing)

    def test_invalid_model_json_empty_document_and_wrong_shape_can_retry(self):
        script = fixtures.FAKE.replace('write_text(json.dumps(result),',
            "write_text(scenario.get('raw_result', json.dumps(result)),")
        self.fixture.fake.write_text(script, encoding='utf-8')
        for revision, raw in enumerate(('not json', '{"requirements_markdown": ""}', '{"wrong_key": "content"}'), 1):
            with self.subTest(raw=raw):
                self.scenario(raw_result=raw)
                self.assert_code(self.cli(), 2)
                self.assertEqual(self.state(current=False)['status'], 'STALE')
                self.assertEqual(self.state(current=False)['revision'], revision)
                self.assertFalse(self.document().exists())
        self.scenario()
        self.assert_code(self.cli(), 3)
        self.assertEqual(self.state()['revision'], 4)
        self.assertEqual(self.kinds(), ['requirements'] * 4)

    def test_empty_functional_document_can_retry_without_reconfirming_requirements(self):
        self.pending()
        self.scenario(functional='')
        self.assert_code(self.cli('--confirm-file', str(self.returned_document())), 2)
        confirmation = self.state()['confirmation']
        self.assertEqual(self.state(current=False, stage='functional')['status'], 'STALE')
        self.scenario()
        self.assert_code(self.cli(), 3)
        self.assertEqual(self.state()['confirmation'], confirmation)
        self.assertEqual(self.state(stage='functional')['revision'], 2)
        self.assertEqual(self.kinds(), ['requirements', 'functional', 'functional'])

    def test_input_change_after_successful_exit_retires_the_result(self):
        prepare = self.requirements._prepare_result
        def change_then_prepare(root, journal):
            self.fixture.write('SPEC.md', 'Changed after the successful process exit.\n')
            return prepare(root, journal)
        with mock.patch.object(self.requirements, '_prepare_result', side_effect=change_then_prepare):
            code, output = self.local()
        self.assertEqual(code, 2, output)
        state = self.state(current=False)
        self.assertEqual(state['status'], 'STALE')
        journal = self.requirements._journal(self.root, state)
        self.assertEqual(journal['retry_reason'], 'inputs_changed')
        self.assertEqual(json.loads(self.outcome().read_bytes())['exit_code'], 0)
        self.assertFalse(self.document().exists())
        self.assert_code(self.cli(), 3)
        self.assertEqual(self.state()['revision'], 2)
        self.assertEqual(self.kinds(), ['requirements', 'clarify', 'requirements'])

    def test_input_change_after_prepared_publication_requires_a_new_result(self):
        prepare = self.requirements._prepare_result
        def stop_after_prepare(*args, **kwargs):
            prepare(*args, **kwargs)
            raise fixtures.AbruptStop()
        with mock.patch.object(self.requirements, '_prepare_result', side_effect=stop_after_prepare):
            with self.assertRaises(fixtures.AbruptStop):
                self.local()
        state = self.state(current=False)
        receipt = self.root / state['directory'] / 'result-receipt.json'
        original = receipt.read_bytes()
        self.fixture.write('SPEC.md', 'Changed while the completed result was waiting to be saved.\n')
        self.assert_code(self.cli(), 3)
        self.assertEqual(self.state()['revision'], 2)
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(self.kinds(), ['requirements', 'clarify', 'requirements'])

    def test_unreferenced_source_edit_after_functional_success_requires_parent_approval_again(self):
        self.pending()
        prepare = self.requirements._prepare_result
        def change_then_prepare(root, journal):
            self.fixture.write('notes.txt', 'A user saved another project file.\n')
            return prepare(root, journal)
        with mock.patch.object(self.requirements, '_prepare_result', side_effect=change_then_prepare):
            code, output = self.local('--confirm-file', str(self.returned_document()))
        self.assertEqual(code, 2, output)
        self.assertEqual(self.state(current=False, stage='functional')['status'], 'STALE')
        self.assert_code(self.cli(), 3)
        self.assertEqual(self.state()['status'], 'WAITING_CONFIRMATION')
        self.assertEqual(self.state()['revision'], 2)
        self.assertEqual(self.kinds(), ['requirements', 'functional', 'requirements'])
        self.assert_code(self.cli('--confirm-file', str(self.returned_document())), 3)
        self.assertEqual(self.state(stage='functional')['revision'], 2)

    def test_changed_input_never_makes_a_tampered_success_receipt_retryable(self):
        execute = self.requirements.lifecycle.execute
        def stop_after_success(*args, **kwargs):
            execute(*args, **kwargs)
            raise fixtures.AbruptStop()
        with mock.patch.object(self.requirements.lifecycle, 'execute', side_effect=stop_after_success):
            with self.assertRaises(fixtures.AbruptStop):
                self.local()
        state = self.state(current=False)
        receipt = self.root / state['directory'] / 'result-receipt.json'
        receipt.write_bytes(receipt.read_bytes() + b' ')
        self.fixture.write('SPEC.md', 'A changed input cannot legitimize a tampered receipt.\n')
        self.assert_code(self.cli(), 2)
        self.assertEqual(self.kinds(), ['requirements'])


if __name__ == '__main__':
    unittest.main()
