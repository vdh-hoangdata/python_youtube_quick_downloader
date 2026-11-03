from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests._support import ARTIFACTS

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from youtube_downloader.services.models import (
    DownloadCancelled, DownloadError, DownloadProgress, DownloadResult, Stage, VideoMetadata,
)
from youtube_downloader.services.ffmpeg import MISSING_FFMPEG
from youtube_downloader.services.downloader import Downloader
from youtube_downloader.services.quality import format_selector
from tests.test_downloader import FakeRunner
from youtube_downloader.ui.main_window import MainWindow
from youtube_downloader.utils.settings import AppSettings
from youtube_downloader.workers.download_worker import DownloadWorker


class FakeDownloader:
    def __init__(self, *, wait_at: Stage | None = None, error: Exception | None = None) -> None:
        self.event = threading.Event()
        self.wait_at = wait_at
        self.error = error

    def cancel(self) -> None:
        self.event.set()

    def download(self, request, on_stage, on_metadata, on_progress):
        for stage in (Stage.EXTRACTING, Stage.DOWNLOADING, Stage.MERGING):
            on_stage(stage)
            if self.event.wait(3 if self.wait_at == stage else 0.06):
                raise DownloadCancelled()
            if stage == Stage.EXTRACTING:
                if self.error:
                    raise self.error
                on_metadata(VideoMetadata("<b>Plain title</b>", "Channel", 90, 1080, 2160, 200, False))
                if request.preview_only:
                    on_stage(Stage.PREVIEWED)
                    return None
            if stage == Stage.DOWNLOADING:
                on_progress(DownloadProgress("Downloading video", 73.4, 734, 1000, 1024, 18))
        path = request.folder / "finished.mp4"
        path.write_bytes(b"completed")
        on_stage(Stage.COMPLETED)
        return DownloadResult(path, "Plain title")


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=ARTIFACTS)
        self.folder = Path(self.temporary.name)
        self.settings = AppSettings(QSettings(str(self.folder / "settings.ini"), QSettings.Format.IniFormat))
        self.settings.save_folder(self.folder)
        self.windows = []
        self.ffmpeg_patch = patch("youtube_downloader.ui.main_window.find_ffmpeg", return_value=self.folder / "ffmpeg.exe")
        self.ffmpeg_patch.start()

    def tearDown(self) -> None:
        for window in self.windows:
            if window.worker is not None:
                window.cancel_download()
                self.wait_for(lambda: window.worker is None)
            window.close()
            window.deleteLater()
        self.app.processEvents()
        self.ffmpeg_patch.stop()
        self.temporary.cleanup()

    def wait_for(self, condition, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(10)
            # qWait processes Qt events but can retain the GIL; let Python workers run.
            time.sleep(0.001)
        self.assertTrue(condition(), "Timed out waiting for the GUI state")

    def window(self, downloader: FakeDownloader | None = None) -> MainWindow:
        backend = downloader if downloader is not None else FakeDownloader()
        window = MainWindow(self.settings, lambda request: DownloadWorker(request, backend))
        window.show()
        self.windows.append(window)
        return window

    def start(self, window: MainWindow) -> None:
        window.url_input.setText("https://youtu.be/abc123")
        window.start_download()

    def test_idle_window_and_invalid_url_do_not_start_worker(self) -> None:
        window = self.window()
        self.assertEqual(window.status_label.text(), "Ready")
        self.assertFalse(window.cancel_button.isEnabled())
        window.url_input.setText("https://example.com/video")
        window.start_download()
        self.assertIsNone(window.worker)
        self.assertTrue(window.download_button.isEnabled())
        self.assertTrue(window.message_label.isVisible())

    def test_gui_remains_responsive_and_completion_returns_idle_controls(self) -> None:
        window = self.window()
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        self.start(window)
        self.assertFalse(window.download_button.isEnabled())
        self.assertTrue(window.cancel_button.isEnabled())
        self.wait_for(lambda: window.worker is None)
        timer.stop()
        self.assertGreater(len(ticks), 3)
        self.assertEqual(window.status_label.text(), "Completed")
        self.assertEqual(window.progress_bar.value(), 1000)
        self.assertTrue(window.download_button.isEnabled())
        self.assertFalse(window.cancel_button.isEnabled())
        self.assertEqual(window.title_label.text(), "<b>Plain title</b>")
        self.assertIn("MP4", window.message_label.text())

    def test_cancel_during_extraction_download_and_merging(self) -> None:
        for stage in (Stage.EXTRACTING, Stage.DOWNLOADING, Stage.MERGING):
            with self.subTest(stage=stage):
                backend = FakeDownloader(wait_at=stage)
                window = self.window(backend)
                self.start(window)
                self.wait_for(lambda: window.current_label.text() == str(stage))
                window.cancel_download()
                self.wait_for(lambda: window.worker is None)
                self.assertTrue(backend.event.is_set())
                self.assertEqual(window.status_label.text(), "Cancelled")
                self.assertTrue(window.download_button.isEnabled())
                self.assertFalse(window.cancel_button.isEnabled())
                self.assertEqual(window.progress_bar.value(), 0)

    def test_missing_ffmpeg_is_visible_and_logged_without_traceback_in_ui(self) -> None:
        window = self.window(FakeDownloader(error=DownloadError(MISSING_FFMPEG)))
        with self.assertLogs("youtube_downloader.workers.download_worker", level="ERROR"):
            self.start(window)
            self.wait_for(lambda: window.worker is None)
        self.assertEqual(window.message_label.text(), MISSING_FFMPEG)
        self.assertNotIn("Traceback", window.message_label.text())
        self.assertTrue(window.download_button.isEnabled())

    def test_folder_and_window_geometry_persist(self) -> None:
        selected = self.folder / "chosen"
        selected.mkdir()
        first = self.window()
        first.set_folder(selected)
        first.resize(720, 740)
        first.close()
        fresh = AppSettings(QSettings(str(self.folder / "settings.ini"), QSettings.Format.IniFormat))
        second = MainWindow(fresh)
        self.windows.append(second)
        self.assertEqual(second.folder, selected.resolve())
        self.assertEqual(second.size(), first.size())

    def test_close_active_window_cancels_and_waits_for_worker(self) -> None:
        window = self.window(FakeDownloader(wait_at=Stage.MERGING))
        self.start(window)
        self.wait_for(lambda: window.current_label.text() == str(Stage.MERGING))
        window.close()
        self.wait_for(lambda: window.worker is None)
        self.assertFalse(window.isVisible())

    def test_progress_updates_speed_eta_and_exact_percent(self) -> None:
        window = self.window()
        window._on_progress(DownloadProgress("Downloading audio", 73.4, 734, 1000, 1024, 18))
        self.assertEqual(window.current_label.text(), "Downloading audio")
        self.assertEqual(window.progress_bar.value(), 734)
        self.assertEqual(window.progress_bar.format(), "73.4%")
        self.assertIn("1.0 KB/s", window.stats_label.text())
        self.assertIn("00:18", window.stats_label.text())

    def test_missing_ffmpeg_status_is_shown_at_startup_and_preview_stays_available(self) -> None:
        with patch("youtube_downloader.ui.main_window.find_ffmpeg", return_value=None):
            window = self.window()
            self.assertEqual(window.ffmpeg_label.text(), "FFmpeg: Not found — required for high-quality video/audio merging.")
            self.assertLess(window.ffmpeg_label.y(), window.quality_combo.y())
            self.assertTrue(window.download_button.isEnabled())
            self.assertEqual(window.download_button.text(), "Preview metadata")
            self.start(window)
            self.assertTrue(window.worker.request.preview_only)
            self.assertIn("Only metadata", window.message_label.text())
            self.wait_for(lambda: window.worker is None)
            self.assertEqual(window.status_label.text(), "Metadata ready")
            self.assertEqual(window.title_label.text(), "<b>Plain title</b>")
            self.assertFalse((self.folder / "finished.mp4").exists())
            self.assertTrue(window.check_ffmpeg_button.isEnabled())

    def test_check_again_enables_best_download_without_restarting_or_losing_preview(self) -> None:
        runners = []

        def factory(request):
            runner = FakeRunner()
            runners.append(runner)
            return DownloadWorker(request, Downloader(runner))

        with patch("youtube_downloader.ui.main_window.find_ffmpeg", return_value=None) as detected, \
             patch("youtube_downloader.services.downloader.find_ffmpeg", return_value=None) as service_detected, \
             patch("youtube_downloader.services.downloader.verify_ffmpeg"):
            window = MainWindow(self.settings, factory)
            window.show()
            self.windows.append(window)
            self.start(window)
            self.wait_for(lambda: window.worker is None)
            self.assertEqual(window.status_label.text(), "Metadata ready")
            self.assertEqual(len(runners[0].commands), 1)
            self.assertIn("2160p", window.resolution_label.text())
            path = self.folder / "ffmpeg.exe"
            detected.return_value = service_detected.return_value = path
            window.check_ffmpeg_button.click()
            self.assertEqual(window.ffmpeg_label.text(), "FFmpeg: Ready")
            self.assertEqual(window.ffmpeg_label.toolTip(), str(path))
            self.assertEqual(window.download_button.text(), "Download")
            self.assertEqual(window.title_label.text(), "Example video")
            self.start(window)
            self.assertFalse(window.worker.request.preview_only)
            self.wait_for(lambda: window.worker is None)
            self.assertEqual(window.status_label.text(), "Completed")
            self.assertEqual(len(runners[1].commands), 2)
            command = runners[1].commands[1]
            self.assertEqual(command[command.index("--format") + 1], format_selector(window.quality_combo.currentData()))
            self.assertEqual(command[command.index("--ffmpeg-location") + 1], str(path))

    def test_recheck_removes_stale_ready_state(self) -> None:
        window = self.window()
        self.assertEqual(window.ffmpeg_label.text(), "FFmpeg: Ready")
        with patch("youtube_downloader.ui.main_window.find_ffmpeg", return_value=None):
            window.check_ffmpeg_button.click()
            self.assertIsNone(window.ffmpeg_path)
            self.assertEqual(window.download_button.text(), "Preview metadata")
            self.assertNotEqual(window.ffmpeg_label.toolTip(), str(self.folder / "ffmpeg.exe"))

    def test_worker_distinguishes_extraction_and_merge_failure(self) -> None:
        for backend, expected in (
            (FakeDownloader(error=RuntimeError("Unable to extract player response")), "yt-dlp extraction failed"),
            (FakeDownloader(wait_at=Stage.MERGING), "Merge/post-processing failed"),
        ):
            if backend.wait_at == Stage.MERGING:
                def merge_failure(request, on_stage, on_metadata, on_progress):
                    on_stage(Stage.MERGING)
                    raise RuntimeError("processing crashed")
                backend.download = merge_failure
            with self.subTest(expected=expected):
                window = self.window(backend)
                with self.assertLogs("youtube_downloader.workers.download_worker", level="ERROR"):
                    self.start(window)
                    self.wait_for(lambda: window.worker is None)
                self.assertIn(expected, window.message_label.text())


if __name__ == "__main__":
    unittest.main()

