"""Actual CSV fixtures for join cardinality and input disposition ledgers."""
import copy
import csv
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('test_assurance_data_module', SOURCE / 'assurance_data.py')
data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(data)


class AssuranceDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='assurance-data-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.problem, self.artifacts = self.root / 'problem', self.root / 'artifacts'
        self.problem.mkdir()
        self.artifacts.mkdir()

    def csv(self, name, rows, header=('id',), root=None):
        path = (root or self.problem) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = io.StringIO(newline='')
        writer = csv.writer(text)
        writer.writerow(header)
        writer.writerows(rows)
        path.write_bytes(text.getvalue().encode('utf-8'))
        return path

    def join(self, **changes):
        contract = dict(id='J1', left='left.csv', right='right.csv', left_keys=['id'], right_keys=['id'],
            relationship='many_to_one', join_type='left', allow_unmatched=False, allow_blank_keys=False, expected_rows=None)
        contract.update(changes)
        return contract

    def ledger(self, **changes):
        contract = dict(id='L1', input='input.csv', keys=['id'], parts=[
            {'file': 'used.csv', 'kind': 'used'}, {'file': 'excluded.csv', 'kind': 'excluded'},
            {'file': 'quarantined.csv', 'kind': 'quarantined'}])
        contract.update(changes)
        return contract

    def outputs(self, used=(), excluded=(), quarantined=()):
        for name, values in (('used', used), ('excluded', excluded), ('quarantined', quarantined)):
            self.csv(name + '.csv', [(value,) for value in values], root=self.artifacts)

    def inspect(self, contract=None):
        return data.inspect_joins(self.problem, [contract or self.join()])

    def reconcile(self, contract=None):
        return data.reconcile(self.problem, self.artifacts, [contract or self.ledger()])

    def test_many_to_one_detects_master_duplicates_and_join_row_inflation(self):
        self.csv('left.csv', [('001',), ('001',), ('002',)])
        self.csv('right.csv', [('001',), ('001',), ('002',)])
        report = self.inspect()
        check = report['checks'][0]
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(check['counts']['projected_rows'], 5)
        self.assertEqual(check['counts']['right_duplicate_keys'], 1)
        self.assertEqual(check['violations'], [{'code': 'right_key_not_unique', 'count': 1}])
        self.assertEqual(check['examples']['right_duplicates'][0]['key'], ['001'])

    def test_one_to_many_is_allowed_when_declared_and_expected_rows_match(self):
        self.csv('left.csv', [('A',), ('B',)])
        self.csv('right.csv', [('A',), ('A',), ('B',)])
        report = self.inspect(self.join(relationship='one_to_many', expected_rows=3))
        self.assertTrue(report['passed'])
        self.assertEqual(report['checks'][0]['counts']['inner_rows'], 3)
        self.assertFalse(self.inspect(self.join(relationship='one_to_many', expected_rows=2))['passed'])

    def test_relationship_constraints_are_directional(self):
        self.csv('left.csv', [('A',), ('A',)])
        self.csv('right.csv', [('A',), ('A',)])
        self.assertTrue(self.inspect(self.join(relationship='many_to_many'))['passed'])
        check = self.inspect(self.join(relationship='one_to_one'))['checks'][0]
        self.assertEqual({v['code'] for v in check['violations']}, {'left_key_not_unique', 'right_key_not_unique'})
        self.csv('right.csv', [('A',)])
        self.assertTrue(self.inspect(self.join(relationship='many_to_one'))['passed'])
        self.assertFalse(self.inspect(self.join(relationship='one_to_many'))['passed'])

    def test_unmatched_left_and_unused_master_are_reported_separately(self):
        self.csv('left.csv', [('A',), ('B',)])
        self.csv('right.csv', [('A',), ('C',)])
        self.assertFalse(self.inspect()['passed'])
        left = self.inspect(self.join(allow_unmatched=True))['checks'][0]
        inner = self.inspect(self.join(allow_unmatched=True, join_type='inner'))['checks'][0]
        self.assertTrue(left['passed'])
        self.assertEqual(left['counts']['projected_rows'], 2)
        self.assertEqual(inner['counts']['projected_rows'], 1)
        self.assertEqual(left['counts']['unused_right_keys'], 1)
        self.csv('left.csv', [('A',)])
        self.assertTrue(self.inspect()['passed'])

    def test_blank_composite_keys_never_join_even_when_explicitly_allowed(self):
        self.csv('left.csv', [('A', ''), ('A', '1')], header=('id', 'part'))
        self.csv('right.csv', [('A', ''), ('A', '1')], header=('id', 'part'))
        contract = self.join(left_keys=['id', 'part'], right_keys=['id', 'part'])
        self.assertFalse(self.inspect(contract)['passed'])
        contract.update(allow_blank_keys=True)
        check = self.inspect(contract)['checks'][0]
        self.assertTrue(check['passed'])
        self.assertEqual((check['counts']['inner_rows'], check['counts']['left_join_rows']), (1, 2))
        self.assertEqual((check['counts']['left_blank_rows'], check['counts']['right_blank_rows']), (1, 1))

    def test_ids_preserve_leading_zeros_and_whitespace(self):
        self.csv('left.csv', [('001',), ('A ',)])
        self.csv('right.csv', [('1',), ('A',)])
        check = self.inspect()['checks'][0]
        self.assertEqual(check['counts']['unmatched_left_rows'], 2)
        self.assertFalse(check['passed'])

    def test_duplicate_or_missing_headers_are_blocked(self):
        self.csv('left.csv', [('A', 'A')], header=('id', 'id'))
        self.csv('right.csv', [('A',)])
        self.assertEqual(self.inspect()['status'], 'BLOCKED')
        self.csv('left.csv', [('A',)], header=('other',))
        self.assertIn('missing key columns', self.inspect()['checks'][0]['error'])

    def test_invalid_encoding_and_malformed_rows_are_blocked(self):
        self.csv('right.csv', [('A',)])
        (self.problem / 'left.csv').write_bytes(b'id\n\xff\n')
        self.assertEqual(self.inspect()['status'], 'BLOCKED')
        (self.problem / 'left.csv').write_text('id\nA,B\n', encoding='utf-8')
        self.assertIn('row width', self.inspect()['checks'][0]['error'])

    def test_exact_duplicate_input_occurrences_are_preserved_across_parts(self):
        self.csv('input.csv', [('A',), ('A',), ('B',), ('C',)])
        self.outputs(used=['A', 'B'], excluded=['A'], quarantined=['C'])
        report = self.reconcile()
        self.assertTrue(report['passed'])
        self.assertEqual(report['checks'][0]['counts']['input_rows'], 4)
        self.assertEqual(report['checks'][0]['counts']['output_rows'], 4)
        self.assertEqual(set(report['artifacts']), {'used.csv', 'excluded.csv', 'quarantined.csv'})

    def test_same_total_rows_with_different_ids_still_fails(self):
        self.csv('input.csv', [('A',), ('B',)])
        self.outputs(used=['A', 'C'])
        check = self.reconcile()['checks'][0]
        self.assertFalse(check['passed'])
        self.assertEqual(check['counts']['input_rows'], check['counts']['output_rows'])
        self.assertEqual((check['counts']['missing_occurrences'], check['counts']['excess_occurrences']), (1, 1))
        self.assertEqual(check['examples']['missing'][0]['key'], ['B'])
        self.assertEqual(check['examples']['unknown'][0]['key'], ['C'])

    def test_deduplication_or_double_assignment_requires_accounting(self):
        self.csv('input.csv', [('A',), ('A',), ('B',)])
        self.outputs(used=['A', 'B'])
        self.assertEqual(self.reconcile()['checks'][0]['counts']['missing_occurrences'], 1)
        self.outputs(used=['A', 'A', 'B'], excluded=['A'])
        self.assertEqual(self.reconcile()['checks'][0]['counts']['excess_occurrences'], 1)

    def test_empty_parts_allowed_but_missing_parts_or_empty_input_are_not_passes(self):
        self.csv('input.csv', [('A',)])
        self.outputs(used=['A'])
        self.assertTrue(self.reconcile()['passed'])
        (self.artifacts / 'excluded.csv').unlink()
        blocked = self.reconcile()
        self.assertEqual(blocked['status'], 'BLOCKED')
        self.assertEqual(blocked['checks'][0]['stage'], 'artifact')
        self.outputs()
        self.csv('input.csv', [])
        blocked = self.reconcile()
        self.assertEqual(blocked['status'], 'BLOCKED')
        self.assertEqual(blocked['checks'][0]['stage'], 'input')

    def test_blank_ledger_ids_cannot_prove_identity_even_when_counts_match(self):
        self.csv('input.csv', [('',), ('A',)])
        self.outputs(used=['', 'A'])
        check = self.reconcile()['checks'][0]
        self.assertFalse(check['passed'])
        self.assertEqual(check['violations'], [{'code': 'blank_ledger_key_rows', 'count': 2}])

    def test_composite_keys_have_no_delimiter_collision(self):
        self.csv('input.csv', [('A|B', 'C')], header=('id', 'part'))
        self.csv('used.csv', [('A', 'B|C')], header=('id', 'part'), root=self.artifacts)
        contract = self.ledger(keys=['id', 'part'], parts=[{'file': 'used.csv', 'kind': 'used'}])
        self.assertFalse(self.reconcile(contract)['passed'])

    def test_contracts_reject_outside_paths_inferred_policies_and_duplicate_parts(self):
        for changes in ({'left': '../outside.csv'}, {'left': 'C:/outside.csv'}, {'left': 'folder\\file.csv'},
                        {'allow_blank_keys': 1}, {'expected_rows': True}, {'left_keys': ['id', 'id']},
                        {'left_keys': ['id', 'part']}, {'relationship': 'auto'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                data.validate_joins([self.join(**changes)])
        bad = self.ledger(parts=[{'file': 'used.csv', 'kind': 'used'}, {'file': 'USED.csv', 'kind': 'excluded'}])
        with self.assertRaises(ValueError):
            data.validate_reconciliations([bad])
        bad = self.ledger(parts=[{'file': '../used.csv', 'kind': 'used'}])
        with self.assertRaises(ValueError):
            data.validate_reconciliations([bad])

    def test_hashes_describe_original_bytes_and_inputs_are_not_changed(self):
        left = self.csv('left.csv', [('A',)])
        right = self.csv('right.csv', [('A',)])
        originals = {path: path.read_bytes() for path in (left, right)}
        report = self.inspect()
        self.assertEqual(report['inputs']['left.csv'], hashlib.sha256(originals[left]).hexdigest())
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_row_byte_and_total_limits_fail_without_truncation(self):
        self.csv('left.csv', [('A',), ('A',)])
        self.csv('right.csv', [('A',)])
        with mock.patch.object(data, 'MAX_ROWS', 1):
            self.assertEqual(self.inspect()['status'], 'BLOCKED')
        with mock.patch.object(data, 'MAX_BYTES', 3):
            self.assertEqual(self.inspect()['status'], 'BLOCKED')
        size = (self.problem / 'left.csv').stat().st_size
        with mock.patch.object(data, 'MAX_TOTAL_BYTES', size):
            self.assertEqual(self.inspect()['status'], 'BLOCKED')

    def test_examples_are_bounded_and_long_ids_are_only_shortened_for_display(self):
        values = ['K{:02d}'.format(i) + 'x' * 150 for i in range(12)]
        self.csv('left.csv', [(value,) for value in values])
        self.csv('right.csv', [('other',)])
        check = self.inspect()['checks'][0]
        self.assertEqual(check['counts']['unmatched_left_keys'], 12)
        self.assertEqual(len(check['examples']['unmatched_left']), 5)
        self.assertTrue(all(item['key_truncated'] and len(item['key'][0]) == 81 for item in check['examples']['unmatched_left']))

    def test_hardlinked_input_is_rejected(self):
        original = self.csv('original.csv', [('A',)])
        self.csv('right.csv', [('A',)])
        try:
            os.link(str(original), str(self.problem / 'left.csv'))
        except (OSError, NotImplementedError) as exc:
            self.skipTest('Hard links unavailable: ' + str(exc))
        self.assertEqual(self.inspect()['status'], 'BLOCKED')

    def test_redirected_artifact_file_is_rejected(self):
        self.csv('input.csv', [('A',)])
        outside = self.csv('outside.csv', [('A',)], root=self.root)
        self.outputs()
        (self.artifacts / 'used.csv').unlink()
        try:
            os.symlink(str(outside), str(self.artifacts / 'used.csv'))
        except (OSError, NotImplementedError) as exc:
            self.skipTest('Symbolic links unavailable: ' + str(exc))
        self.assertEqual(self.reconcile()['status'], 'BLOCKED')

    def test_empty_configuration_is_explicitly_not_applicable(self):
        self.assertEqual(data.inspect_joins(self.problem, [])['status'], 'NOT_APPLICABLE')
        self.assertEqual(data.reconcile(self.problem, self.artifacts, [])['status'], 'NOT_APPLICABLE')

    def test_strict_schemas_are_self_contained_without_min_length(self):
        def inspect(schema):
            if isinstance(schema, dict):
                self.assertNotIn('minLength', schema)
                self.assertNotIn('$ref', schema)
                if schema.get('type') == 'object':
                    self.assertFalse(schema['additionalProperties'])
                    self.assertEqual(set(schema['required']), set(schema['properties']))
                for child in schema.values():
                    inspect(child)
            elif isinstance(schema, list):
                for child in schema:
                    inspect(child)
        inspect(data.JOIN_SCHEMA)
        inspect(data.RECONCILIATION_SCHEMA)


if __name__ == '__main__':
    unittest.main()
