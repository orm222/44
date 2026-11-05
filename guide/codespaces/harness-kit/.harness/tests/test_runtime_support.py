"""Process and lock boundaries retained by the document-only kit."""
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest


BASE = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, BASE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


locks = load("runtime_locks", "operation_lock.py")
processes = load("runtime_processes", "processes.py")
lifecycle = load("runtime_lifecycle", "lifecycle.py")


class RuntimeSupportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="document-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def child_lock(self, crash=False):
        program = '''import importlib.util,os,sys
from pathlib import Path
s=importlib.util.spec_from_file_location('child_lock',sys.argv[1])
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
try:
    with m.operation_lock(Path(sys.argv[2]), 'document'):
        if sys.argv[3]=='crash': os._exit(71)
except m.OperationBusy:
    sys.exit(75)
'''
        return subprocess.run([sys.executable, "-B", "-c", program, str(BASE / "operation_lock.py"),
                               str(self.root), "crash" if crash else "normal"],
                              capture_output=True, timeout=15)

    def test_other_process_is_blocked_and_exception_keeps_a_reusable_lock(self):
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with locks.operation_lock(self.root, "requirements"):
                child = self.child_lock()
                self.assertEqual(child.returncode, 75, child.stderr)
                raise RuntimeError("interrupted")
        self.assertTrue(locks.lock_path(self.root).is_file())
        self.assertEqual(self.child_lock().returncode, 0)

    def test_nested_imports_share_ownership_but_other_threads_cannot_bypass_it(self):
        separately_loaded = load("another_document_lock", "operation_lock.py")
        with locks.operation_lock(self.root, "requirements") as outer:
            with self.assertRaises(locks.OperationBusy):
                with locks.operation_lock(self.root, "clarify"):
                    pass
            with separately_loaded.operation_lock(self.root, "clarify", allow_nested=True) as nested:
                self.assertEqual(nested, outer)
            outcomes = []
            def another_thread():
                try:
                    with locks.operation_lock(self.root, "parallel", allow_nested=True):
                        outcomes.append("acquired")
                except locks.OperationBusy:
                    outcomes.append("blocked")
            thread = threading.Thread(target=another_thread)
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, ["blocked"])

    def test_actual_process_exit_releases_the_os_lock(self):
        child = self.child_lock(crash=True)
        self.assertEqual(child.returncode, 71, child.stderr)
        self.assertEqual(self.child_lock().returncode, 0)

    def test_unknown_reserved_lock_state_is_not_overwritten(self):
        path = locks.lock_path(self.root)
        path.parent.mkdir(parents=True)
        original = b"Q" + b"a" * 64 + b"legacy pending operation"
        path.write_bytes(original)
        with self.assertRaises(locks.OperationBusy):
            with locks.operation_lock(self.root, "requirements"):
                pass
        self.assertEqual(path.read_bytes(), original)



    def test_normal_exit_and_timeout_both_clean_up_spawned_children(self):
        for wait in (False, True):
            with self.subTest(timeout=wait):
                listener = socket.socket()
                self.addCleanup(listener.close)
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                listener.settimeout(15)
                child = ("import socket,time; s=socket.create_connection(('127.0.0.1', "
                         + str(listener.getsockname()[1]) + ")); s.sendall(b'ready'); time.sleep(30)")
                release = self.root / ("release-" + str(wait))
                parent = ("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); "
                          + ("time.sleep(30)" if wait else "from pathlib import Path\nwhile not Path("
                             + repr(str(release)) + ").exists(): time.sleep(.01)"))
                connections = []
                def start(*args, **kwargs):
                    process = processes.start_process(*args, **kwargs)
                    try:
                        client, _ = listener.accept()
                        client.settimeout(5)
                        self.addCleanup(client.close)
                        self.assertEqual(client.recv(5), b"ready")
                        connections.append(client)
                        if not wait:
                            release.write_text("ready", encoding="utf-8")
                    except BaseException:
                        processes.stop_process(process)
                        raise
                    return process
                prefix = self.root / ("worker-" + str(wait))
                def execute():
                    return lifecycle.execute([sys.executable, "-B", "-c", parent], self.root, prefix,
                                             0.2 if wait else 15, start_process=start,
                                             stop_process=processes.stop_process)
                if wait:
                    with self.assertRaisesRegex(RuntimeError, "Timeout"):
                        execute()
                else:
                    self.assertEqual(execute(), 0)
                self.assertEqual(len(connections), 1)
                try:
                    self.assertEqual(connections[0].recv(1), b"", "A descendant survived cleanup")
                except ConnectionResetError:
                    pass
                outcome = json.loads(prefix.with_suffix(".exit.json").read_text(encoding="utf-8"))
                self.assertTrue(outcome["cleanup"]["completed"])
                self.assertEqual(outcome["termination_reason"], "timed_out" if wait else "completed")
