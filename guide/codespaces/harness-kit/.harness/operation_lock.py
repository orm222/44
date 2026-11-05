"""A project-scoped controller lock, independent of evidence folder selection."""
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading
import types

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class OperationBusy(RuntimeError):
    pass


# Controllers load helpers with importlib under different names. Share ownership
# only inside this process, never through an inherited environment bypass token.
_candidate = types.ModuleType("_codex_harness_operation_owners_v1")
_candidate.active = {}
_candidate.mutex = threading.RLock()
_owners = sys.modules.setdefault(_candidate.__name__, _candidate)


def lock_path(project):
    return Path(project).resolve() / ".harness" / "work" / "operation.lock"


def _check_idle(stream):
    stream.seek(0)
    if stream.read(1) not in (b"", b"\0"):
        raise OperationBusy("Unfinished or unrecognized operation lock; preserve the existing project state")


@contextlib.contextmanager
def operation_lock(project, operation, allow_nested=False):
    """Nested calls require explicit opt-in and the same process AND thread."""
    project = Path(project).resolve()
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("Operation name is required")
    path = lock_path(project)
    key = str(path)
    identity = (os.getpid(), threading.get_ident())
    nested = False
    stream = None
    with _owners.mutex:
        current = _owners.active.get(key)
        if current is not None:
            if not allow_nested or current["identity"] != identity:
                raise OperationBusy("Another harness operation is active: " + current["lease"]["operation"])
            _check_idle(current["stream"])
            lease = current["lease"]
            nested = True
        else:
            if any(member.is_symlink() or member.absolute() != member.resolve()
                   for member in (path, path.parent, path.parent.parent)):
                raise OperationBusy("Operation lock path is redirected: " + str(path))
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = os.fdopen(os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600), "r+b", buffering=0)
            try:
                stream.seek(0)
                if os.name == "nt":
                    # Windows permits locking beyond EOF; initialize only as owner.
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                stream.close()
                raise OperationBusy("Another harness operation is using this project: " + str(project)) from exc
            lease = {"project": str(project), "operation": operation, "pid": identity[0],
                     "thread": identity[1], "started_at": datetime.now(timezone.utc).isoformat()}
            try:
                _check_idle(stream)
                # Preserve the reserved prefix when reopening an older inactive lock.
                stream.seek(65)
                stream.truncate()
                stream.write(json.dumps(lease, ensure_ascii=False).encode("utf-8"))
                stream.flush()
            except BaseException:
                stream.close()
                raise
            _owners.active[key] = {"identity": identity, "lease": lease, "stream": stream}
    if nested:
        yield dict(lease)
        return
    try:
        yield dict(lease)
    finally:
        with _owners.mutex:
            try:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
            finally:
                stream.close()
                _owners.active.pop(key, None)
    # Keep the inode: unlinking a lock can give competing owners different files.
