"""Generated development documents retain human explanations and real evidence."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / 'development_documents.py'
loader = importlib.util.spec_from_file_location('tested_development_documents', MODULE)
documents = importlib.util.module_from_spec(loader)
loader.loader.exec_module(documents)


class DevelopmentDocumentsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.app = self.base / 'app-project'
        self.problem = self.base / 'problem-project'
        self.app.mkdir()
        self.problem.mkdir()
        (self.app / 'src').mkdir()
        (self.app / 'src/service.py').write_text(
            'class Service:\n    def load(self):\n        return 3\n\ndef read():\n    return Service().load()\n',
            encoding='utf-8')
        self.flow = {'id': 'F-001', 'feature': '결과 조회', 'entry': 'GET /results',
                     'chain': [{'file': 'src/service.py', 'function': 'read'},
                               {'file': 'src/service.py', 'function': 'Service.load'}],
                     'reads': ['results 테이블'], 'writes': []}
        self.state = {'plan': {'summary': '확정 명세 구현', 'architecture': {'language': 'Python 3.9'},
                              'tasks': [{'id': 'T-001', 'title': '결과 조회 구현',
                                         'requirements': ['R-001'], 'files': ['src/service.py'],
                                         'test_files': ['tests/test_service.py'], 'checks': [],
                                         'expected_minutes': 10}],
                              'run_commands': [['python', '-m', 'src.service']]},
                      'flows': [self.flow], 'history': [], 'decisions': [], 'checks': [], 'reviews': []}

    def publish_understanding(self, text):
        target = self.app / 'doc/UNDERSTANDING.md'
        target.parent.mkdir(exist_ok=True)
        target.write_text(text, encoding='utf-8')
        return target

    def test_render_is_pure_and_has_exact_flow_columns(self):
        result = documents.render_documents(self.app, self.problem, self.state)
        self.assertIn('doc/UNDERSTANDING.md', result)
        self.assertIn('| 기능 | 시작점(화면, URL, 명령) | 거치는 모듈 순서(파일:함수 > 파일:함수) | 저장, 읽는 곳 | 내 한 줄 설명 |',
                      result['doc/UNDERSTANDING.md'])
        self.assertFalse((self.app / 'doc').exists())
        self.assertEqual(self.state['flows'], [self.flow])

    def test_human_explanation_survives_flow_rename_and_reorder(self):
        first = documents.render_documents(self.app, self.problem, self.state)['doc/UNDERSTANDING.md']
        first = first.replace('<!-- flow:F-001 -->', '<!-- flow:F-001 -->')
        rows = first.splitlines()
        index = next(i for i, line in enumerate(rows) if '<!-- flow:F-001 -->' in line)
        rows[index] = rows[index].rsplit('|', 2)[0] + '| 내가 쓴 설명: 결과를 읽어 보여준다. |'
        self.publish_understanding('\n'.join(rows) + '\n')
        updated = copy.deepcopy(self.state)
        updated['flows'][0]['feature'] = '조회 화면'
        updated['flows'].insert(0, dict(self.flow, id='F-002', feature='두 번째 조회'))
        result = documents.render_documents(self.app, self.problem, updated)['doc/UNDERSTANDING.md']
        self.assertIn('조회 화면', result)
        self.assertIn('내가 쓴 설명: 결과를 읽어 보여준다.', result)
        self.publish_understanding(result)
        self.assertEqual(documents.read_explanations(self.app)['F-001'], '내가 쓴 설명: 결과를 읽어 보여준다.')

    def test_pipe_and_linebreak_in_human_explanation_are_preserved(self):
        first = documents.render_documents(self.app, self.problem, self.state)['doc/UNDERSTANDING.md']
        rows = first.splitlines()
        index = next(i for i, line in enumerate(rows) if '<!-- flow:F-001 -->' in line)
        rows[index] = rows[index].rsplit('|', 2)[0] + '| A \\| B<br>두 번째 줄 |'
        self.publish_understanding('\n'.join(rows) + '\n')
        result = documents.render_documents(self.app, self.problem, self.state)['doc/UNDERSTANDING.md']
        self.publish_understanding(result)
        self.assertEqual(documents.read_explanations(self.app)['F-001'], 'A | B\n두 번째 줄')

    def test_removed_flow_keeps_human_explanation(self):
        self.publish_understanding('| 예전 기능 <!-- flow:F-009 --> | CLI | old.py:main | 없음 | 삭제해도 보존할 설명 |\n')
        result = documents.render_documents(self.app, self.problem, self.state)['doc/UNDERSTANDING.md']
        self.assertIn('삭제해도 보존할 설명', result)
        self.assertIn('이전 기능의 설명', result)
        self.publish_understanding(result)
        self.assertEqual(documents.read_explanations(self.app)['F-009'], '삭제해도 보존할 설명')

    def test_unknown_function_or_path_cannot_be_documented_as_real(self):
        invalid = copy.deepcopy(self.flow)
        invalid['chain'][0]['function'] = 'does_not_exist'
        with self.assertRaisesRegex(ValueError, 'function|함수'):
            documents.validate_flows(self.app, [invalid])
        invalid['chain'][0] = {'file': '../outside.py', 'function': 'main'}
        with self.assertRaises(ValueError):
            documents.validate_flows(self.app, [invalid])

    def test_function_name_in_comment_does_not_pass_python_validation(self):
        (self.app / 'src/service.py').write_text('# def imagined(): pass\nVALUE = "imagined"\n', encoding='utf-8')
        invalid = copy.deepcopy(self.flow)
        invalid['chain'] = [{'file': 'src/service.py', 'function': 'imagined'}]
        with self.assertRaisesRegex(ValueError, 'function|함수'):
            documents.validate_flows(self.app, [invalid])

    def test_duplicate_flow_id_or_malformed_human_row_stops_publication(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate|중복'):
            documents.validate_flows(self.app, [self.flow, self.flow])
        self.publish_understanding('| 기능 <!-- flow:F-001 --> | 잘못된 | 표 |\n')
        with self.assertRaises(ValueError):
            documents.render_documents(self.app, self.problem, self.state)

    def test_changelog_does_not_accept_model_claim_without_test_and_review(self):
        self.state['history'] = [
            {'task_id': 'T-001', 'summary': '모델이 완료라고 답함', 'files': ['src/service.py'], 'status': 'complete'},
            {'task_id': 'T-002', 'summary': '검사와 검토 완료', 'files': ['src/service.py'],
             'tests_passed': True, 'review_approved': True},
        ]
        result = documents.render_documents(self.app, self.problem, self.state)['doc/CHANGELOG.md']
        self.assertIn('모델이 완료라고 답함', result)
        self.assertIn('검증 대기', result)
        self.assertEqual(result.count('| 수정 완료 |'), 1)

    def test_decisions_show_costs_claim_verification_and_limitations(self):
        self.state['decisions'] = [{'id': 'D-001', 'time': '10:05', 'before': '파일 저장', 'after': 'SQLite',
                                    'reason': '동시 갱신 확인', 'discarded': ['메모리만 사용'], 'expected_minutes': 8,
                                    'status': 'approved', 'actual': {'minutes': 9, 'files': 2, 'retested': 'unittest'}}]
        self.state['verification_notes'] = [{'time': '10:07', 'claim': '빈 입력도 정상',
                                             'method': '빈 CSV 재현', 'result': '실패 1건 확인 후 수정'}]
        self.state['limitations'] = ['큰 입력의 장시간 실행은 미검증']
        result = documents.render_documents(self.app, self.problem, self.state)['doc/DECISIONS.md']
        for text in ['파일 저장', 'SQLite', '메모리만 사용', '예상 비용', '실제 비용', '빈 CSV 재현', '큰 입력']:
            self.assertIn(text, result)

    def test_report_renders_real_commands_and_missing_counts_as_unverified(self):
        self.state['checks'] = [{'passed': True, 'checks': [{'id': 'test', 'argv': ['python', '-m', 'unittest'],
                                                           'exit_code': 0, 'passed': True}]}]
        result = documents.render_documents(self.app, self.problem, self.state)['doc/VERIFICATION_REPORT.md']
        self.assertIn('unittest', result)
        self.assertIn('미기록', result)

    def test_hash_changes_only_with_actual_understanding_bytes(self):
        self.assertIsNone(documents.understanding_hash(self.app))
        path = self.publish_understanding('사용자 설명\n')
        original = documents.understanding_hash(self.app)
        path.write_text('수정한 사용자 설명\n', encoding='utf-8')
        self.assertNotEqual(original, documents.understanding_hash(self.app))

    def test_initial_choices_and_invalidated_current_tasks_are_distinct_from_history(self):
        self.state['plan']['decisions'] = [{'decision': 'SQLite 선택', 'reason': '동시 갱신 필요', 'discarded': ['CSV만 사용']}]
        self.state['revision'] = 2
        self.state['tasks'] = {}
        self.state['history'] = [{'task_id': 'T-001', 'files': ['src/service.py'], 'summary': '이전 요구사항 구현',
                                  'tests_passed': True, 'review_approved': True}]
        self.state['input_changes'] = [{'time': '10:05', 'previous_revision': 1, 'reason': '담당자가 조건 수정'}]
        rendered = documents.render_documents(self.app, self.problem, self.state)
        self.assertIn('SQLite 선택', rendered['doc/DECISIONS.md'])
        self.assertIn('CSV만 사용', rendered['doc/DECISIONS.md'])
        self.assertIn('담당자가 조건 수정', rendered['doc/DECISIONS.md'])
        self.assertIn('당시 상태', rendered['doc/CHANGELOG.md'])
        self.assertIn('리비전: 2', rendered['doc/CHANGELOG.md'])
        self.assertIn('| T-001 결과 조회 구현 | 미완료 |', rendered['doc/CHANGELOG.md'])

    def test_report_sums_only_reported_tokens_without_double_counting_cache(self):
        self.state['usage'] = [
            {'role': 'implementer', 'run_id': 'A', 'usage': {'reported': True, 'input_tokens': 12, 'cached_input_tokens': 3, 'output_tokens': 5}},
            {'role': 'reviewer', 'run_id': 'B', 'usage': {'reported': False, 'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0}},
            {'role': 'coordinator', 'run_id': 'C', 'usage': None},
        ]
        result = documents.render_documents(self.app, self.problem, self.state)['doc/VERIFICATION_REPORT.md']
        self.assertIn('| implementer | A | 12 | 3 | 5 | 17 |', result)
        self.assertIn('| reviewer | B | 미보고 | 미보고 | 미보고 | 미보고 |', result)
        self.assertIn('| 보고된 값 합계 |  | 12 | 3 | 5 | 17 |', result)
        self.assertIn('입력 1/3회', result)

    def test_report_partial_usage_is_not_changed_to_zero(self):
        self.state['usage'] = [{'role': 'implementer', 'run_id': 'A',
                               'usage': {'reported': True, 'input_tokens': 12, 'cached_input_tokens': None, 'output_tokens': None}}]
        result = documents.render_documents(self.app, self.problem, self.state)['doc/VERIFICATION_REPORT.md']
        self.assertIn('| implementer | A | 12 | 미보고 | 미보고 | 미보고 |', result)

    def test_real_check_measurement_and_nested_docker_report_are_rendered(self):
        self.state['checks'] = [{'status': 'PASS', 'stages': [
            {'stage': 'build', 'outcome': {'exit_code': 0, 'stdout_path': 'build.stdout.log'}},
            {'stage': 'verify', 'report': {'checks': [
                {'id': 'business', 'argv': ['docker', 'exec', 'app', 'python', '-m', 'unittest'],
                 'test_framework': 'unittest', 'passed': True,
                 'measurement': {'measured': True, 'tests': 4, 'errors': 0, 'failures': 0, 'skipped': 0},
                 'outcome': {'exit_code': 0, 'stdout_path': 'verify.stdout.log'}}]}}
        ]}]
        result = documents.render_documents(self.app, self.problem, self.state)['doc/VERIFICATION_REPORT.md']
        self.assertIn('business', result)
        self.assertIn('docker', result)
        self.assertIn('| 0 | 4 | 통과 |', result)
        self.assertIn('verify.stdout.log', result)

    def test_plan_displays_assurance_classification_contract_and_probe_for_approval(self):
        self.state['plan']['assurance'] = {
            'project_type': 'non_web', 'reason': '명령형 분석 도구',
            'environment': {'executables': ['python'], 'python_packages': [], 'ports': []},
            'joins': [], 'join_reason': '연결할 파일 없음',
            'reconciliations': [], 'reconciliation_reason': '행 분류 없음',
            'algorithms': [{'after_task': 'T-001', 'contract': {'id': 'quality', 'max_regression': 2,
                'candidates': ['baseline', 'candidate'], 'constraints': [{'name': 'load', 'max': 10}]},
                'measurements': 'measurements.json', 'probe': {'argv': ['python', 'verify.py', '{artifacts}'],
                    'cwd': '.', 'timeout_seconds': 120}}],
            'algorithm_reason': '비용 퇴보를 확인해야 함', 'browser': None}
        result = documents.render_documents(self.app, self.problem, self.state)['doc/IMPLEMENTATION_PLAN.md']
        for value in ['추가 검사 계약', 'non_web', '연결할 파일 없음', '비용 퇴보를 확인해야 함',
                      '"max_regression": 2', '"max": 10', '"timeout_seconds": 120', '{artifacts}', 'T-001']:
            self.assertIn(value, result)

    def test_environment_and_join_report_use_recorded_counts_and_missing_status(self):
        self.state['environment'] = {'status': 'BLOCKED', 'passed': False, 'python': {'version': '3.9.25'},
            'missing_required': ['executable:missing'], 'installed_package_count': 21, 'report_path': '.harness/environment.json'}
        self.state['plan_checks'] = {'seal': 'approved-contract-hash', 'joins': {'status': 'FAIL',
            'checks': [{'id': 'join-1', 'kind': 'join', 'status': 'FAIL', 'passed': False,
                       'counts': {'left_rows': 20, 'right_rows': 10, 'projected_rows': 24, 'unmatched_left_rows': 2,
                                  'left_duplicate_keys': 1, 'right_duplicate_keys': 3},
                       'violations': [{'code': 'right_key_not_unique', 'count': 3}]}], 'inputs': {'input.csv': 'hash'}}}
        result = documents.render_documents(self.app, self.problem, self.state)['doc/VERIFICATION_REPORT.md']
        for value in ['BLOCKED', 'executable:missing', '21', 'approved-contract-hash', '왼쪽 행 20', '연결 예상 행 24',
                      '미연결 행 2', 'right_key_not_unique']:
            self.assertIn(value, result)

    def test_ledger_measurements_and_evidence_are_in_report(self):
        self.state['assurance_runs'] = {'task-run': {'status': 'FAIL', 'passed': False, 'reports': [{
            'kind': 'reconciliation', 'id': 'ledger', 'task_id': 'T-001', 'status': 'FAIL', 'passed': False,
            'run_dir': 'D:/project/.harness/assurance/task-run', 'outcome': {'exit_code': 0,
                'cleanup': {'completed': True}, 'stdout_path': 'producer.stdout.log', 'prefix': 'producer'},
            'details': {'checks': [{'id': 'ledger', 'kind': 'input_ledger', 'status': 'FAIL', 'passed': False,
                'counts': {'input_rows': 10, 'output_rows': 9, 'missing_occurrences': 1, 'excess_occurrences': 0, 'unknown_keys': 0},
                'parts': [{'file': 'used.csv', 'kind': 'used', 'rows': 9, 'sha256': 'ledger-hash'}],
                'violations': [{'code': 'input_occurrences_missing', 'count': 1}]}]}, 'artifacts': {'used.csv': 'ledger-hash'}}]}}
        result = documents.render_documents(self.app, self.problem, self.state)['doc/VERIFICATION_REPORT.md']
        for value in ['입력 행 10', '대장 행 9', '누락 1', 'used.csv', 'ledger-hash', 'producer.command.json', 'report.json']:
            self.assertIn(value, result)

    def test_algorithm_measured_values_and_unknown_exact_are_distinguished(self):
        self.state['assurance_runs'] = {'final-run': {'status': 'PASS', 'passed': True, 'results': [{
            'kind': 'algorithm', 'id': 'quality', 'status': 'PASS', 'passed': True,
            'run_dir': 'D:/project/.harness/assurance/quality', 'details': {'case_count': 1, 'candidate_count': 2,
                'measurement_count': 2, 'cases': [{'case_id': 'small', 'passed': True,
                    'candidates': [{'id': 'baseline', 'objective': 10, 'feasible': True, 'violations': []},
                                   {'id': 'candidate', 'objective': 8, 'feasible': True, 'violations': []}],
                    'absolute_regression': -2, 'exact': None, 'relative_gap': None, 'exact_status': 'not_measured', 'reasons': []}],
                'evidence': {'measurements_sha256': 'measured-hash'}}}]}}
        result = documents.render_documents(self.app, self.problem, self.state)['doc/VERIFICATION_REPORT.md']
        for value in ['측정 수: 2', 'baseline', 'candidate', '10', '8', '-2', '정확값 미측정', '격차 미측정', 'measured-hash']:
            self.assertIn(value, result)

    def test_browser_case_image_paths_and_blocked_reasons_are_preserved(self):
        self.state['assurance_runs'] = {
            'blocked': {'status': 'BLOCKED', 'passed': False, 'reports': [{'kind': 'browser', 'passed': False, 'status': 'BLOCKED',
                'details': {'status': 'BLOCKED', 'reason': 'Playwright unavailable', 'cases': []}}]},
            'done': {'status': 'PASS', 'passed': True, 'reports': [{'kind': 'browser', 'passed': True, 'status': 'PASS',
                'details': {'status': 'PASS', 'run_dir': 'D:/project/.harness/browser', 'cases': [{'id': 'screen', 'path': '/',
                    'width': 1280, 'height': 720, 'passed': True, 'assertions': [{'selector': 'h1', 'passed': True}],
                    'layout': {'overflow': 0}, 'issues': [], 'screenshot': 'case-screen.png'}],
                    'artifacts': [{'path': 'D:/project/.harness/browser/case-screen.png', 'kind': 'screenshot'}]}}]}}
        result = documents.render_documents(self.app, self.problem, self.state)['doc/VERIFICATION_REPORT.md']
        for value in ['BLOCKED', 'Playwright unavailable', 'case-screen.png', '1280×720', 'overflow', 'h1']:
            self.assertIn(value, result)

    def test_legacy_assurance_passed_flag_is_not_presented_as_new_checks_pass(self):
        self.state['assurance_runs'] = {'legacy': {'status': 'LEGACY', 'passed': True, 'reports': []}}
        result = documents.render_documents(self.app, self.problem, self.state)
        self.assertIn('추가 검사 미설정', result['doc/IMPLEMENTATION_PLAN.md'])
        self.assertIn('### 실행 legacy\n\n판정: 추가 검사 미설정', result['doc/VERIFICATION_REPORT.md'])

    def test_readiness_reports_preserve_blocked_reason_measurements_and_evidence(self):
        self.state['readiness_reports'] = [
            {'signature': 'plan-hash', 'report': {'status': 'BLOCKED', 'passed': False,
                'reason': 'Docker engine unavailable', 'run_dir': 'D:/project/.harness/readiness/1',
                'environment': {'python': {'version': '3.9.25'}, 'missing_required': ['executable:missing'],
                    'executables': [{'name': 'python', 'available': True}], 'python_packages': [], 'ports': []},
                'docker': {'status': 'BLOCKED', 'passed': False}, 'compose': None, 'browser': None}},
            {'signature': 'plan-hash', 'report': {'status': 'PASS', 'passed': True, 'reason': '',
                'run_dir': 'D:/project/.harness/readiness/2', 'environment': {'python': {'version': '3.9.25'},
                    'missing_required': [], 'executables': [], 'python_packages': [], 'ports': [{'port': 8765, 'available': True}]},
                'docker': {'status': 'PASS', 'passed': True}, 'compose': None,
                'browser': {'passed': True, 'availability': {'browser': 'chromium', 'version': 'fixture-version', 'reason': ''}}}}]
        result = documents.render_documents(self.app, self.problem, self.state)['doc/VERIFICATION_REPORT.md']
        for value in ['1 / BLOCKED', '2 / PASS', 'Docker engine unavailable', 'executable:missing', '3.9.25',
                      '8765', 'chromium', 'fixture-version', '해당 없음', '.harness/readiness/1', '.harness/readiness/2']:
            self.assertIn(value, result)
        self.assertIn('이후 기능·Docker·실제 브라우저 검사를 대신하지 않습니다', result)

    def test_assurance_plan_does_not_replace_original_deadline_with_fixed_minutes(self):
        self.state['plan']['assurance'] = {'project_type': 'non_web'}
        result = documents.render_documents(self.app, self.problem, self.state)['doc/IMPLEMENTATION_PLAN.md']
        self.assertIn('접수 당시 마감', result)
        self.assertNotIn('150분', result)
        self.assertNotIn('160분', result)


if __name__ == '__main__':
    unittest.main()
