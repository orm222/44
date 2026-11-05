"""One immutable deadline: 160 minutes for new projects, preserving legacy limits.

Complete pending config writes resume with their original timestamp. Partial or
unexpected temporary files stop recovery; preserve them for manual inspection.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import stat
import sys
import time

RECEIPT = ".harness/problem-time.json"
LIMIT_SECONDS = 9600
VALID_LIMITS = (9000, 9600)
CONFIG = "config.json"


class DeadlineError(ValueError):
    pass


def required(config):
    if "problem_time_limit_seconds" not in config:
        return False
    value = config["problem_time_limit_seconds"]
    if type(value) is not int or value not in VALID_LIMITS:
        raise DeadlineError("problem_time_limit_seconds must be exactly 9000 or 9600")
    return True


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise DeadlineError(name + " must be a finite number")
    return float(value)


def _now(value):
    return _number(time.time() if value is None else value, "Current time")


def _path(root):
    root = Path(root).absolute()
    path = root / RECEIPT
    if not root.is_dir() or root != root.resolve():
        raise DeadlineError("Problem-time project root is missing or redirected")
    for item in (root, path.parent, path):
        if item.is_symlink() or item.absolute() != item.resolve():
            raise DeadlineError("Problem-time receipt path is redirected")
        if item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
            raise DeadlineError("Problem-time receipt path is redirected")
    return root, path


def _checksum(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise DeadlineError("Duplicate problem-time receipt field: " + key)
        result[key] = value
    return result


def _config(root):
    path = Path(root) / CONFIG
    for item in (path.parent, path):
        if item.is_symlink() or item.absolute() != item.resolve() or (
                item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400):
            raise DeadlineError("Problem-time config path is redirected")
    if not path.exists():
        return path, None
    if not path.is_file() or path.stat().st_nlink != 1:
        raise DeadlineError("Problem-time config must be an ordinary file")
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_pairs)
    except (OSError, ValueError, UnicodeError) as exc:
        raise DeadlineError("Cannot read problem-time config: " + str(exc)) from exc
    if not isinstance(config, dict):
        raise DeadlineError("Problem-time config must be an object")
    required(config)
    if "problem_received_at" in config:
        _number(config["problem_received_at"], "Configured problem_received_at")
    return path, config


def _pending_anchor(path, config):
    temporary = path.with_name(path.name + ".problem-time.tmp")
    if not temporary.exists() and not temporary.is_symlink():
        return None
    if config is None or "problem_received_at" in config:
        raise DeadlineError("Unexpected pending problem-time config")
    info = temporary.lstat()
    if (temporary.absolute() != temporary.resolve() or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400):
        raise DeadlineError("Pending problem-time config is redirected or not an ordinary file")
    try:
        raw = temporary.read_bytes()
        pending = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        if not isinstance(pending, dict) or "problem_received_at" not in pending:
            raise DeadlineError("Pending problem-time config has no original timestamp")
        _number(pending["problem_received_at"], "Pending problem_received_at")
        required(pending)
        expected = dict(config, problem_received_at=pending["problem_received_at"])
        if "problem_time_limit_seconds" not in config and "problem_time_limit_seconds" in pending:
            expected["problem_time_limit_seconds"] = pending["problem_time_limit_seconds"]
        if pending != expected or raw != (json.dumps(expected, ensure_ascii=False, indent=2) + "\n").encode("utf-8"):
            raise DeadlineError("Pending problem-time config differs from the original config")
        return pending
    except (OSError, ValueError, UnicodeError) as exc:
        raise DeadlineError("Cannot recover pending problem-time config: " + str(exc)) from exc


def _anchor(path, config, received, limit):
    if config is None or "problem_received_at" in config:
        return
    target = dict(config, problem_received_at=received, problem_time_limit_seconds=limit)
    temporary = path.with_name(path.name + ".problem-time.tmp")
    # Publish the original time before creating the receipt, so a crash cannot reset it.
    if _pending_anchor(path, config) is None:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(target, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    if _config(path.parent)[1] != config:
        raise DeadlineError("Problem config changed while preserving original timestamp")
    pending = _pending_anchor(path, config)
    if pending["problem_received_at"] != received or pending.get("problem_time_limit_seconds", 9000) != limit:
        raise DeadlineError("Pending problem-time timestamp cannot be changed")
    os.replace(str(temporary), str(path))


def load(root, required=False):
    """Read the original receipt; expiration never prevents inspection."""
    try:
        root, path = _path(root)
        if not path.exists():
            if required:
                raise DeadlineError("Missing problem-time receipt; explicitly start the problem before running")
            return None
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise DeadlineError("Problem-time receipt must be an ordinary unlinked file")
        raw = path.read_bytes()
        after = path.lstat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
        if any(getattr(before, field) != getattr(after, field) for field in fields):
            raise DeadlineError("Problem-time receipt changed while reading")
        _path(root)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        keys = {"version", "project_root", "limit_seconds", "received_at", "deadline_at", "sha256"}
        if not isinstance(value, dict) or set(value) != keys:
            raise DeadlineError("Invalid problem-time receipt fields")
        signed = {key: val for key, val in value.items() if key != "sha256"}
        if value["sha256"] != _checksum(signed):
            raise DeadlineError("Problem-time receipt checksum differs")
        if type(value["version"]) is not int or value["version"] != 1:
            raise DeadlineError("Invalid problem-time receipt version")
        if value["project_root"] != str(root):
            raise DeadlineError("Problem-time receipt belongs to another project")
        if type(value["limit_seconds"]) is not int or value["limit_seconds"] not in VALID_LIMITS:
            raise DeadlineError("Problem-time receipt limit must be exactly 9000 or 9600")
        received = _number(value["received_at"], "Receipt received_at")
        deadline = _number(value["deadline_at"], "Receipt deadline_at")
        if deadline != received + value["limit_seconds"]:
            raise DeadlineError("Problem-time receipt deadline differs from its original receipt time")
        _, config = _config(root)
        if config is not None and "problem_received_at" in config and config["problem_received_at"] != received:
            raise DeadlineError("Problem-time receipt differs from its original config timestamp")
        if config is not None:
            configured = config.get("problem_time_limit_seconds", 9000 if "problem_received_at" in config else value["limit_seconds"])
            if configured != value["limit_seconds"]:
                raise DeadlineError("Problem-time receipt differs from its original config limit")
        return value
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        if isinstance(exc, DeadlineError):
            raise
        raise DeadlineError("Cannot read problem-time receipt: " + str(exc)) from exc


def _received(value):
    if not isinstance(value, str):
        raise DeadlineError("received_at must be an ISO timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Timezone is required")
        return _number(parsed.timestamp(), "received_at")
    except (ValueError, OverflowError, OSError) as exc:
        raise DeadlineError("received_at must be an ISO timestamp with a timezone") from exc


def begin(root, received_at=None, now=None):
    """Create once using exclusive creation; retries never reset the clock."""
    current = _now(now)
    original = load(root)
    root, path = _path(root)
    config_path, config = _config(root)
    anchor = (config or {}).get("problem_received_at")
    pending = _pending_anchor(config_path, config)
    previous = original["received_at"] if original is not None else anchor
    if pending is not None:
        if previous is not None and pending["problem_received_at"] != previous:
            raise DeadlineError("Pending problem-time timestamp differs from original receipt")
        previous = pending["problem_received_at"]
    # Old anchors without a limit were created when the only allowed limit was 9000.
    limit = (original["limit_seconds"] if original is not None else
             pending.get("problem_time_limit_seconds", 9000) if pending is not None else
             (config or {}).get("problem_time_limit_seconds", 9000 if anchor is not None else LIMIT_SECONDS))
    received = _received(received_at) if received_at is not None else (previous if previous is not None else current)
    if received > current:
        raise DeadlineError("Problem received_at cannot be in the future")
    if previous is not None and received != previous:
        raise DeadlineError("Problem receipt already exists; its start time cannot be changed")
    try:
        _anchor(config_path, config, received, limit)
    except OSError as exc:
        raise DeadlineError("Cannot preserve original problem timestamp: " + str(exc)) from exc
    if original is not None:
        return original
    value = {"version": 1, "project_root": str(root), "limit_seconds": limit,
             "received_at": received, "deadline_at": received + limit}
    value["sha256"] = _checksum(value)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _path(root)
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return begin(root, received_at, now=current)
    except OSError as exc:
        raise DeadlineError("Cannot create problem-time receipt: " + str(exc)) from exc
    return load(root, required=True)


def status(root, required=False, now=None):
    value = load(root, required=required)
    if value is None:
        return {"started": False}
    current = _now(now)
    if value["received_at"] > current:
        raise DeadlineError("Problem receipt is in the future; check the system clock")
    remaining = value["deadline_at"] - current
    return dict(value, started=True, remaining_seconds=max(0.0, remaining), expired=remaining <= 0,
                remaining_minutes=max(0.0, remaining) / 60,
                received_at_utc=datetime.fromtimestamp(value["received_at"], timezone.utc).isoformat(),
                deadline_at_utc=datetime.fromtimestamp(value["deadline_at"], timezone.utc).isoformat())


def budget(root, seconds, required=False, now=None):
    requested = _number(seconds, "Requested timeout")
    if requested <= 0:
        raise DeadlineError("Requested timeout must be positive")
    value = status(root, required=required, now=now)
    if not value["started"]:
        return requested
    if value["expired"]:
        raise DeadlineError("Problem deadline expired ({} minutes from receipt, including waiting)".format(value["limit_seconds"] // 60))
    return min(requested, value["remaining_seconds"])


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    modes = parser.add_subparsers(dest="mode", required=True)
    start = modes.add_parser("start")
    start.add_argument("--received-at")
    modes.add_parser("status")
    args = parser.parse_args(arguments)
    try:
        if args.mode == "start":
            loader = importlib.util.spec_from_file_location("problem_time_lock", Path(__file__).with_name("operation_lock.py"))
            locking = importlib.util.module_from_spec(loader)
            loader.loader.exec_module(locking)
            try:
                with locking.operation_lock(args.root, "problem-start"):
                    begin(args.root, args.received_at)
            except locking.OperationBusy as exc:
                raise DeadlineError(str(exc)) from exc
        print(json.dumps(status(args.root), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print("Problem clock stopped: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
