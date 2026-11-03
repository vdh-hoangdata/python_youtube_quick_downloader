from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests._support import ARTIFACTS
from tests import test_gui as gui_fixtures
from tests.test_gui import FakeDownloader
from tests.test_downloader import FakeRunner
from youtube_downloader.services.txt_import import parse_txt, video_key, MAX_TXT_BYTES, MAX_LINES
from youtube_downloader.services.queue import QueueItem, QueueStatus, BatchConfig
from youtube_downloader.services.models import Stage, Quality
from youtube_downloader.services.downloader import Downloader
from youtube_downloader.workers.download_worker import DownloadWorker
from youtube_downloader.ui.main_window import MainWindow
from PySide6.QtCore import QTimer


class ParserTests(unittest.TestCase):
    def parse(self, data: bytes, existing=frozenset()):
        with tempfile.TemporaryDirectory(dir=ARTIFACTS) as directory:
            path = Path(directory) / 'links.txt'
            path.write_bytes(data)
            return parse_txt(path, existing)

    def test_utf8_bom_crlf_blanks_order_and_id_deduplication(self):
        result = self.parse(('\ufeff  https://youtu.be/abc123\r\n\r\n'
                             'https://youtube.com/watch?v=abc123&t=3\n'
                             'https://youtube.com/shorts/def456\n').encode('utf-8'))
        self.assertEqual([video_key(url) for url in result.urls], ['abc123', 'def456'])
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.invalid, ())

    def test_invalid_lines_do_not_stop_import(self):
        result = self.parse(b'bad\nhttps://youtu.be/ok\nhttps://youtube.com/playlist?list=x\n'
                            b'https://youtube.com/@channel\nhttps://youtu.be/x,https://youtu.be/y\n')
        self.assertEqual(result.urls, ('https://youtu.be/ok',))
        self.assertEqual(len(result.invalid), 4)
        self.assertIn('Line 1:', result.invalid[0])

    def test_single_and_empty_txt(self):
        self.assertEqual(len(self.parse(b'https://youtu.be/abc').urls), 1)
        self.assertEqual(self.parse(b' \n\r\n').urls, ())

    def test_existing_queue_deduplication(self):
        result = self.parse(b'https://youtube.com/embed/abc\nhttps://youtu.be/new', frozenset({'abc'}))
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(len(result.urls), 1)

    def test_limits_and_bad_encoding(self):
        for data in (b'x' * (MAX_TXT_BYTES + 1), b'\n' * (MAX_LINES + 1), b'\xff'):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                self.parse(data)

    def test_queue_limit_is_atomic(self):
        with self.assertRaisesRegex(ValueError, 'Queue'):
            self.parse(b'https://youtu.be/new', frozenset(str(i) for i in range(5000)))

    def test_single_video_url_variants(self):
        for url in ('https://youtube.com/watch?v=abc&list=p', 'https://youtu.be/abc?t=2',
                    'https://music.youtube.com/watch?v=abc', 'https://youtube.com/live/abc',
                    'https://youtube-nocookie.com/embed/abc'):
            self.assertEqual(video_key(url), 'abc')

    def test_terminal_state_cannot_be_overwritten(self):
        item = QueueItem('https://youtu.be/abc')
        item.transition(QueueStatus.EXTRACTING)
        item.transition(QueueStatus.COMPLETED)
        with self.assertRaises(ValueError):
            item.transition(QueueStatus.CANCELLED)


