import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lifecycle = load("harness_lifecycle_tests", BASE / "lifecycle.py")
processes = load("harness_lifecycle_processes", BASE / "processes.py")


class StopRun(RuntimeError):
    pass


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness lifecycle ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.started = []

    def start(self, *args, **kwargs):
        process = processes.start_process(*args, **kwargs)
        self.started.append(process)
        return process

    def execute(self, name, program, timeout=5, guard=None, start=None, stop=None):
        return lifecycle.execute([sys.executable, "-c", program], self.root, self.root / name,
                                 timeout, guard=guard, start_process=start or self.start,
                                 stop_process=stop or processes.stop_process, error_type=StopRun)

    def result(self, name):
        return json.loads((self.root / (name + ".exit.json")).read_text(encoding="utf-8"))

    def test_normal_nonzero_and_launch_failure_have_outcomes(self):
        self.assertEqual(self.execute("ok", "print('output')"), 0)
        self.assertEqual(self.execute("bad", "import sys;sys.exit(7)"), 7)
        for name, code in [("ok", 0), ("bad", 7)]:
            result = self.result(name)
            self.assertEqual(result["exit_code"], code)
            self.assertEqual(result["termination_reason"], "completed")
            self.assertTrue(result["cleanup"]["completed"])
        def fail_launch(*args, **kwargs):
            raise FileNotFoundError("missing executable")
        with self.assertRaises(FileNotFoundError):
            self.execute("missing", "pass", start=fail_launch)
        result = self.result("missing")
        self.assertEqual(result["termination_reason"], "launch_failed")
        self.assertFalse(result["cleanup"]["attempted"])
        self.assertIsNone(result["exit_code"])

    def test_timeout_guard_failure_and_cancellation_preserve_errors_and_cleanup(self):
        with self.assertRaisesRegex(StopRun, "Timeout"):
            self.execute("timeout", "import time;print('before timeout',flush=True);time.sleep(30)", timeout=0.2)
        for name, error in [("guard", StopRun("source changed")), ("cancel", KeyboardInterrupt())]:
            def fail(error=error):
                raise error
            with self.subTest(name=name), self.assertRaises(type(error)) as caught:
                self.execute(name, "import time;time.sleep(30)", guard=fail)
            self.assertIs(caught.exception, error)
        for name, reason in [("timeout", "timed_out"), ("guard", "guard_failed"), ("cancel", "cancelled")]:
            result = self.result(name)
            self.assertEqual(result["termination_reason"], reason)
            self.assertTrue(result["cleanup"]["completed"])
            self.assertIsNotNone(result["exit_code"])
            self.assertTrue(result["error"])
        self.assertTrue(all(process.poll() is not None for process in self.started))

    def test_cleanup_failure_cannot_be_recorded_as_success(self):
        def broken_cleanup(process):
            processes.stop_process(process)
            raise OSError("cleanup evidence failure")
        with self.assertRaisesRegex(OSError, "cleanup evidence failure"):
            self.execute("cleanup", "pass", stop=broken_cleanup)
        result = self.result("cleanup")
        self.assertEqual(result["termination_reason"], "cleanup_failed")
        self.assertFalse(result["cleanup"]["completed"])
        self.assertIn("cleanup evidence failure", result["cleanup"]["error"])

    def test_running_marker_write_failure_still_cleans_started_process(self):
        original_write = lifecycle.write_json
        error = OSError("injected running marker write failure")
        def fail_running_marker(path, value):
            if Path(path).name.endswith(".running.json") and value.get("pid") is not None:
                raise error
            return original_write(path, value)
        try:
            with patch.object(lifecycle, "write_json", side_effect=fail_running_marker):
                with self.assertRaises(OSError) as caught:
                    self.execute("marker-failure", "import time;time.sleep(30)")
            self.assertIs(caught.exception, error)
            self.assertEqual(len(self.started), 1)
            self.assertIsNotNone(self.started[0].poll(), "Failed marker publication must not leave a live worker")
            result = self.result("marker-failure")
            self.assertTrue(result["cleanup"]["attempted"])
            self.assertTrue(result["cleanup"]["completed"])
            self.assertIsNotNone(result["exit_code"])
            self.assertIn("injected running marker", result["error"])
        finally:
            for process in self.started:
                processes.stop_process(process)



    def test_post_cleanup_guard_detects_a_last_descendant_write(self):
        source = self.root / "source.txt"
        source.write_text("before", encoding="utf-8")
        def guard():
            if source.read_text(encoding="utf-8") != "before":
                raise StopRun("source changed during cleanup")
        def late_write(process):
            processes.stop_process(process)
            source.write_text("late descendant write", encoding="utf-8")
        with self.assertRaisesRegex(StopRun, "source changed during cleanup"):
            self.execute("late-write", "pass", guard=guard, stop=late_write)
        result = self.result("late-write")
        self.assertEqual(result["termination_reason"], "guard_failed")
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["cleanup"]["completed"])


class GuardCadenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="guard-cadence-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.clock = 0.0
        self.heavy = []
        self.light = []
        self.stopped = []

    def execute(self, duration, interval=5.0, light=None, heavy=None):
        owner = self
        class Process:
            pid = 12345
            returncode = None
            def poll(self):
                return self.returncode
            def wait(self, timeout):
                owner.clock = min(duration, owner.clock + timeout)
                if owner.clock == duration:
                    self.returncode = 0
                    return 0
                raise subprocess.TimeoutExpired("fixture", timeout)
        process = Process()
        def stop(child):
            self.stopped.append(child)
            if child.returncode is None:
                child.returncode = -1
        with patch.object(lifecycle.time, "monotonic", side_effect=lambda: self.clock):
            return lifecycle.execute(["offline-fixture"], self.root, self.root / "run", 30,
                guard=heavy or (lambda: self.heavy.append(self.clock)),
                deadline_guard=light or (lambda: self.light.append(self.clock)), guard_interval_seconds=interval,
                start_process=lambda *a, **kw: process, stop_process=stop, error_type=StopRun)

    def test_default_heavy_guard_is_five_seconds_and_final_check_is_unconditional(self):
        self.assertEqual(self.execute(10.1), 0)
        self.assertEqual(self.heavy, [0.0, 5.0, 10.0, 10.1])
        self.assertGreater(len(self.light), 40)
        self.assertLessEqual(max(b - a for a, b in zip(self.light, self.light[1:])), 0.25)
        self.assertEqual(len(self.stopped), 1)

    def test_zero_interval_checks_each_poll_and_still_checks_after_exit(self):
        self.assertEqual(self.execute(0.85, interval=0), 0)
        self.assertEqual(self.heavy, [0.0, 0.25, 0.5, 0.75, 0.85])

    def test_deadline_guard_stops_child_between_heavy_checks(self):
        def deadline():
            self.light.append(self.clock)
            if self.clock >= 0.75:
                raise StopRun("deadline expired")
        with self.assertRaisesRegex(StopRun, "deadline expired"):
            self.execute(10, light=deadline)
        self.assertEqual(self.heavy, [0.0])
        self.assertEqual(self.clock, 0.75)
        self.assertIsNotNone(self.stopped[0].returncode)
        outcome = json.loads((self.root / "run.exit.json").read_text())
        self.assertEqual(outcome["termination_reason"], "guard_failed")
        self.assertTrue(outcome["cleanup"]["completed"])

    def test_short_process_final_guard_detects_changes_between_polls(self):
        def changed():
            self.heavy.append(self.clock)
            if self.clock > 0:
                raise StopRun("source changed")
        with self.assertRaisesRegex(StopRun, "source changed"):
            self.execute(0.1, heavy=changed)
        self.assertEqual(self.heavy, [0.0, 0.1])
        self.assertEqual(self.stopped[0].returncode, 0)

    def test_invalid_interval_is_rejected_before_launch(self):
        for value in (True, -1, float("nan"), float("inf"), "5"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Guard interval"):
                self.execute(1, interval=value)
        self.assertEqual(self.stopped, [])


if __name__ == "__main__":
    unittest.main()
