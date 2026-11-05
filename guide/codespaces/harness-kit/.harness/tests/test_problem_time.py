import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
loader = importlib.util.spec_from_file_location("test_problem_clock", SOURCE / "problem_time.py")
clock = importlib.util.module_from_spec(loader)
loader.loader.exec_module(clock)


class ProblemTimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="problem-clock-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.now = 1735689600.0

    def test_default_begin_is_fixed_and_repeat_does_not_rewrite_or_extend(self):
        initial = clock.begin(self.root, now=self.now)
        path = self.root / clock.RECEIPT
        original = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(initial["deadline_at"], self.now + 9600)
        self.assertEqual(clock.begin(self.root, now=self.now + 12000), initial)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), original)
        with self.assertRaisesRegex(clock.DeadlineError, "cannot be changed"):
            clock.begin(self.root, "2025-01-01T01:00:00+00:00", now=self.now + 4000)

    def test_explicit_timestamp_counts_prior_waiting_and_accepts_equivalent_timezone(self):
        initial = clock.begin(self.root, "2025-01-01T09:00:00+09:00", now=self.now + 100)
        self.assertEqual(initial["received_at"], self.now)
        self.assertEqual(clock.begin(self.root, "2025-01-01T00:00:00Z", now=self.now + 200), initial)
        self.assertEqual(clock.budget(self.root, 9600, now=self.now + 300), 9300)
        self.assertEqual(clock.budget(self.root, 12, now=self.now + 300), 12)

    def test_future_timezone_missing_and_nonfinite_times_rejected_without_receipt(self):
        for timestamp in ("2025-01-01T00:00:01Z", "2025-01-01T00:00:00", "bad", 123):
            with self.subTest(timestamp=timestamp), self.assertRaises(clock.DeadlineError):
                clock.begin(self.root, timestamp, now=self.now)
        for bad in (True, float("inf"), float("nan")):
            with self.subTest(now=bad), self.assertRaises(clock.DeadlineError):
                clock.begin(self.root, now=bad)
        self.assertIsNone(clock.load(self.root))

    def test_missing_legacy_receipt_and_required_or_deleted_receipt(self):
        self.assertEqual(clock.budget(self.root, 100, now=self.now), 100)
        self.assertEqual(clock.status(self.root), {"started": False})
        with self.assertRaisesRegex(clock.DeadlineError, "Missing"):
            clock.budget(self.root, 100, required=True, now=self.now)
        clock.begin(self.root, now=self.now)
        (self.root / clock.RECEIPT).unlink()
        with self.assertRaisesRegex(clock.DeadlineError, "Missing"):
            clock.load(self.root, required=True)

    def test_expiry_includes_wait_and_status_remains_readable(self):
        clock.begin(self.root, now=self.now)
        self.assertAlmostEqual(clock.budget(self.root, 100, now=self.now + 9599.75), 0.25)
        for later in (self.now + 9600, self.now + 99999):
            with self.assertRaisesRegex(clock.DeadlineError, "expired"):
                clock.budget(self.root, 1, now=later)
            status = clock.status(self.root, now=later)
            self.assertTrue(status["expired"])
            self.assertEqual(status["remaining_seconds"], 0)
            self.assertEqual(status["remaining_minutes"], 0)
            self.assertEqual(status["received_at_utc"], "2025-01-01T00:00:00+00:00")
            self.assertEqual(status["deadline_at_utc"], "2025-01-01T02:40:00+00:00")

    def config(self, limit=9000):
        path = self.root / clock.CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"problem_time_limit_seconds": limit, "spec": "SPEC.md"}), encoding="utf-8")
        return path

    def test_config_anchor_prevents_reset_after_receipt_deletion(self):
        config = self.config()
        original = clock.begin(self.root, now=self.now)
        anchored = config.read_bytes()
        self.assertEqual(json.loads(anchored)["problem_received_at"], self.now)
        (self.root / clock.RECEIPT).unlink()
        self.assertIsNone(clock.load(self.root))
        self.assertEqual(clock.begin(self.root, now=self.now + 12000), original)
        self.assertEqual(config.read_bytes(), anchored)
        (self.root / clock.RECEIPT).unlink()
        with self.assertRaisesRegex(clock.DeadlineError, "cannot be changed"):
            clock.begin(self.root, "2025-01-01T01:00:00Z", now=self.now + 12000)

    def test_crash_after_config_publication_keeps_original_start(self):
        config = self.config()
        original_anchor = clock._anchor
        def stop_after_anchor(*args):
            original_anchor(*args)
            raise KeyboardInterrupt()
        with mock.patch.object(clock, "_anchor", side_effect=stop_after_anchor), self.assertRaises(KeyboardInterrupt):
            clock.begin(self.root, now=self.now)
        self.assertFalse((self.root / clock.RECEIPT).exists())
        self.assertEqual(json.loads(config.read_bytes())["problem_received_at"], self.now)
        receipt = clock.begin(self.root, now=self.now + 12000)
        self.assertEqual(receipt["received_at"], self.now)
        with self.assertRaisesRegex(clock.DeadlineError, "expired"):
            clock.budget(self.root, 10, now=self.now + 12000)

    def test_actual_exit_after_complete_temp_write_recovers_same_original_timestamp(self):
        config = self.config()
        before = config.read_bytes()
        script = '''import importlib.util, os, sys
from pathlib import Path
loader = importlib.util.spec_from_file_location("crash_clock", sys.argv[1])
clock = importlib.util.module_from_spec(loader)
loader.loader.exec_module(clock)
clock.os.replace = lambda source, target: os._exit(71)
clock.begin(Path(sys.argv[2]), now=float(sys.argv[3]))
'''
        result = subprocess.run([sys.executable, "-B", "-c", script, str(SOURCE / "problem_time.py"),
                                 str(self.root), str(self.now)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 71, result.stderr)
        temporary = config.with_name(config.name + ".problem-time.tmp")
        pending = temporary.read_bytes()
        self.assertEqual(json.loads(pending)["problem_received_at"], self.now)
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse((self.root / clock.RECEIPT).exists())
        with self.assertRaisesRegex(clock.DeadlineError, "cannot be changed"):
            clock.begin(self.root, "2025-01-01T01:00:00Z", now=self.now + 12000)
        self.assertEqual(temporary.read_bytes(), pending)
        receipt = clock.begin(self.root, now=self.now + 12000)
        self.assertEqual(receipt["received_at"], self.now)
        self.assertEqual(receipt["deadline_at"], self.now + 9000)
        self.assertEqual(config.read_bytes(), pending)
        self.assertFalse(temporary.exists())
        with self.assertRaisesRegex(clock.DeadlineError, "expired"):
            clock.budget(self.root, 1, now=self.now + 12000)

    def test_partial_or_unexpected_pending_config_is_preserved_and_rejected(self):
        config = self.config()
        before = config.read_bytes()
        expected = dict(json.loads(before), problem_received_at=self.now)
        temporary = config.with_name(config.name + ".problem-time.tmp")
        cases = [b'{"problem_received_at":', before,
                 (json.dumps(dict(expected, spec="OTHER.md"), indent=2) + "\n").encode("utf-8"),
                 (json.dumps(dict(expected, problem_received_at=True), indent=2) + "\n").encode("utf-8")]
        for raw in cases:
            with self.subTest(raw=raw):
                temporary.write_bytes(raw)
                with self.assertRaises(clock.DeadlineError):
                    clock.begin(self.root, now=self.now + 12000)
                self.assertEqual(temporary.read_bytes(), raw)
                self.assertEqual(config.read_bytes(), before)
                self.assertFalse((self.root / clock.RECEIPT).exists())

    def test_pending_config_cannot_replace_changed_original_config(self):
        config = self.config()
        pending = dict(json.loads(config.read_bytes()), problem_received_at=self.now)
        temporary = config.with_name(config.name + ".problem-time.tmp")
        temporary.write_bytes((json.dumps(pending, indent=2) + "\n").encode("utf-8"))
        changed = {"problem_time_limit_seconds": 9000, "spec": "USER-CHANGED.md"}
        config.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(clock.DeadlineError, "differs from the original config"):
            clock.begin(self.root, now=self.now + 12000)
        self.assertEqual(json.loads(config.read_bytes()), changed)
        self.assertFalse((self.root / clock.RECEIPT).exists())

    def test_existing_receipt_supplies_missing_anchor_and_changed_anchor_is_rejected(self):
        original = clock.begin(self.root, now=self.now)
        path = self.config(9600)
        self.assertEqual(clock.begin(self.root, now=self.now + 100), original)
        config = json.loads(path.read_bytes())
        self.assertEqual(config["problem_received_at"], self.now)
        config["problem_received_at"] += 1
        path.write_text(json.dumps(config), encoding="utf-8")
        for call in (lambda: clock.load(self.root), lambda: clock.begin(self.root, now=self.now + 100)):
            with self.assertRaisesRegex(clock.DeadlineError, "original config timestamp"):
                call()

    def test_start_cli_obeys_existing_operation_lock(self):
        loader = importlib.util.spec_from_file_location("clock_test_lock", SOURCE / "operation_lock.py")
        locking = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(locking)
        with locking.operation_lock(self.root, "requirements"):
            self.assertEqual(clock.main(["--root", str(self.root), "start"]), 2)
        self.assertIsNone(clock.load(self.root))

    def test_corrupt_transplanted_and_forged_deadline_rejected(self):
        receipt = clock.begin(self.root, now=self.now)
        path = self.root / clock.RECEIPT
        other = self.root / "other"
        (other / ".harness").mkdir(parents=True)
        (other / clock.RECEIPT).write_bytes(path.read_bytes())
        with self.assertRaisesRegex(clock.DeadlineError, "another project"):
            clock.load(other)
        for raw in (b"{", path.read_bytes().replace(b"9600", b"9601", 1), b'{"version":1,"version":1}'):
            path.write_bytes(raw)
            with self.assertRaises(clock.DeadlineError):
                clock.load(self.root)
        receipt["deadline_at"] += 1
        receipt["sha256"] = clock._checksum({k: v for k, v in receipt.items() if k != "sha256"})
        path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(clock.DeadlineError, "deadline differs"):
            clock.load(self.root)

    def test_config_limit_is_exact_and_cannot_disable_present_receipt(self):
        self.assertFalse(clock.required({}))
        self.assertTrue(clock.required({"problem_time_limit_seconds": 9000}))
        self.assertTrue(clock.required({"problem_time_limit_seconds": 9600}))
        for value in (None, 0, 150, 9001, 9601, "9000", 9000.0, 9600.0, True):
            with self.subTest(value=value), self.assertRaises(clock.DeadlineError):
                clock.required({"problem_time_limit_seconds": value})
        clock.begin(self.root, now=self.now)
        with self.assertRaises(clock.DeadlineError):
            clock.budget(self.root, 1, required=clock.required({}), now=self.now + 9600)

    def test_invalid_requested_budget_and_clock_rollback_rejected(self):
        clock.begin(self.root, now=self.now)
        for value in (True, 0, -1, "1", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(clock.DeadlineError):
                clock.budget(self.root, value, now=self.now)
        with self.assertRaisesRegex(clock.DeadlineError, "future"):
            clock.budget(self.root, 1, now=self.now - 1)



if __name__ == "__main__":
    unittest.main()
