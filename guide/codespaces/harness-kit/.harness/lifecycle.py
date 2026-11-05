"""Durable execution outcomes using the caller's verified process-tree helpers."""
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

_time_spec = importlib.util.spec_from_file_location("lifecycle_problem_time", Path(__file__).with_name("problem_time.py"))
problem_time = importlib.util.module_from_spec(_time_spec)
_time_spec.loader.exec_module(problem_time)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))


def execute(argv, cwd, prefix, timeout, prompt=None, guard=None, *,
            start_process, stop_process, error_type=RuntimeError,
            guard_interval_seconds=5.0, deadline_guard=None):
    """Return the real exit code; preserve exceptions after recording and cleanup."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Execution timeout must be a finite positive number")
    if (isinstance(guard_interval_seconds, bool) or not isinstance(guard_interval_seconds, (int, float))
            or not math.isfinite(guard_interval_seconds) or guard_interval_seconds < 0):
        raise ValueError("Guard interval must be a finite nonnegative number")
    prefix = Path(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    outcome = {"version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
               "finished_at": None, "exit_code": None, "seconds": None,
               "termination_reason": "launch_failed", "error": None,
               "cleanup": {"attempted": False, "completed": False, "error": None}}
    primary_error = None
    phase = "launch_failed"
    try:
        receipt = problem_time.load(cwd)
        effective_timeout = problem_time.budget(cwd, timeout, required=receipt is not None)
        def check_deadline():
            if receipt is not None:
                if problem_time.load(cwd, required=True) != receipt:
                    raise error_type("Problem receipt changed during execution")
                problem_time.budget(cwd, 1, required=True)
            if deadline_guard:
                deadline_guard()
        last_heavy_check = None
        pycache = prefix.with_suffix(".pycache")
        pycache.mkdir(exist_ok=False)
        python_env = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": str(pycache.resolve())}
        environment = dict(os.environ, **python_env)
        environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
        environment["PYTHONIOENCODING"] = "utf-8"
        environment.pop("PYTHONHOME", None)
        environment.pop("VIRTUAL_ENV", None)
        if sys.prefix != sys.base_prefix:
            environment["VIRTUAL_ENV"] = sys.prefix
        stdin_path = prefix.with_suffix(".input.txt")
        stdin_path.write_text(prompt or "", encoding="utf-8")
        write_json(prefix.with_suffix(".command.json"),
                   {"argv": argv, "cwd": str(cwd), "timeout": timeout, "python_environment": python_env})
        with stdin_path.open("rb") as stdin, prefix.with_suffix(".stdout.log").open("wb") as stdout, \
                prefix.with_suffix(".stderr.log").open("wb") as stderr:
            check_deadline()
            write_json(prefix.with_suffix(".launch.json"), {"started_at": outcome["started_at"], "pid": None})
            process = start_process(argv, cwd=str(cwd), stdin=stdin, stdout=stdout, stderr=stderr,
                                    start_new_session=os.name != "nt", env=environment,
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            try:
                write_json(prefix.with_suffix(".running.json"), {"pid": process.pid, "started_at": outcome["started_at"]})
                while process.poll() is None:
                    if time.monotonic() - started >= effective_timeout:
                        phase = "timed_out"
                        raise error_type("Timeout: " + str(prefix))
                    phase = "guard_failed"
                    check_deadline()
                    if guard and (last_heavy_check is None or time.monotonic() - last_heavy_check >= guard_interval_seconds):
                        guard()
                        last_heavy_check = time.monotonic()
                    phase = "execution_failed"
                    try:
                        process.wait(timeout=min(0.25, max(0.01, effective_timeout - (time.monotonic() - started))))
                    except subprocess.TimeoutExpired:
                        pass
                outcome["exit_code"] = process.returncode
                phase = "completed"
            finally:
                # Also stop remaining descendants after their parent exits normally.
                active_error = sys.exc_info()[1]
                outcome["cleanup"]["attempted"] = True
                try:
                    stop_process(process)
                    if process.poll() is None:
                        raise RuntimeError("Started process is still running after cleanup")
                    outcome["cleanup"]["completed"] = True
                except BaseException as cleanup_error:
                    outcome["cleanup"]["error"] = "{}: {}".format(type(cleanup_error).__name__, cleanup_error)
                    # Never replace the timeout, cancellation or guard exception.
                    if active_error is None:
                        phase = "cleanup_failed"
                        raise
                finally:
                    outcome["exit_code"] = process.poll()
        # A surviving descendant can still write while cleanup is in progress.
        phase = "guard_failed"
        check_deadline()
        if guard:
            guard()
        phase = "completed"
        return outcome["exit_code"]
    except BaseException as exc:
        primary_error = exc
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            phase = "cancelled"
        outcome["error"] = "{}: {}".format(type(exc).__name__, exc)
        raise
    finally:
        outcome["termination_reason"] = phase
        outcome["finished_at"] = datetime.now(timezone.utc).isoformat()
        outcome["seconds"] = time.monotonic() - started
        try:
            write_json(prefix.with_suffix(".exit.json"), outcome)
        except OSError as evidence_error:
            if primary_error is None:
                raise
            # Preserve the original failure, but never silently hide lost evidence.
            print("Execution evidence write failed: " + str(evidence_error), file=sys.stderr)
