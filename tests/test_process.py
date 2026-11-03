import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests._support import ARTIFACTS

from youtube_downloader.services.downloader import Downloader
from youtube_downloader.services.models import DownloadCancelled, DownloadRequest
from youtube_downloader.services.process import ProcessRunner


class ProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows Job Object startup")
    def test_failed_job_assignment_stops_gated_process_promptly(self) -> None:
        processes = []
        popen = subprocess.Popen

        def capture_process(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process

        class FailedJob:
            def assign(self, process):
                raise OSError("test assignment failure")

            def close(self):
                pass

        runner = ProcessRunner()
        command = [sys.executable, "-u", str(Path(__file__).resolve().parents[1] / "src/youtube_downloader/services/cli.py"), "--version"]
        started = time.monotonic()
        with patch("youtube_downloader.services.process.WindowsJob", FailedJob), patch("youtube_downloader.services.process.subprocess.Popen", side_effect=capture_process):
            with self.assertRaisesRegex(OSError, "assignment failure"):
                runner.run(command, lambda line: None, threading.Event(), ARTIFACTS)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNotNone(processes[0].poll())

    def test_real_installed_cli_runs_with_owned_process(self) -> None:
        runner = ProcessRunner()
        lines = []
        runner.run([
            sys.executable, "-u", str(Path(__file__).resolve().parents[1] / "src/youtube_downloader/services/cli.py"),
            "--ignore-config", "--no-plugin-dirs", "--version",
        ], lines.append, threading.Event(), ARTIFACTS)
        self.assertTrue(any(line.startswith("2026.") for line in lines))

    @unittest.skipUnless(os.name == "nt", "Windows Job Object integration")
    def test_cancel_stops_owned_child_tree_and_preserves_unrelated_process(self) -> None:
        with tempfile.TemporaryDirectory(dir=ARTIFACTS) as directory:
            folder = Path(directory)
            helper = folder / "owned_helper.py"
            helper.write_text(
                "import subprocess, sys, time\n"
                "sys.stdin.readline()\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                "print(child.pid, flush=True)\n"
                "time.sleep(30)\n", encoding="utf-8",
            )
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                         creationflags=subprocess.CREATE_NO_WINDOW)
            runner = ProcessRunner()
            cancelled = threading.Event()
            ready = threading.Event()
            errors = []
            child_pid = []

            def on_line(line: str) -> None:
                child_pid.append(int(line))
                ready.set()

            def execute() -> None:
                try:
                    runner.run([sys.executable, "-u", str(helper)], on_line, cancelled, folder)
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=execute, daemon=True)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = None
            try:
                thread.start()
                self.assertTrue(ready.wait(5), "Owned child process did not start")
                handle = kernel.OpenProcess(0x00100000, False, child_pid[0])  # SYNCHRONIZE
                self.assertTrue(handle)
                cancelled.set()
                runner.cancel()
                thread.join(5)
                self.assertFalse(thread.is_alive(), "Cancel did not unblock the worker")
                self.assertTrue(any(isinstance(error, DownloadCancelled) for error in errors))
                self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0, "Owned child survived cancellation")
                self.assertIsNone(unrelated.poll(), "An unrelated process was stopped")
            finally:
                cancelled.set()
                runner.cancel()
                thread.join(5)
                if handle:
                    kernel.CloseHandle(handle)
                unrelated.kill()
                unrelated.wait(timeout=5)

    def test_real_ytdlp_downloads_local_fixture_and_emits_progress(self) -> None:
        """Exercise real CLI templates and final path, entirely from a local file.

        File URLs are enabled ONLY for this test. Production does not allow them.
        The fixture bytes test transfer; they are not playable media.
        """
        with tempfile.TemporaryDirectory(dir=ARTIFACTS) as directory:
            folder = Path(directory)
            source = folder / "source.mp4"
            source.write_bytes(b"offline test bytes" * 4096)
            destination = folder / "downloads"
            fmt = {
                "format_id": "fixture", "height": 360, "width": 640, "ext": "mp4",
                "vcodec": "avc1", "acodec": "mp4a", "url": source.as_uri(),
                "protocol": "file", "filesize": source.stat().st_size,
            }
            info = {
                **fmt, "id": "offline123", "title": "Offline fixture",
                "extractor": "youtube", "extractor_key": "Youtube", "duration": 1,
                "webpage_url": "https://www.youtube.com/watch?v=offline123", "formats": [fmt],
            }

            class LocalFixtureRunner(ProcessRunner):
                def run(self, command, on_line, cancelled, cwd) -> None:
                    if "--dump-single-json" in command:
                        on_line(json.dumps(info))
                    else:
                        super().run([*command, "--enable-file-urls"], on_line, cancelled, cwd)

            progresses = []
            stages = []
            result = Downloader(LocalFixtureRunner()).download(
                DownloadRequest("https://youtu.be/offline123", destination),
                stages.append, lambda metadata: None, progresses.append,
            )
            self.assertEqual(result.path.read_bytes(), source.read_bytes())
            self.assertTrue(progresses, "The real yt-dlp CLI did not emit parsed progress")
            self.assertEqual(progresses[-1].percent, 100)
            self.assertFalse(any(path.name.startswith(".youtube-local-") for path in destination.iterdir()))


if __name__ == "__main__":
    unittest.main()

