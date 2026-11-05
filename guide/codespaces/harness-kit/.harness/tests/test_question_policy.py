"""Question wording, recorded-answer compatibility and explicit deferral flow."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import test_boundary
import test_interview


boundary = test_boundary.boundary


class QuestionPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='question-policy-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / 'SPEC.md').write_text('API: /tenants/{tenant_id}/ask\n', encoding='utf-8')
        (self.root / boundary.TEMPLATE).write_text('T01 through T15\n', encoding='utf-8')
        self.state = boundary.create_state(self.root, 'SPEC.md')

    def report(self, question):
        report = copy.deepcopy(test_boundary.QUESTION)
        report['questions'][0]['question'] = question
        return report

    def test_data_rows_production_lines_and_interview_sections_are_allowed(self):
        for wording in ('data/mon.csv 32행 값 30.0은 허용되는 측정값인가요?',
                        '생산 line 3의 값은 다른 공정과 같은 기준으로 비교하나요?',
                        '인터뷰 3절 R-01의 응답 시간 목표를 어떻게 정하나요?',
                        'Does interview section 3 govern the response?',
                        'What does /tenants/{tenant_id}/ask return?'):
            for field in ('question', 'decides'):
                report = self.report('측정값의 허용 범위는 무엇인가요?')
                report['questions'][0][field] = wording
                with self.subTest(wording=wording, field=field):
                    boundary.validate_report(self.root, report, self.state)

    def test_separate_source_labels_pages_and_unfilled_placeholders_still_fail(self):
        for wording in ('See page 3: what happens?', '4페이지: 조건은 무엇인가요?',
                        '질문\n근거: SPEC.md 4절', '질문\n출처 위치: data/mon.csv',
                        'What should {대상} do?'):
            with self.subTest(wording=wording), self.assertRaises(boundary.BoundaryError):
                boundary.validate_report(self.root, self.report(wording), self.state)

    def test_new_question_labels_are_korean_and_actual_answers_are_unchanged(self):
        boundary.complete_analysis(self.root, self.state, self.report('측정값이 없으면 어떻게 하나요?'))
        document = (self.root / boundary.ANSWERS).read_text(encoding='utf-8')
        self.assertIn('질문: 측정값이 없으면 어떻게 하나요?', document)
        self.assertIn('실제 답변: 대기 중', document)
        answer = 'Return unavailable. 실제 답변 원문은 그대로 보존합니다.'
        boundary.submit_answer(self.root, 'Q-001', answer)
        document = (self.root / boundary.ANSWERS).read_text(encoding='utf-8')
        self.assertIn('실제 답변 1:\n\n' + answer, document)
        self.assertNotIn('User answer:', document)
        self.assertEqual(boundary.actual_answers(boundary.load_state(self.root))['Q-001'], answer)

    def test_legacy_answer_bytes_stay_valid_until_an_actual_answer_changes(self):
        report = self.report('측정값이 없으면 어떻게 하나요?')
        state = boundary.analysis_state(self.root, self.state, report)
        legacy = ('# Boundary questions and actual answers\n\nSpecification: SPEC.md\n\n'
                  'Only answers explicitly submitted by the user appear below.\n\n'
                  '## Q-001 | T03\n\nQuestion: 측정값이 없으면 어떻게 하나요?\n\n'
                  'Determines: The response for unavailable measurements\n\nUser answer: pending\n')
        state['answers_document_sha256'] = boundary.digest(legacy.encode('utf-8'))
        boundary.atomic_write(self.root, boundary.STATE, json.dumps(state).encode('utf-8'))
        boundary.atomic_write(self.root, boundary.ANSWERS, legacy.encode('utf-8'))
        self.assertEqual(boundary.render_answers(boundary.load_state(self.root)), legacy)
        boundary.atomic_write(self.root, boundary.ANSWERS, (legacy + 'unexpected').encode('utf-8'))
        with self.assertRaises(boundary.BoundaryError):
            boundary.load_state(self.root)
        boundary.atomic_write(self.root, boundary.ANSWERS, legacy.encode('utf-8'))
        boundary.submit_answer(self.root, 'Q-001', '값 없이 제공 불가로 표시합니다.')
        saved = boundary.load_state(self.root)
        self.assertIn('실제 답변 1:', boundary.render_answers(saved))
        self.assertEqual(saved['questions'], state['questions'])


class DeferralFlowTests(test_interview.InterviewFixture, unittest.TestCase):
    def test_explicit_deferral_preserves_actual_answers_and_both_confirmation_stops(self):
        question = copy.deepcopy(test_boundary.QUESTION)
        question['questions'][0].update(template='T06', question='응답 시간 목표를 얼마로 정하나요?',
                                        decides='응답 시간의 필수 합격 기준')
        question['unresolved'] = ['응답 시간 목표가 미정입니다.']
        self.interview_scenario(question)
        self.assert_code(self.frontend('requirements'), 3)
        answer_file = self.base / 'actual answer.txt'
        answer_file.write_text('아직 모른다.', encoding='utf-8')
        follow = copy.deepcopy(test_boundary.QUESTION)
        follow['questions'][0].update(template='T12', follow_up_to='Q-001',
                                     question='응답 시간 목표를 미정으로 두고 문서 작성을 진행할까요?',
                                     decides='이 항목의 보류 및 진행 여부')
        follow['unresolved'] = ['보류하고 진행해도 되는지 확인이 필요합니다.']
        self.interview_scenario(follow)
        self.assert_code(self.frontend('requirements', '--answer', 'Q-001', '--answer-file', str(answer_file)), 3)
        self.assertFalse(self.document().exists())
        answer = '응답 시간 목표는 미정으로 두고 나머지 문서 작성을 진행하세요.'
        answer_file.write_text(answer, encoding='utf-8')
        self.interview_scenario(markdown='# 요구사항 초안\n\n## 미결 사항\n응답 시간 목표는 Q-002의 실제 답변으로 보류합니다.\n',
                                functional='# 기능명세서 초안\n\n## 미결 사항\n응답 시간 합격 기준은 미정입니다. Q-002에서 보류를 요청했습니다.\n')
        self.assert_code(self.frontend('requirements', '--answer', 'Q-002', '--answer-file', str(answer_file)), 3)
        self.assertEqual(self.state()['status'], 'WAITING_CONFIRMATION')
        self.assertIn('## 미결 사항', self.document().read_text(encoding='utf-8'))
        self.confirm_both()
        actual = boundary.actual_answers(boundary.load_state(self.root))
        self.assertEqual(actual, {'Q-001': '아직 모른다.', 'Q-002': answer})
        for call in self.calls():
            # Check the policy reaches every worker, not live-model semantic accuracy.
            self.assertIn('Korean', call['prompt'])
            self.assertIn('미결 사항', call['prompt'])
            self.assertIn('아직 모른다', call['prompt'])
        self.assert_no_implementation()


if __name__ == '__main__':
    unittest.main()
