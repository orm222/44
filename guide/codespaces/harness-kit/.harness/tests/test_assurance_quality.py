"""Constraint-first benchmarks and side-effect-free environment observations."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


loader = importlib.util.spec_from_file_location('tested_assurance_quality', Path(__file__).resolve().parents[1] / 'assurance_quality.py')
quality = importlib.util.module_from_spec(loader)
loader.loader.exec_module(quality)


class AlgorithmQualityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / 'input.csv').write_bytes(b'id,load\n1,8\n')
        self.hash = hashlib.sha256((self.root / 'input.csv').read_bytes()).hexdigest()
        self.path = self.root / 'measurements.json'
        self.contract = dict(id='route-cost', direction='minimize', baseline='base', selected='new',
                             candidates=['base', 'new'], cases=[dict(id='small', input='input.csv', exact=8, max_relative_gap=0)],
                             constraints=[dict(name='load', min=0, max=10)], max_regression=0)
        self.rows = [dict(case_id='small', candidate_id='base', input_sha256=self.hash, objective=10, metrics={'load': 8}),
                     dict(case_id='small', candidate_id='new', input_sha256=self.hash, objective=8, metrics={'load': 8})]

    def compare(self):
        self.path.write_text(json.dumps({'results': self.rows}), encoding='utf-8')
        return quality.compare_algorithms(self.root, self.path, self.contract)

    def test_exact_small_fixture_and_actual_hashes_are_reported(self):
        report = self.compare()
        self.assertTrue(report['passed'])
        self.assertEqual(report['cases'][0]['exact_status'], 'matches_exact_on_case')
        self.assertEqual(report['cases'][0]['absolute_regression'], -2)
        self.assertEqual(report['evidence']['input_sha256']['small'], self.hash)
        self.assertEqual(report['measurement_count'], 2)

    def test_constraint_violation_cannot_be_hidden_by_better_objective(self):
        self.rows[1].update(objective=1, metrics={'load': 11})
        report = self.compare()
        self.assertFalse(report['passed'])
        self.assertNotIn('new', report['cases'][0]['feasible_ranking'])
        self.assertIn('selected candidate violates', report['cases'][0]['reasons'][0])

    def test_infeasible_alternative_is_excluded_without_hiding_feasible_results(self):
        self.contract['candidates'].append('cheater')
        self.rows.append(dict(case_id='small', candidate_id='cheater', input_sha256=self.hash, objective=1, metrics={'load': 99}))
        report = self.compare()
        self.assertTrue(report['passed'])
        self.assertEqual(report['cases'][0]['feasible_ranking'], ['new', 'base'])

    def test_infeasible_baseline_is_not_a_valid_regression_claim(self):
        self.rows[0]['metrics']['load'] = 99
        report = self.compare()
        self.assertFalse(report['passed'])
        self.assertIsNone(report['cases'][0]['absolute_regression'])

    def test_absolute_regression_budget_uses_declared_units(self):
        self.contract['cases'][0].update(exact=None, max_relative_gap=None)
        self.contract['max_regression'] = 1
        self.rows[1]['objective'] = 12
        self.assertFalse(self.compare()['passed'])
        self.rows[1]['objective'] = 11
        self.assertTrue(self.compare()['passed'])

    def test_decimal_threshold_does_not_add_an_unapproved_tolerance(self):
        self.contract['cases'][0].update(exact=None, max_relative_gap=None)
        self.contract['max_regression'] = 0.1
        self.rows[0]['objective'], self.rows[1]['objective'] = 0.2, 0.3
        self.assertTrue(self.compare()['passed'])
        self.rows[1]['objective'] = 0.3000000001
        self.assertFalse(self.compare()['passed'])

    def test_relative_gap_and_direction_bound_are_separate_checks(self):
        self.rows[1]['objective'] = 9
        self.contract['cases'][0]['max_relative_gap'] = 0.125
        self.assertTrue(self.compare()['passed'])
        self.contract['cases'][0]['max_relative_gap'] = 0.124
        self.assertFalse(self.compare()['passed'])
        self.rows[1]['objective'] = 7
        self.assertFalse(self.compare()['passed'])

    def test_maximize_negative_objectives_use_absolute_exact_denominator(self):
        self.contract['direction'] = 'maximize'
        self.contract['cases'][0].update(exact=-5, max_relative_gap=0.2)
        self.rows[0]['objective'], self.rows[1]['objective'] = -9, -6
        report = self.compare()
        self.assertTrue(report['passed'])
        self.assertEqual(report['cases'][0]['relative_gap'], 0.2)
        self.rows[1]['objective'] = -4
        self.assertFalse(self.compare()['passed'])

    def test_exact_zero_is_handled_without_invented_denominator(self):
        self.contract['cases'][0].update(exact=0, max_relative_gap=0.1)
        self.rows[0]['objective'], self.rows[1]['objective'] = 2, 1
        report = self.compare()
        self.assertFalse(report['passed'])
        self.assertIsNone(report['cases'][0]['relative_gap'])
        self.rows[1]['objective'] = 0
        self.assertTrue(self.compare()['passed'])

    def test_unknown_exact_remains_unmeasured(self):
        self.contract['cases'][0].update(exact=None, max_relative_gap=None)
        report = self.compare()
        self.assertTrue(report['passed'])
        self.assertEqual(report['cases'][0]['exact_status'], 'not_measured')
        self.assertIsNone(report['cases'][0]['relative_gap'])
        self.assertIn('no general optimality claim', report['optimality_claim'])

    def test_duplicate_missing_and_unknown_rows_are_rejected(self):
        original = copy.deepcopy(self.rows)
        for changed in (original + [original[0]], original[:1], [dict(original[0], candidate_id='other'), original[1]]):
            with self.subTest(rows=len(changed)):
                self.rows = changed
                with self.assertRaises(ValueError):
                    self.compare()

    def test_every_case_candidate_pair_is_required(self):
        self.contract['cases'].append(dict(id='second', input='input.csv', exact=None, max_relative_gap=None))
        with self.assertRaisesRegex(ValueError, 'Missing'):
            self.compare()

    def test_measurement_input_hash_is_checked_against_real_bytes(self):
        (self.root / 'input.csv').write_bytes(b'id,load\n1,9\n')
        with self.assertRaisesRegex(ValueError, 'hash differs'):
            self.compare()

    def test_nonfinite_boolean_metrics_and_trusted_pass_flags_are_rejected(self):
        for value in (float('nan'), float('inf'), True):
            with self.subTest(value=str(value)):
                self.rows[1]['objective'] = value
                with self.assertRaises(ValueError):
                    self.compare()
        self.rows[1]['objective'] = 8
        self.rows[1]['metrics']['load'] = True
        with self.assertRaises(ValueError):
            self.compare()
        self.rows[1]['metrics']['load'] = 8
        self.rows[1]['passed'] = True
        with self.assertRaisesRegex(ValueError, 'unexpected fields'):
            self.compare()

    def test_missing_extra_constraint_metrics_and_duplicate_json_keys_are_rejected(self):
        for metrics in ({}, {'load': 8, 'unexpected': 1}):
            self.rows[1]['metrics'] = metrics
            with self.assertRaises(ValueError):
                self.compare()
        self.path.write_text('{"results": [], "results": []}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            quality.compare_algorithms(self.root, self.path, self.contract)

    def test_contract_limits_candidates_and_requires_approved_thresholds(self):
        invalids = [dict(self.contract, candidates=['a', 'b', 'c', 'd']), dict(self.contract, max_regression=float('inf')),
                    dict(self.contract, max_regression=-1), dict(self.contract, max_regression=True)]
        for contract in invalids:
            with self.subTest(contract=str(contract)):
                with self.assertRaises(ValueError):
                    quality.validate_algorithm(contract)
        self.contract['cases'][0].update(exact=None, max_relative_gap=0.1)
        with self.assertRaises(ValueError):
            quality.validate_algorithm(self.contract)

    def test_fixture_path_cannot_escape_problem_root(self):
        self.contract['cases'][0]['input'] = '../elsewhere.csv'
        with self.assertRaises(ValueError):
            self.compare()


class EnvironmentQualityTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(quality.importlib.metadata, 'distributions', return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_discovery_does_not_force_missing_stack_or_probe_ports(self):
        with mock.patch.object(quality.shutil, 'which', return_value=None), \
                mock.patch.object(quality.socket, 'socket', side_effect=AssertionError('Unrequested port scan')):
            result = quality.inspect_environment()
        self.assertEqual(result['status'], 'DISCOVERED')
        self.assertIsNone(result['passed'])
        self.assertEqual(result['missing_required'], [])

    def test_missing_requested_tool_or_package_is_blocked(self):
        with mock.patch.object(quality.shutil, 'which', return_value=None), \
                mock.patch.object(quality.importlib.metadata, 'version', side_effect=quality.importlib.metadata.PackageNotFoundError('absent')):
            result = quality.inspect_environment({'executables': ['missing-tool'], 'python_packages': ['absent'], 'ports': []})
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['missing_required'], ['executable:missing-tool', 'python_package:absent'])

    def test_running_python_is_found_with_empty_parent_path(self):
        executable = Path(quality.sys.executable).name
        with mock.patch.dict(os.environ, {'PATH': ''}):
            self.assertIsNone(quality.shutil.which(executable))
            result = quality.inspect_environment({'executables': [executable], 'python_packages': [], 'ports': []})
            self.assertEqual(os.environ['PATH'], '')
        self.assertTrue(result['passed'], result['missing_required'])
        self.assertEqual(Path(result['executables'][0]['path']).resolve(), Path(quality.sys.executable).resolve())

    def test_python_fallback_does_not_hide_a_missing_required_tool(self):
        executable = Path(quality.sys.executable).name
        missing = 'harness_missing_tool_9f8432'
        with mock.patch.dict(os.environ, {'PATH': ''}):
            result = quality.inspect_environment({'executables': [executable, missing], 'python_packages': [], 'ports': []})
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['missing_required'], ['executable:' + missing])

    def test_only_explicit_loopback_ports_are_tested_without_remote_connection(self):
        listener = mock.MagicMock()
        with mock.patch.object(quality.socket, 'socket', return_value=listener) as factory, \
                mock.patch.object(quality.socket, 'create_connection', side_effect=AssertionError('Remote connection forbidden')):
            result = quality.inspect_environment({'executables': [], 'python_packages': [], 'ports': [8123]})
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        listener.__enter__.return_value.bind.assert_called_once_with(('127.0.0.1', 8123))
        self.assertTrue(result['passed'])
        self.assertTrue(result['ports'][0]['available'])

    def test_unavailable_requested_port_is_not_passed(self):
        listener = mock.MagicMock()
        listener.__enter__.return_value.bind.side_effect = OSError('busy')
        with mock.patch.object(quality.socket, 'socket', return_value=listener):
            result = quality.inspect_environment({'executables': [], 'python_packages': [], 'ports': [8123]})
        self.assertFalse(result['passed'])
        self.assertEqual(result['missing_required'], ['port:8123'])

    def test_inventory_is_complete_in_report_and_bounded_in_planner_summary(self):
        inventory = [SimpleNamespace(metadata={'Name': 'package-' + str(i)}, version='1.0') for i in range(30)]
        with mock.patch.object(quality.importlib.metadata, 'distributions', return_value=inventory), \
                mock.patch.object(quality.shutil, 'which', return_value=None):
            result = quality.inspect_environment()
        self.assertEqual(len(result['installed_packages']), 30)
        self.assertEqual(len(result['summary']['package_sample']), 20)
        self.assertEqual(result['summary']['additional_package_count'], 10)

    def test_requested_packages_are_metadata_only_and_no_command_is_executed(self):
        with mock.patch.object(quality.shutil, 'which', return_value='C:/tools/tool.exe'), \
                mock.patch.object(quality.importlib.metadata, 'version', return_value='1.2.3'), \
                mock.patch('subprocess.run', side_effect=AssertionError('Subprocess forbidden')):
            result = quality.inspect_environment({'executables': ['tool'], 'python_packages': ['chosen-package'], 'ports': []})
        self.assertTrue(result['passed'])
        self.assertEqual(result['python_packages'][0]['version'], '1.2.3')

    def test_invalid_request_rejects_boolean_ports_and_unknown_fields(self):
        for request in ({'executables': [], 'python_packages': [], 'ports': [True]},
                        {'executables': [], 'python_packages': [], 'ports': [], 'install': True}):
            with self.assertRaises(ValueError):
                quality.inspect_environment(request)


if __name__ == '__main__':
    unittest.main()
