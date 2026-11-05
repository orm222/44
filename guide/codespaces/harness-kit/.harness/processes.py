"""Start native commands and clean up their complete process trees."""
import os
from pathlib import Path
import shutil
import signal
import subprocess

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL

    class _JobBasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _JobExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JobBasicLimits), ("IoInfo", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def start_process(*args, **kwargs):
    if os.name == "nt":
        command = args[0] if args else kwargs["args"]
        if isinstance(command, (str, bytes)):
            raise ValueError("Windows command must be an argument array with a native executable or an explicitly chosen shell.")
        executable = os.fsdecode(kwargs.get("executable") or command[0])
        search_path = (kwargs.get("env") or os.environ).get("PATH")
        resolved = shutil.which(executable, path=search_path)
        if not resolved and (Path(executable).is_absolute() or Path(executable).parent != Path(".")):
            candidate = Path(kwargs.get("cwd") or Path.cwd()) / executable
            resolved = shutil.which(candidate.name, path=str(candidate.parent))
        if Path(executable).suffix.lower() in (".cmd", ".bat") or (resolved and Path(resolved).suffix.lower() in (".cmd", ".bat")):
            raise ValueError("Windows batch shim rejected; use a native executable or an explicitly chosen shell: " + executable)
    process = subprocess.Popen(*args, **kwargs)
    if os.name == "nt":
        job = _kernel32.CreateJobObjectW(None, None)
        limits = _JobExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not job or not _kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not _kernel32.AssignProcessToJobObject(job, int(process._handle))):
            error = ctypes.WinError(ctypes.get_last_error())
            if job:
                _kernel32.CloseHandle(job)
            process.kill()
            process.wait()
            raise error
        process._harness_job = job
    return process


def finish_process(process):
    if os.name == "nt" and getattr(process, "_harness_job", None):
        error = None
        if not _kernel32.TerminateJobObject(process._harness_job, 1):
            error = ctypes.WinError(ctypes.get_last_error(), "TerminateJobObject failed")
        if _kernel32.CloseHandle(process._harness_job):
            process._harness_job = None
        else:
            error = error or ctypes.WinError(ctypes.get_last_error(), "CloseHandle failed")
        if error is not None:
            raise error
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def stop_process(process):
    try:
        finish_process(process)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
