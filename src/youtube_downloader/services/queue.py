"""Qt queue orchestration; every transfer still belongs to DownloadWorker."""
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QTimer, Signal
from youtube_downloader.services.models import DownloadRequest, Quality, Stage
from youtube_downloader.services.txt_import import MAX_QUEUE_ITEMS, video_key
from youtube_downloader.workers.download_worker import DownloadWorker


class QueueStatus(StrEnum):
    PENDING = 'Pending'
    EXTRACTING = 'Extracting'
    DOWNLOADING = 'Downloading'
    MERGING = 'Merging'
    COMPLETED = 'Completed'
    FAILED = 'Failed'
    CANCELLED = 'Cancelled'


TERMINAL = {QueueStatus.COMPLETED, QueueStatus.FAILED, QueueStatus.CANCELLED}


@dataclass
class QueueItem:
    url: str
    title: str = ''
    quality: Quality | None = None
    status: QueueStatus = QueueStatus.PENDING
    progress: float | None = None
    path: Path | None = None
    error: str = ''

    def transition(self, status: QueueStatus) -> None:
        allowed = {
            QueueStatus.PENDING: {QueueStatus.EXTRACTING, QueueStatus.CANCELLED},
            QueueStatus.EXTRACTING: {QueueStatus.DOWNLOADING, QueueStatus.MERGING, *TERMINAL},
            QueueStatus.DOWNLOADING: {QueueStatus.MERGING, *TERMINAL},
            QueueStatus.MERGING: TERMINAL,
        }
        if status != self.status and status not in allowed.get(self.status, set()):
            raise ValueError(f'Invalid queue transition: {self.status} -> {status}')
        self.status = status


@dataclass(frozen=True)
class BatchConfig:
    quality: Quality
    folder: Path


class QueueModel(QAbstractTableModel):
    headers = ('Video / URL', 'Quality', 'Status', 'Stream progress', 'Output / Error')

    def __init__(self) -> None:
        super().__init__()
        self.items: list[QueueItem] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole}:
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return f'{item.url}\n{item.path or item.error}'
        return (item.title or item.url, str(item.quality or 'Global selection'), str(item.status),
                f'{item.progress:.1f}%' if item.progress is not None else '—', str(item.path or item.error))[index.column()]

    def changed(self, row: int) -> None:
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.headers) - 1))

    def add(self, urls: tuple[str, ...]) -> int:
        seen = {video_key(item.url) for item in self.items}
        additions = []
        for url in urls:
            key = video_key(url)
            if key not in seen:
                additions.append(QueueItem(url))
                seen.add(key)
        if len(self.items) + len(additions) > MAX_QUEUE_ITEMS:
            raise ValueError('Queue exceeds the 5,000 item limit.')
        if additions:
            self.beginInsertRows(QModelIndex(), len(self.items), len(self.items) + len(additions) - 1)
            self.items.extend(additions)
            self.endInsertRows()
        return len(additions)

    def remove_pending(self, rows: list[int]) -> None:
        for row in sorted(set(rows), reverse=True):
            if 0 <= row < len(self.items) and self.items[row].status == QueueStatus.PENDING:
                self.beginRemoveRows(QModelIndex(), row, row)
                del self.items[row]
                self.endRemoveRows()

    def clear(self) -> None:
        self.beginResetModel()
        self.items.clear()  # Queue records only; completed files are never touched.
        self.endResetModel()


