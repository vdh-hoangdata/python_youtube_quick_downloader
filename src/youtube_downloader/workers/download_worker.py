import logging

from PySide6.QtCore import QThread, Signal

from youtube_downloader.services.downloader import Downloader
from youtube_downloader.services.errors import user_error
from youtube_downloader.services.models import DownloadCancelled, DownloadRequest, Stage


logger = logging.getLogger(__name__)


class DownloadWorker(QThread):
    stage_changed = Signal(str)
    metadata_ready = Signal(object)
    progress_changed = Signal(object)
    completed = Signal(object)
    preview_completed = Signal()
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, request: DownloadRequest, downloader: Downloader | None = None) -> None:
        super().__init__()
        self.request = request
        self.downloader = downloader if downloader is not None else Downloader()
        self._stage = Stage.EXTRACTING

    def _report_stage(self, stage: Stage) -> None:
        self._stage = stage
        self.stage_changed.emit(str(stage))

    def request_cancel(self) -> None:
        # Deliberately a direct, thread-safe method: a queued slot cannot run while
        # a worker is blocked reading subprocess output.
        try:
            self.downloader.cancel()
        except OSError:
            logger.exception("Could not signal the download process to stop.")

    def run(self) -> None:
        try:
            result = self.downloader.download(
                self.request, self._report_stage,
                self.metadata_ready.emit, self.progress_changed.emit,
            )
        except DownloadCancelled:
            self.stage_changed.emit(str(Stage.CANCELLED))
            self.cancelled.emit()
        except Exception as exc:
            logger.exception("Download failed")
            self.stage_changed.emit(str(Stage.FAILED))
            self.failed.emit(user_error(exc, stage=self._stage))
        else:
            if result is None:
                self.preview_completed.emit()
            else:
                self.completed.emit(result)