class BatchGuiTests(unittest.TestCase):
    setUpClass = classmethod(gui_fixtures.GuiTests.setUpClass.__func__)
    setUp = gui_fixtures.GuiTests.setUp
    tearDown = gui_fixtures.GuiTests.tearDown
    wait_for = gui_fixtures.GuiTests.wait_for

    def make_window(self, factory=None):
        window = MainWindow(self.settings, factory or (lambda request: DownloadWorker(request, FakeDownloader())))
        self.windows.append(window)
        window.show()
        return window

    def test_background_import_does_not_download_and_reports_invalid_lines(self):
        window = self.make_window(lambda request: self.fail('Import must not start a download'))
        path = self.folder / 'links.txt'
        path.write_text('https://youtu.be/first\nhttps://youtu.be/second\ninvalid\n', encoding='utf-8-sig')
        window.import_file(path)
        self.wait_for(lambda: window.import_worker is None)
        self.assertEqual([item.url for item in window.queue_model.items], ['https://youtu.be/first', 'https://youtu.be/second'])
        self.assertIn('Invalid: 1', window.import_report.toPlainText())
        self.assertIsNone(window.worker)

    def test_sequential_failure_isolation_collision_and_snapshot(self):
        requests, active = [], []

        class TrackedDownloader(Downloader):
            def download(backend, request, *callbacks):
                self.assertEqual(active, [])
                active.append(request.url)
                try:
                    return super().download(request, *callbacks)
                finally:
                    active.clear()

        def factory(request):
            requests.append(request)
            error = RuntimeError('Connection failed') if len(requests) == 2 else None
            return DownloadWorker(request, TrackedDownloader(FakeRunner(error=error)))

        window = self.make_window(factory)
        window.queue_model.add(tuple(f'https://youtu.be/{i}' for i in range(3)))
        window.quality_combo.setCurrentIndex(2)
        with patch('youtube_downloader.services.downloader.verify_ffmpeg'), \
             self.assertLogs('youtube_downloader.workers.download_worker', level='ERROR'):
            window.start_batch()
            window.quality_combo.setCurrentIndex(0)
            window.folder = self.folder / 'different'
            window.start_download()  # Cannot overlap with batch.
            window.start_batch()
            self.wait_for(lambda: not window.batch.active)
        self.assertEqual([item.status for item in window.queue_model.items],
                         [QueueStatus.COMPLETED, QueueStatus.FAILED, QueueStatus.COMPLETED])
        self.assertEqual([request.url for request in requests], [f'https://youtu.be/{i}' for i in range(3)])
        self.assertTrue(all(request.quality == Quality.MAX_720 and request.folder == self.folder.resolve() for request in requests))
        paths = [item.path for item in window.queue_model.items if item.path]
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(path.exists() for path in paths))
        self.assertEqual(window.batch.summary(), dict(total=3, completed=2, failed=1, cancelled=0, remaining=0))
        self.assertTrue(window.queue_model.items[1].error)
        window.clear_queue()
        self.assertEqual(window.queue_model.rowCount(), 0)
        self.assertTrue(all(path.exists() for path in paths))

    def test_cancel_each_stage_stops_pending_and_preserves_completed(self):
        for stage in (Stage.EXTRACTING, Stage.DOWNLOADING, Stage.MERGING):
            with self.subTest(stage=stage):
                calls = []
                def factory(request):
                    calls.append(request)
                    return DownloadWorker(request, FakeDownloader(wait_at=stage if len(calls) == 2 else None))
                window = self.make_window(factory)
                window.queue_model.add(tuple(f'https://youtu.be/{i}' for i in range(3)))
                window.start_batch()
                self.wait_for(lambda: len(calls) == 2 and window.current_label.text() == str(stage))
                completed = window.queue_model.items[0].path
                window.cancel_batch()
                self.wait_for(lambda: not window.batch.active)
                self.assertEqual(len(calls), 2)
                self.assertEqual([item.status for item in window.queue_model.items],
                                 [QueueStatus.COMPLETED, QueueStatus.CANCELLED, QueueStatus.CANCELLED])
                self.assertTrue(completed.exists())

    def test_close_during_batch_stops_without_starting_next(self):
        window = self.make_window(lambda request: DownloadWorker(request, FakeDownloader(wait_at=Stage.MERGING)))
        window.queue_model.add(('https://youtu.be/first', 'https://youtu.be/second'))
        window.start_batch()
        self.wait_for(lambda: window.current_label.text() == str(Stage.MERGING))
        window.close()
        self.wait_for(lambda: not window.batch.active)
        self.assertFalse(window.isVisible())
        self.assertEqual(window.batch.summary()['cancelled'], 2)
        self.assertIsNone(window.batch.worker)

    def test_ffmpeg_missing_blocks_batch(self):
        window = self.make_window(lambda request: self.fail('FFmpeg missing must block batch'))
        window.queue_model.add(('https://youtu.be/first',))
        with patch('youtube_downloader.ui.main_window.find_ffmpeg', return_value=None):
            window.start_batch()
        self.assertFalse(window.batch.active)
        self.assertEqual(window.queue_model.items[0].status, QueueStatus.PENDING)
        self.assertIn('Check again', window.message_label.text())

    def test_remove_pending_and_import_dedup(self):
        window = self.make_window()
        window.queue_model.add(('https://youtu.be/first', 'https://youtube.com/watch?v=first', 'https://youtu.be/second'))
        self.assertEqual(window.queue_model.rowCount(), 2)
        window.queue_view.selectRow(0)
        window.remove_pending()
        self.assertEqual(window.queue_model.items[0].url, 'https://youtu.be/second')

    def test_batch_event_loop_and_edit_guards(self):
        window = self.make_window()
        window.queue_model.add(('https://youtu.be/first', 'https://youtu.be/second'))
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        window.start_batch()
        window.clear_queue()
        window.queue_view.selectRow(1)
        window.remove_pending()
        self.assertEqual(window.queue_model.rowCount(), 2)
        self.assertFalse(window.import_button.isEnabled())
        self.wait_for(lambda: not window.batch.active)
        timer.stop()
        self.assertGreater(len(ticks), 3)
