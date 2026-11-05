"""New plan policy rejects duplicate work without rewriting approved contracts."""
import copy
import importlib.util
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('plan_refinements_' + name, SOURCE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contracts = load('development_contracts')
checks = load('development_checks')


def example_plan():
    check = dict(id='add-example', argv=['python', '-m', 'unittest', 'tests.test_add'], cwd='.',
                 timeout_seconds=30, test_framework='unittest', min_tests=1,
                 requirement='R-1', oracle='The problem gives the independent example 2 + 3 = 5.')
    return dict(summary='Implement addition',
        architecture=dict(framework='standard library', ports=[], storage='none', algorithm='addition'),
        decisions=[dict(decision='Use the standard library', reason='One function needs no framework', discarded=[])],
        tasks=[dict(id='ADD', title='Add numbers', requirements=['R-1'], depends_on=[],
                    files=['calc.py'], test_files=['tests/test_add.py'], checks=[check], expected_minutes=5)],
        docker=dict(dockerfile='Dockerfile', context='.', command=[], checks=[copy.deepcopy(check)],
                    container_port=0, host_port=0, compose_file=None, service=None),
        docker_files=['Dockerfile', '.dockerignore'], run_commands=[['python', 'calc.py']], limitations=[],
        stage_minutes=dict(plan=5, implementation=10, docker=5), assurance=contracts.assurance.defaults())


def add_second_task(plan):
    task = copy.deepcopy(plan['tasks'][0])
    task.update(id='FORMAT', title='Format the result', depends_on=['ADD'],
                files=['format_result.py'], test_files=['tests/test_format.py'])
    task['checks'][0].update(id='format-example', requirement='R-2', oracle='The output text is specified separately.')
    plan['tasks'].append(task)
    return task


class PlanRefinementsTests(unittest.TestCase):
    def test_legacy_call_keeps_duplicate_approved_contracts_unchanged(self):
        plan = example_plan()
        add_second_task(plan)
        plan['decisions'][0]['reason'] = ' '
        original = copy.deepcopy(plan)
        self.assertIs(contracts.validate_plan(plan, checks), plan)
        self.assertEqual(plan, original)

    def test_new_plan_rejects_same_execution_in_different_tasks_without_dropping_evidence(self):
        plan = example_plan()
        second = add_second_task(plan)
        second['checks'][0].update(timeout_seconds=60, min_tests=2)
        original = copy.deepcopy(plan)
        with self.assertRaises(ValueError) as raised:
            contracts.validate_plan(plan, checks, enforce_new_policy=True)
        message = str(raised.exception)
        for text in ('ADD/add-example', 'FORMAT/format-example', 'task-owned', 'requirement', 'oracle', 'min_tests'):
            self.assertIn(text, message)
        self.assertEqual(plan, original)

    def test_new_plan_rejects_duplicate_execution_inside_one_task(self):
        plan = example_plan()
        duplicate = copy.deepcopy(plan['tasks'][0]['checks'][0])
        duplicate.update(id='second-contract', requirement='R-2', oracle='Another independent expectation')
        plan['tasks'][0]['checks'].append(duplicate)
        with self.assertRaisesRegex(ValueError, 'ADD/second-contract'):
            contracts.validate_plan(plan, checks, enforce_new_policy=True)

    def test_equivalent_working_directory_notation_does_not_hide_duplicate_execution(self):
        plan = example_plan()
        add_second_task(plan)['checks'][0]['cwd'] = './'
        with self.assertRaisesRegex(ValueError, 'task-owned'):
            contracts.validate_plan(plan, checks, enforce_new_policy=True)

    def test_different_execution_targets_or_working_directories_remain_valid(self):
        for difference in ('argv', 'cwd', 'test_framework'):
            plan = example_plan()
            second = add_second_task(plan)['checks'][0]
            if difference == 'argv':
                second['argv'][-1] = 'tests.test_format'
            elif difference == 'cwd':
                second['cwd'] = 'other_component'
            else:
                second['test_framework'] = 'command'
            with self.subTest(difference=difference):
                self.assertIs(contracts.validate_plan(plan, checks, enforce_new_policy=True), plan)

    def test_new_plan_requires_nonblank_decision_and_reason(self):
        for field in ('decision', 'reason'):
            for blank in ('', ' \t\n'):
                plan = example_plan()
                plan['decisions'][0][field] = blank
                with self.subTest(field=field, blank=blank), self.assertRaisesRegex(ValueError, field):
                    contracts.validate_plan(plan, checks, enforce_new_policy=True)

    def test_empty_discarded_alternatives_and_local_plus_docker_execution_are_allowed(self):
        plan = example_plan()
        original = copy.deepcopy(plan)
        self.assertEqual(plan['decisions'][0]['discarded'], [])
        self.assertIs(contracts.validate_plan(plan, checks, enforce_new_policy=True), plan)
        self.assertEqual(plan, original)

    def test_generic_check_validation_preserves_all_required_contracts(self):
        plan = example_plan()
        add_second_task(plan)
        required = [item for task in plan['tasks'] for item in task['checks']]
        self.assertEqual(checks.validate_checks(required), required)


if __name__ == '__main__':
    unittest.main()
