"""CSV evidence enters the existing document/question flow, without a second application."""
import json
import unittest

import test_boundary
import test_functional_registration
import test_interview
import test_requirements as fixtures


MEASUREMENTS = 'timestamp,value,group,regime\n0,10,A,R\n1,11,A,R\n2,,A,R\n3,1000,A,R\n'
BASELINE = 'timestamp,value,group,regime\n-4,10,A,R\n-3,11,A,R\n-2,10,A,R\n-1,11,A,R\n'


class DataFlowTests(test_interview.InterviewFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fixture.write('measurements.csv', MEASUREMENTS)
        self.fixture.write('baseline.csv', BASELINE)
        self.fixture.write('SPEC.md', test_interview.SPEC + '\nInspect measurements.csv using baseline.csv.\n')
        self.set_config(data_files=['measurements.csv'], data_baseline='baseline.csv')
        self.data_scenario()

    def data_scenario(self, report=None, **options):
        report = dict(test_boundary.EMPTY if report is None else report)
        report['references'] = ['INTERVIEW.md', 'measurements.csv', 'baseline.csv']
        self.scenario(report=report, **options)

    def context(self, call):
        marker = 'BOUNDARY_CLARIFICATION_CONTEXT\n' if call['kind'] == 'clarify' else 'REQUIREMENTS_CONTEXT\n'
        return json.loads(call['prompt'].split(marker, 1)[1])

    def assert_data_context(self, call):
        context = self.context(call)
        checks = context['data_checks']
        self.assertEqual(checks['status'], 'review_required')
        self.assertTrue(checks['findings'])
        self.assertEqual({item['file'] for item in checks['files']}, {'measurements.csv', 'baseline.csv'})
        for name in ('measurements.csv', 'baseline.csv'):
            self.assertEqual(checks['input_hashes'][name],
                             self.requirements.boundary.digest((self.root / name).read_bytes()))
        return context

    def test_csv_and_baseline_are_hashed_and_cannot_be_omitted_from_model_references(self):
        state = self.requirements.boundary.create_state(self.root, 'SPEC.md')
        for name in ('measurements.csv', 'baseline.csv'):
            self.assertIn(name, state['references'])
            self.assertEqual(state['input_hashes'][name],
                             self.requirements.boundary.digest((self.root / name).read_bytes()))
        self.interview_scenario()
        self.assert_code(self.frontend('requirements'), 2)
        self.assertEqual(self.kinds(), ['clarify'])
        self.assert_data_context(self.calls()[0])
        self.assertFalse(self.document().exists())

    def test_data_findings_and_actual_conflicting_answers_use_the_existing_question_flow(self):
        self.data_scenario(test_boundary.QUESTION)
        self.assert_code(self.frontend('requirements'), 3)
        first_answer = self.base / 'conflicting actual answer.txt'
        first_answer.write_text('Fill missing readings with zero.', encoding='utf-8')
        follow_up = dict(test_boundary.EMPTY, questions=[{
            'template': 'T12',
            'question': 'The interview says unavailable, but the latest answer says zero. Which rule governs missing readings?',
            'decides': 'The agreed missing-reading result', 'follow_up_to': 'Q-001'}],
            unresolved=['The actual answer conflicts with the interview.'])
        self.data_scenario(follow_up)
        self.assert_code(self.frontend('requirements', '--answer', 'Q-001', '--answer-file', str(first_answer)), 3)
        self.assertFalse(self.document().exists())
        unresolved = self.requirements.boundary.load_state(self.root)
        self.assertEqual(unresolved['status'], 'needs_answers')
        self.assertEqual(unresolved['questions'][-1]['follow_up_to'], 'Q-001')
        self.assertEqual(unresolved['answers'][0]['text'], first_answer.read_text(encoding='utf-8'))
        final_answer = self.base / 'resolved actual answer.txt'
        final_answer.write_text('Use unavailable without a numeric value; never replace a missing reading with zero.', encoding='utf-8')
        self.data_scenario()
        self.assert_code(self.frontend('requirements', '--answer', 'Q-002', '--answer-file', str(final_answer)), 3)
        self.confirm_both()
        self.assertEqual(self.kinds(), ['clarify', 'clarify', 'clarify', 'requirements', 'functional'])
        for call in self.calls():
            self.assert_data_context(call)
        for call in self.calls()[-3:]:
            answers = self.context(call)['actual_answers']
            self.assertEqual(answers[-1]['text'], final_answer.read_text(encoding='utf-8'))
            self.assertEqual(answers[0]['text'], first_answer.read_text(encoding='utf-8'))
        for name in ('measurements.csv', 'baseline.csv'):
            self.assertIn(name, self.state()['input_hashes'])
            self.assertIn(name, self.state(stage='functional')['input_hashes'])
        self.assert_no_implementation()

    def test_changed_csv_invalidates_both_approvals_before_reanalysis(self):
        self.assert_code(self.frontend('requirements'), 3)
        self.confirm_both()
        old_returned = self.functional_return()
        old_calls = self.kinds()
        previous_revision = self.state()['revision']
        self.fixture.write('measurements.csv', MEASUREMENTS.replace('3,1000', '3,2000'))
        status = self.assert_code(self.frontend('requirements', '--status'), 0)
        state = json.loads(status.stdout)
        self.assertEqual(state['requirements_status'], 'STALE')
        self.assertEqual(state['functional_status'], 'STALE')
        self.assert_code(self.frontend('requirements', '--confirm-functional', str(old_returned)), 2)
        self.assertEqual(self.kinds(), old_calls)
        self.data_scenario(markdown=fixtures.DOCUMENT + 'Review the updated measurements.\n',
                           functional=fixtures.FUNCTIONAL + 'F-02: Preserve updated measurement values.\n')
        self.assert_code(self.frontend('requirements'), 3)
        self.assertEqual(self.state()['revision'], previous_revision + 1)
        self.assertEqual(self.state()['status'], 'WAITING_CONFIRMATION')
        self.assertEqual(self.state(current=False, stage='functional')['status'], 'STALE')
        self.confirm_both()
        self.assertEqual(self.kinds(), old_calls + ['clarify', 'requirements', 'functional'])
        for call in self.calls()[-3:]:
            self.assert_data_context(call)

    def test_interrupted_functional_registration_rebuilds_the_same_data_context(self):
        # The crash helper imports this fixture's runtime; resume with that same runtime.
        self.assert_code(self.cli(), 3)
        _, journal = test_functional_registration.FunctionalRegistrationTests.interrupt_registration(self)
        self.assertIn('data_checks', journal['context'])
        self.assertEqual(self.kinds(), ['clarify', 'requirements'])
        self.assertFalse((self.root / self.requirements.FUNCTIONAL_STATE).exists())
        self.assert_code(self.cli(), 3)
        self.assertEqual(self.kinds(), ['clarify', 'requirements', 'functional'])
        state = self.state(stage='functional')
        self.assertEqual(state['directory'], journal['directory'])
        self.assertEqual(state['status'], 'WAITING_CONFIRMATION')
        self.assert_data_context(self.calls()[-1])
        self.assertEqual(self.context(self.calls()[-1])['data_checks'], journal['context']['data_checks'])
        self.assert_code(self.cli('--confirm-functional', str(self.functional_return())), 0)
        self.assert_no_implementation()


if __name__ == '__main__':
    unittest.main()
