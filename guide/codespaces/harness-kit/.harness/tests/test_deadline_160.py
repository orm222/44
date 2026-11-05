"""New 160-minute clocks never extend or rewrite legacy 150-minute evidence."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_problem_time import clock
import test_requirements as document_fixtures


class DeadlineCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='deadline-160-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1735689600.0
        self.config_path = self.root / clock.CONFIG

    def config(self, **values):
        self.config_path.write_text(json.dumps(values), encoding='utf-8')

    def test_new_config_without_limit_persists_160_before_receipt(self):
        self.config(spec='SPEC.md')
        original_anchor = clock._anchor
        def interrupt(*arguments):
            original_anchor(*arguments)
            raise KeyboardInterrupt()
        with mock.patch.object(clock, '_anchor', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            clock.begin(self.root, now=self.now)
        config = json.loads(self.config_path.read_bytes())
        self.assertEqual(config['problem_received_at'], self.now)
        self.assertEqual(config['problem_time_limit_seconds'], 9600)
        receipt = clock.begin(self.root, now=self.now + 200)
        self.assertEqual(receipt['deadline_at'], self.now + 9600)

    def test_completed_legacy_receipt_is_read_and_restarted_without_rewrite(self):
        self.config(problem_time_limit_seconds=9000)
        receipt = clock.begin(self.root, now=self.now)
        path = self.root / clock.RECEIPT
        original = path.read_bytes(), path.stat().st_mtime_ns, self.config_path.read_bytes()
        self.assertEqual(clock.begin(self.root, now=self.now + 9200), receipt)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, self.config_path.read_bytes()), original)
        self.assertEqual(receipt['deadline_at'], self.now + 9000)
        with self.assertRaisesRegex(clock.DeadlineError, '150 minutes'):
            clock.budget(self.root, 1, now=self.now + 9000)

    def test_changed_config_cannot_extend_or_shorten_saved_receipt(self):
        for original, changed in ((9000, 9600), (9600, 9000)):
            with self.subTest(original=original):
                root = self.root / str(original)
                root.mkdir()
                config = root / clock.CONFIG
                config.write_text(json.dumps({'problem_time_limit_seconds': original}), encoding='utf-8')
                clock.begin(root, now=self.now)
                path = root / clock.RECEIPT
                before = path.read_bytes()
                config.write_text(json.dumps({'problem_time_limit_seconds': changed,
                                               'problem_received_at': self.now}), encoding='utf-8')
                with self.assertRaisesRegex(clock.DeadlineError, 'original config limit'):
                    clock.begin(root, now=self.now + 10)
                self.assertEqual(path.read_bytes(), before)

    def test_legacy_anchor_without_limit_keeps_150_and_original_timestamp(self):
        self.config(spec='SPEC.md', problem_received_at=self.now)
        before = self.config_path.read_bytes()
        receipt = clock.begin(self.root, now=self.now + 100)
        self.assertEqual(receipt['received_at'], self.now)
        self.assertEqual(receipt['limit_seconds'], 9000)
        self.assertEqual(receipt['deadline_at'], self.now + 9000)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_legacy_pending_anchor_without_limit_does_not_gain_ten_minutes(self):
        self.config(spec='SPEC.md')
        pending = {'spec': 'SPEC.md', 'problem_received_at': self.now}
        temporary = self.config_path.with_name(self.config_path.name + '.problem-time.tmp')
        raw = (json.dumps(pending, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        temporary.write_bytes(raw)
        receipt = clock.begin(self.root, now=self.now + 9200)
        self.assertEqual(receipt['limit_seconds'], 9000)
        self.assertEqual(receipt['deadline_at'], self.now + 9000)
        self.assertEqual(self.config_path.read_bytes(), raw)
        with self.assertRaisesRegex(clock.DeadlineError, 'expired'):
            clock.budget(self.root, 1, now=self.now + 9200)

    def test_new_pending_anchor_keeps_160_without_resetting_start(self):
        self.config(spec='SPEC.md')
        with mock.patch.object(clock.os, 'replace', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            clock.begin(self.root, now=self.now)
        temporary = self.config_path.with_name(self.config_path.name + '.problem-time.tmp')
        raw = temporary.read_bytes()
        self.assertEqual(json.loads(raw)['problem_time_limit_seconds'], 9600)
        receipt = clock.begin(self.root, now=self.now + 9200)
        self.assertEqual(receipt['received_at'], self.now)
        self.assertEqual(receipt['deadline_at'], self.now + 9600)
        self.assertEqual(clock.budget(self.root, 600, now=self.now + 9200), 400)
        self.assertEqual(self.config_path.read_bytes(), raw)

    def test_unconfigured_legacy_receipt_remains_150(self):
        value = {'version': 1, 'project_root': str(self.root), 'limit_seconds': 9000,
                 'received_at': self.now, 'deadline_at': self.now + 9000}
        value['sha256'] = clock._checksum(value)
        path = self.root / clock.RECEIPT
        path.parent.mkdir()
        path.write_text(json.dumps(value), encoding='utf-8')
        before = path.read_bytes()
        self.assertEqual(clock.begin(self.root, now=self.now + 200), value)
        self.assertEqual(path.read_bytes(), before)


class DocumentGateDeadlineTests(document_fixtures.RequirementsFixture, unittest.TestCase):
    def prepare(self, limit, estimate):
        config = dict(self.fixture.config, problem_time_limit_seconds=limit,
                      document_stage_minutes={'requirements': estimate, 'functional': 10})
        self.fixture.write('config.json', json.dumps(config))
        self.requirements.problem_time.begin(self.root)
        self.ready_boundary()

    def test_new_document_gate_uses_160_and_accepts_estimate_above_150(self):
        self.prepare(9600, 155)
        result = self.pending()
        self.assertIn('제한 160분', result.stdout)
        self.assertIn('계획 155분', result.stdout)
        self.assertNotIn('제한 150분', result.stdout)
        self.assertEqual(self.kinds(), ['requirements'])

    def test_existing_document_gate_keeps_150(self):
        self.prepare(9000, 15)
        before = (self.root / clock.RECEIPT).read_bytes()
        result = self.pending()
        self.assertIn('제한 150분', result.stdout)
        self.assertNotIn('제한 160분', result.stdout)
        self.assertEqual((self.root / clock.RECEIPT).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
