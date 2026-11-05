"""Removing provider-specific string constraints must not weaken local validation."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('document_validation', SOURCE / 'requirements.py')
requirements = importlib.util.module_from_spec(spec)
spec.loader.exec_module(requirements)


class DocumentValidationTests(unittest.TestCase):
    def test_empty_whitespace_and_wrong_type_are_rejected_for_both_documents(self):
        for stage, key in (('requirements', 'requirements_markdown'), ('functional', 'functional_spec_markdown')):
            journal = {'directory': '.harness/work/' + stage + '-' + 'a' * 32}
            for invalid in ('', ' \t\r\n ', None, 0, [], {}):
                with self.subTest(stage=stage, invalid=invalid):
                    with mock.patch.object(requirements.clarify, '_execution_result', return_value={key: invalid}):
                        with self.assertRaisesRegex(requirements.boundary.BoundaryError, 'Invalid document model result'):
                            requirements._report(Path('.'), journal)

    def test_nonempty_korean_document_is_preserved(self):
        for stage, key in (('requirements', 'requirements_markdown'), ('functional', 'functional_spec_markdown')):
            journal = {'directory': '.harness/work/' + stage + '-' + 'b' * 32}
            report = {key: '# 미결 사항\n담당자의 실제 답변에 따라 목표값 결정을 보류합니다.\n'}
            with self.subTest(stage=stage):
                with mock.patch.object(requirements.clarify, '_execution_result', return_value=report):
                    self.assertEqual(requirements._report(Path('.'), journal), report)


if __name__ == '__main__':
    unittest.main()
