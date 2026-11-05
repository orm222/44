"""Small CSV intake checks; no model API or operating-system-specific tests."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('test_csv_intake', SOURCE / 'data_checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class DataCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='csv-intake-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def config(self, files=None, baseline=None):
        value = {'data_files': files or []}
        if baseline is not None:
            value['data_baseline'] = baseline
        self.write('config.json', json.dumps(value))

    def report(self, text, baseline=None):
        self.write('data.csv', text)
        if baseline:
            self.write('baseline.csv', baseline)
        self.config(['data.csv'], 'baseline.csv' if baseline else None)
        return checks.inspect(self.root)

    def kinds(self, report):
        return {finding['kind'] for finding in report['findings']}

    def test_no_data_is_optional_and_does_not_create_files(self):
        before = list(self.root.iterdir())
        self.assertEqual(checks.configured_files(self.root), [])
        self.assertIsNone(checks.inspect(self.root))
        self.assertEqual(list(self.root.iterdir()), before)
        self.write('base.csv', 'value\n1\n')
        self.config(baseline='base.csv')
        with self.assertRaisesRegex(ValueError, 'requires data_files'):
            checks.inspect(self.root)

    def test_iqr_is_explicitly_exploratory_and_deterministic_with_exact_hashes(self):
        report = self.report('value\n1\n2\n3\n4\n100\n')
        candidate = next(item for item in report['findings'] if item['kind'] == 'iqr_candidate')
        self.assertEqual(candidate['examples'], [{'record': 6, 'value': 100.0}])
        self.assertEqual((candidate['lower'], candidate['upper']), (-1.0, 7.0))
        self.assertIn('not a domain threshold', candidate['description'])
        self.assertEqual(report, checks.inspect(self.root))
        self.assertEqual(report['input_hashes'], {'data.csv': hashlib.sha256((self.root / 'data.csv').read_bytes()).hexdigest()})
        json.dumps(report, allow_nan=False)

    def test_missing_nonfinite_and_mixed_types_are_reported_without_repair(self):
        text = 'value,label\n,null\nnone,a\nNaN,b\ninf,c\n-Inf,d\n1,e\nword,f\n'
        report = self.report(text)
        self.assertTrue({'missing_value', 'nonfinite_value', 'mixed_numeric_text'} <= self.kinds(report))
        self.assertEqual([item['kind'] for item in report['findings'][:2]],
                         ['nonfinite_value', 'mixed_numeric_text'])
        self.assertEqual((self.root / 'data.csv').read_text(encoding='utf-8'), text)
        self.assertTrue(all(len(item['examples']) <= 3 for item in report['findings']))
        json.dumps(report, allow_nan=False)

    def test_invalid_csv_is_rejected_instead_of_partially_analyzed(self):
        for text in ('a,a\n1,2\n', 'a,b\n1\n', 'a\n1,2\n', 'a\n', 'a\n"unterminated\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.report(text)
        path = self.write('bad.csv', '')
        path.write_bytes(b'\xff\xfe')
        self.config(['bad.csv'])
        with self.assertRaises(ValueError):
            checks.inspect(self.root)

    def test_runtime_traversal_duplicate_and_self_baseline_paths_are_rejected(self):
        self.write('data.csv', 'value\n1\n')
        self.write('.harness/runtime.csv', 'value\n1\n')
        for paths in (['../outside.csv'], ['.harness/runtime.csv'], ['data.csv', './data.csv'], [str(self.root / 'data.csv')]):
            self.config(paths)
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                checks.configured_files(self.root)
        self.config(['data.csv'], 'data.csv')
        with self.assertRaises(ValueError):
            checks.configured_files(self.root)

    def test_row_byte_and_file_limits_are_explicit_errors(self):
        self.write('data.csv', 'value\n1\n2\n3\n')
        self.config(['data.csv'])
        with mock.patch.object(checks, 'MAX_ROWS', 2), self.assertRaisesRegex(ValueError, 'row intake limit'):
            checks.inspect(self.root)
        with mock.patch.object(checks, 'MAX_BYTES', 5), self.assertRaisesRegex(ValueError, 'intake limit'):
            checks.inspect(self.root)
        with mock.patch.object(checks, 'MAX_FILES', 0), self.assertRaisesRegex(ValueError, 'at most eight'):
            checks.inspect(self.root)

    def test_change_after_first_read_is_not_published_as_a_consistent_snapshot(self):
        self.write('data.csv', 'value\n1\n2\n3\n4\n')
        self.config(['data.csv'])
        read = checks._read
        seen = []
        def change_after_read(path):
            raw = read(path)
            if path.name == 'data.csv' and not seen:
                seen.append(True)
                path.write_text('value\n9\n8\n7\n6\n', encoding='utf-8')
            return raw
        with mock.patch.object(checks, '_read', side_effect=change_after_read), self.assertRaisesRegex(ValueError, 'changed during inspection'):
            checks.inspect(self.root)

    def test_group_timestamps_allow_interleaving_but_reject_duplicate_and_reversed_order(self):
        report = self.report('timestamp,value,group,regime\n1,10,A,R\n1,10,B,R\n2,11,A,R\n1,12,A,R\n')
        issue = next(item for item in report['findings'] if item['kind'] == 'timeseries_input_issue')
        self.assertEqual(issue['count'], 1)
        self.assertIn('duplicate or reversed', issue['examples'][0]['reason'])

    def test_explicit_baseline_limits_are_fixed_and_future_data_does_not_refit(self):
        baseline = 'timestamp,value,group,regime\n-4,10,A,R\n-3,11,A,R\n-2,10,A,R\n-1,11,A,R\n'
        report = self.report('timestamp,value,group,regime\n0,15,A,R\n', baseline)
        candidate = next(item for item in report['findings'] if item['kind'] == 'i_limit_candidate')
        self.assertAlmostEqual(candidate['examples'][0]['upper'], 10.5 + 3 / 1.128)
        self.assertIn('mr_limit_candidate', self.kinds(report))
        self.assertEqual(set(report['input_hashes']), {'data.csv', 'baseline.csv'})
        self.write('data.csv', 'timestamp,value,group,regime\n0,15,A,R\n1,100,A,R\n')
        again = checks.inspect(self.root)
        updated = next(item for item in again['findings'] if item['kind'] == 'i_limit_candidate')
        self.assertEqual(candidate['examples'][0], updated['examples'][0])

    def test_unusable_baselines_never_become_normal_results(self):
        for baseline in ('timestamp,value\n-3,10\n-2,10\n-1,10\n',
                         'timestamp,value\n-2,10\n-1,11\n',
                         'timestamp,value\n-5,10\n-3,11\n-2,10\n'):
            with self.subTest(baseline=baseline):
                report = self.report('timestamp,value\n0,100\n', baseline)
                self.assertIn('baseline_unavailable', self.kinds(report))
                self.assertIn('missing_baseline', self.kinds(report))
                self.assertNotIn('i_limit_candidate', self.kinds(report))
                self.assertEqual(report['status'], 'review_required')

    def test_overlap_new_group_and_gap_are_not_silently_compared(self):
        baseline = 'timestamp,value,group,regime\n-4,10,A,R\n-3,11,A,R\n-2,10,A,R\n-1,11,A,R\n'
        report = self.report('timestamp,value,group,regime\n-1,100,A,R\n0,100,B,R\n2,100,A,R\n', baseline)
        self.assertTrue({'baseline_overlap', 'missing_baseline', 'sampling_gap'} <= self.kinds(report))
        candidate = next(item for item in report['findings'] if item['kind'] == 'i_limit_candidate')
        self.assertEqual([item['record'] for item in candidate['examples']], [4])
        self.assertNotIn('mr_limit_candidate', self.kinds(report))

    def test_summary_truncation_is_explicit_and_values_remain_valid_json(self):
        columns = ['x' + str(i) for i in range(8)]
        report = self.report(','.join(columns) + '\n' + ','.join(['NaN'] * 8) + '\n')
        self.assertEqual(len(report['findings']), 8)
        with mock.patch.object(checks, 'MAX_FINDINGS', 3):
            smaller = checks.inspect(self.root)
        self.assertEqual(len(smaller['findings']), 3)
        self.assertEqual(smaller['findings_omitted_count'], 5)
        self.assertEqual(smaller['findings'], report['findings'][:3])
        json.dumps(smaller, allow_nan=False)

    def test_baseline_candidates_survive_many_auxiliary_column_findings(self):
        header = ['timestamp', 'value'] + ['aux' + str(i) for i in range(12)]
        auxiliary = ['1', '1', '1', '1', '100', '']
        def csv_text(start, values):
            rows = [','.join(header)]
            for offset, value in enumerate(values):
                rows.append(','.join([str(start + offset), str(value)] + [auxiliary[offset]] * 12))
            return '\n'.join(rows) + '\n'
        report = self.report(csv_text(6, [30, 11, 10, 11, 10, 11]),
                             csv_text(0, [10, 11, 10, 11, 10, 11]))
        self.assertEqual(len(report['findings']), 40)
        self.assertGreater(report['findings_omitted_count'], 0)
        self.assertEqual([item['kind'] for item in report['findings'][:2]],
                         ['i_limit_candidate', 'mr_limit_candidate'])
        candidate = report['findings'][0]['examples'][0]
        self.assertEqual(candidate['value'], 30)
        self.assertAlmostEqual(candidate['upper'], 10.5 + 3 / 1.128)
        with mock.patch.object(checks, 'MAX_FINDINGS', 1000):
            full = checks.inspect(self.root)
        self.assertEqual(report['findings'], full['findings'][:40])
        self.assertEqual(report['findings_omitted_count'], len(full['findings']) - 40)

    def test_extreme_moving_range_is_reported_without_nonfinite_json(self):
        baseline = 'timestamp,value\n-3,-1e307\n-2,0\n-1,-1e307\n'
        report = self.report('timestamp,value\n0,1e308\n1,-1e308\n', baseline)
        self.assertIn('calculation_unavailable', self.kinds(report))
        json.dumps(report, allow_nan=False)

    def test_absolute_and_relative_time_axes_require_confirmation(self):
        baseline = 'timestamp,value\n-3,10\n-2,11\n-1,10\n'
        report = self.report('timestamp,value\n2026-01-01T00:00:00Z,100\n', baseline)
        self.assertIn('comparison_unavailable', self.kinds(report))
        self.assertNotIn('i_limit_candidate', self.kinds(report))

    def test_naive_iso_baseline_and_monitoring_use_the_same_wall_clock_axis(self):
        baseline = ('timestamp,value\n2026-11-05 10:00:00,10\n2026-11-05 10:01:00,11\n'
                    '2026-11-05 10:02:00,10\n2026-11-05 10:03:00,11\n')
        report = self.report('timestamp,value\n2026-11-05 10:04:00,30\n', baseline)
        self.assertEqual(self.kinds(report), {'i_limit_candidate', 'mr_limit_candidate'})
        self.assertEqual(report['findings'][1]['examples'][0]['moving_range'], 19)
        self.assertEqual(checks._timestamp('1970-01-01 00:00:00'), (0.0, 'naive_iso'))

    def test_naive_offset_and_numeric_axes_cannot_be_mixed(self):
        naive = ('timestamp,value\n2026-11-05 10:00:00,10\n2026-11-05 10:01:00,11\n'
                 '2026-11-05 10:02:00,10\n')
        aware = naive.replace(':00,', ':00+09:00,')
        numeric = 'timestamp,value\n0,10\n60,11\n120,10\n'
        for baseline, timestamp in ((naive, '2026-11-05 10:03:00+09:00'),
                                    (naive, '2026-11-05 10:03:00Z'),
                                    (aware, '2026-11-05 10:03:00'),
                                    (numeric, '2026-11-05 10:03:00'),
                                    (naive, '180')):
            with self.subTest(timestamp=timestamp, baseline=baseline):
                report = self.report('timestamp,value\n' + timestamp + ',30\n', baseline)
                self.assertIn('comparison_unavailable', self.kinds(report))
                self.assertNotIn('i_limit_candidate', self.kinds(report))
                self.assertNotIn('mr_limit_candidate', self.kinds(report))
        mixed = 'timestamp,value\n2026-11-05 10:03:00,30\n2026-11-05 10:04:00Z,31\n'
        report = self.report(mixed, naive)
        self.assertIn('timeseries_input_issue', self.kinds(report))
        self.assertIn('comparison_unavailable', self.kinds(report))

    def test_naive_time_axis_retains_overlap_and_order_guards(self):
        baseline = ('timestamp,value\n2026-11-05 10:00:00,10\n2026-11-05 10:01:00,11\n'
                    '2026-11-05 10:02:00,10\n')
        report = self.report('timestamp,value\n2026-11-05 10:02:00,30\n', baseline)
        self.assertIn('baseline_overlap', self.kinds(report))
        self.assertNotIn('i_limit_candidate', self.kinds(report))
        report = self.report('timestamp,value\n2026-11-05 10:04:00,30\n2026-11-05 10:03:00,31\n', baseline)
        self.assertIn('timeseries_input_issue', self.kinds(report))
        self.assertNotIn('i_limit_candidate', self.kinds(report))

    def test_finite_timestamps_with_overflowing_difference_do_not_form_limits(self):
        baseline = 'timestamp,value\n-1e308,10\n1e308,11\n1.1e308,10\n'
        report = self.report('timestamp,value\n1.2e308,100\n', baseline)
        self.assertIn('baseline_unavailable', self.kinds(report))
        self.assertNotIn('i_limit_candidate', self.kinds(report))
        json.dumps(report, allow_nan=False)

    def test_ignored_overlap_does_not_replace_the_previous_baseline_value(self):
        baseline = 'timestamp,value\n-4,10\n-3,11\n-2,10\n-1,11\n'
        report = self.report('timestamp,value\n-1,1000\n0,11\n1,12\n', baseline)
        self.assertIn('baseline_overlap', self.kinds(report))
        self.assertNotIn('mr_limit_candidate', self.kinds(report))
        self.assertNotIn('i_limit_candidate', self.kinds(report))


if __name__ == '__main__':
    unittest.main()
