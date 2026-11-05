"""Generated results cannot become upstream problem evidence."""
import unittest

import test_boundary


class SourceInputTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_boundary.BoundaryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.boundary = test_boundary.boundary

    def test_generated_documents_cannot_replace_the_original_problem(self):
        for name in self.boundary.GENERATED:
            with self.subTest(name=name):
                self.fixture.write(name, 'Generated content')
                with self.assertRaises(ValueError):
                    self.boundary.create_state(self.root, name)

    def test_reanalysis_cannot_cite_its_previous_documents_as_new_evidence(self):
        state = self.boundary.create_state(self.root, 'SPEC.md')
        for name in ('CONFIRMED_REQUIREMENTS.md', 'FUNCTIONAL_SPEC.md'):
            with self.subTest(name=name):
                self.fixture.write(name, 'Previously generated content')
                report = dict(test_boundary.EMPTY, references=[name])
                with self.assertRaises(ValueError):
                    self.boundary.validate_report(self.root, report, state)


if __name__ == '__main__':
    unittest.main()
