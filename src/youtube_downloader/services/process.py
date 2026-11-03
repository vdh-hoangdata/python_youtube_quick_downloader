"""Own a subprocess tree so Cancel also stops FFmpeg and JS helpers."""

from collections import deque
from collections.abc import Callable
import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading

from youtube_downloader.services.models import DownloadCancelled


logger = logging.getLogger(__name__)


class WindowsJob:
    """A private, unnamed Windows Job Object; never kill by executable name."""

    def __init__(self) -> None:
        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen[str]) -> None:
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> None:
        if self.handle and not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class ProcessRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._job: WindowsJob | None = None

    def _stop_locked(self) -> None:
        process = self._process
        if process is None:
            return
        if self._job is not None:
            self._job.terminate()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()  # Startup failed before the gated child could spawn helpers.

    def cancel(self) -> None:
        """Thread-safe and nonblocking; the worker waits and performs cleanup."""
        with self._lock:
            self._stop_locked()

    def run(
        self,
        command: list[str],
        on_line: Callable[[str], None],
        cancelled: threading.Event,
        cwd: Path,
    ) -> None:
        tail: deque[str] = deque(maxlen=12)
        env = os.environ.copy()
        env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "TMP": str(cwd), "TEMP": str(cwd)})
        process: subprocess.Popen[str] | None = None
        try:
            with self._lock:
                if cancelled.is_set():
                    raise DownloadCancelled()
                self._process = process = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    shell=False, text=True, encoding="utf-8", errors="replace", bufsize=1,
                    env=env, cwd=cwd,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
                if os.name == "nt":
                    job = WindowsJob()
                    try:
                        job.assign(process)
                    except OSError:
                        job.close()
                        raise
                    self._job = job
                assert process.stdin is not None
                process.stdin.write("start\n")
                process.stdin.flush()
                process.stdin.close()
            assert process.stdout is not None
            for line in process.stdout:
                if cancelled.is_set():
                    raise DownloadCancelled()
                line = line.rstrip("\r\n")
                # Keep warnings/errors; don't log large metadata or signed media URLs.
                if line and not line.startswith(("{", "__YLD_")):
                    tail.append(line)
                    logger.info("yt-dlp: %s", line)
                on_line(line)
            return_code = process.wait()
            if cancelled.is_set():
                raise DownloadCancelled()
            if return_code:
                raise RuntimeError("\n".join(tail) or f"yt-dlp exited with code {return_code}.")
        finally:
            with self._lock:
                try:
                    self._stop_locked()
                finally:
                    if self._job is not None:
                        self._job.close()
                    self._job = None
                    self._process = None
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()