class BatchController(QObject):
    worker_started = Signal(object)
    updated = Signal()
    finished = Signal(object)

    def __init__(self, model: QueueModel, factory: Callable = DownloadWorker) -> None:
        super().__init__()
        self.model, self.factory = model, factory
        self.active = False
        self.worker: DownloadWorker | None = None
        self.config: BatchConfig | None = None
        self.rows: tuple[int, ...] = ()
        self.position = 0
        self.stopping = False

    def start(self, config: BatchConfig) -> None:
        if self.active:
            return
        self.rows = tuple(i for i, item in enumerate(self.model.items) if item.status == QueueStatus.PENDING)
        if not self.rows:
            return
        self.config = BatchConfig(config.quality, config.folder.resolve())
        for row in self.rows:
            self.model.items[row].quality = config.quality
            self.model.changed(row)
        self.position, self.stopping, self.active = 0, False, True
        self._next()

    def _next(self) -> None:
        if self.stopping or self.position == len(self.rows):
            self.active = False
            self.finished.emit(self.summary())
            return
        row = self.rows[self.position]
        item = self.model.items[row]
        item.transition(QueueStatus.EXTRACTING)
        self.model.changed(row)
        assert self.config is not None
        try:
            worker = self.factory(DownloadRequest(item.url, self.config.folder, self.config.quality))
        except Exception as exc:
            item.transition(QueueStatus.FAILED)
            item.error = f'Could not start download: {exc}'
            self.position += 1
            self.model.changed(row)
            self.updated.emit()
            QTimer.singleShot(0, self._next)
            return
        self.worker = worker
        worker.stage_changed.connect(self._stage)
        worker.metadata_ready.connect(self._metadata)
        worker.progress_changed.connect(self._progress)
        worker.completed.connect(self._completed)
        worker.failed.connect(self._failed)
        worker.cancelled.connect(self._cancelled)
        worker.preview_completed.connect(lambda: self._failed('Batch requires FFmpeg; metadata preview is not a download.'))
        worker.finished.connect(self._worker_finished)
        self.worker_started.emit(worker)
        self.updated.emit()
        worker.start()

    def _item(self) -> QueueItem:
        return self.model.items[self.rows[self.position]]

    def _changed(self) -> None:
        self.model.changed(self.rows[self.position])
        self.updated.emit()

    def _stage(self, stage: str) -> None:
        status = {str(Stage.EXTRACTING): QueueStatus.EXTRACTING,
                  str(Stage.DOWNLOADING): QueueStatus.DOWNLOADING,
                  str(Stage.MERGING): QueueStatus.MERGING}.get(stage)
        if status is not None and self._item().status not in TERMINAL:
            self._item().transition(status)
            self._changed()

    def _metadata(self, metadata) -> None:
        self._item().title = metadata.title
        self._changed()

    def _progress(self, progress) -> None:
        self._item().progress = progress.percent
        self._changed()

    def _completed(self, result) -> None:
        self._item().transition(QueueStatus.COMPLETED)
        self._item().path, self._item().progress = result.path, 100.0
        self._changed()

    def _failed(self, message: str) -> None:
        self._item().transition(QueueStatus.FAILED)
        self._item().error = message
        self._changed()

    def _cancelled(self) -> None:
        self._item().transition(QueueStatus.CANCELLED)
        self._changed()

    def _worker_finished(self) -> None:
        worker, self.worker = self.worker, None
        if self._item().status not in TERMINAL:
            self._failed('Worker stopped without a completed file.')
        if worker is not None:
            worker.deleteLater()
        self.position += 1
        QTimer.singleShot(0, self._next)  # Next job only after QThread.finished / cleanup.

    def cancel(self) -> None:
        if not self.active:
            return
        self.stopping = True
        for row in self.rows:
            if self.model.items[row].status == QueueStatus.PENDING:
                self.model.items[row].transition(QueueStatus.CANCELLED)
                self.model.changed(row)
        if self.worker is not None:
            self.worker.request_cancel()
        self.updated.emit()

    def summary(self) -> dict:
        statuses = [self.model.items[row].status for row in self.rows]
        return {'total': len(statuses), 'completed': statuses.count(QueueStatus.COMPLETED),
                'failed': statuses.count(QueueStatus.FAILED), 'cancelled': statuses.count(QueueStatus.CANCELLED),
                'remaining': sum(status not in TERMINAL for status in statuses)}
