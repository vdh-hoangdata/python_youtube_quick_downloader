from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QCloseEvent, QGuiApplication
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QProgressBar, QPushButton, QVBoxLayout, QWidget,
    QTableView, QAbstractItemView, QPlainTextEdit, QScrollArea,
)

from youtube_downloader.services.ffmpeg import MISSING_FFMPEG, find_ffmpeg
from youtube_downloader.services.models import (
    DownloadProgress, DownloadRequest, DownloadResult, Quality, Stage, VideoMetadata,
)
from youtube_downloader.utils.formatting import format_bytes, format_time
from youtube_downloader.utils.settings import AppSettings
from youtube_downloader.utils.urls import normalize_url
from youtube_downloader.workers.download_worker import DownloadWorker
from youtube_downloader.workers.import_worker import ImportWorker
from youtube_downloader.services.txt_import import ImportResult, video_key
from youtube_downloader.services.queue import BatchConfig, BatchController, QueueModel
from youtube_downloader.ui.theme import light_palette


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings: AppSettings | None = None,
        worker_factory: Callable[[DownloadRequest], DownloadWorker] = DownloadWorker,
    ) -> None:
        super().__init__()
        self.setPalette(light_palette())
        self.settings = settings if settings is not None else AppSettings()
        self.worker_factory = worker_factory
        self.worker: DownloadWorker | None = None
        self.ffmpeg_path: Path | None = None
        self.import_worker: ImportWorker | None = None
        self.queue_model = QueueModel()
        self.batch = BatchController(self.queue_model, worker_factory)
        self.batch.worker_started.connect(self._batch_worker_started)
        self.batch.updated.connect(self._batch_updated)
        self.batch.finished.connect(self._batch_finished)
        self.folder = self.settings.download_folder()
        self._close_when_idle = False
        self.setWindowTitle("YouTube Local Downloader")
        self.setMinimumSize(520, 600)
        self.resize(640, 680)
        self._build_ui()
        geometry = self.settings.window_geometry()
        if not geometry.isEmpty():
            self.restoreGeometry(geometry)
            screens = QGuiApplication.screens()
            if screens and not any(screen.availableGeometry().intersects(self.frameGeometry()) for screen in screens):
                self.move(screens[0].availableGeometry().center() - self.rect().center())

    @staticmethod
    def _label(text: str = "") -> QLabel:
        label = QLabel(text)
        # Titles and filenames are untrusted text, never HTML or active links.
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def _build_ui(self) -> None:
        body = QWidget()
        body.setObjectName('content')
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        self.setCentralWidget(scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        heading = self._label("YOUTUBE LOCAL DOWNLOADER")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        subtitle = self._label("One video. Saved to your computer.")
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste YouTube URL here")
        self.url_input.setAccessibleName("YouTube video URL")
        self.url_input.setClearButtonEnabled(True)
        self.url_input.returnPressed.connect(self.start_download)
        layout.addWidget(self.url_input)
        buttons = QHBoxLayout()
        self.download_button = QPushButton("Download")
        self.download_button.setObjectName("downloadButton")
        self.download_button.clicked.connect(self.start_download)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_download)
        buttons.addWidget(self.download_button, 2)
        buttons.addWidget(self.cancel_button, 1)
        layout.addLayout(buttons)

        ffmpeg_row = QHBoxLayout()
        self.ffmpeg_label = self._label()
        self.ffmpeg_label.setObjectName("muted")
        self.check_ffmpeg_button = QPushButton("Check again")
        self.check_ffmpeg_button.setAccessibleName("Check again for FFmpeg")
        self.check_ffmpeg_button.clicked.connect(self.refresh_ffmpeg)
        ffmpeg_row.addWidget(self.ffmpeg_label, 1)
        ffmpeg_row.addWidget(self.check_ffmpeg_button)
        layout.addLayout(ffmpeg_row)

        self.quality_combo = QComboBox()
        for quality in Quality:
            self.quality_combo.addItem(str(quality), quality)
        self.quality_combo.setAccessibleName("Quality preference")
        options = QFormLayout()
        options.setSpacing(12)
        options.addRow("Quality:", self.quality_combo)
        folder_row = QHBoxLayout()
        self.folder_label = self._label(str(self.folder))
        self.folder_label.setToolTip(str(self.folder))
        self.folder_label.setMinimumWidth(100)
        self.folder_label.setMaximumHeight(60)
        self.choose_folder_button = QPushButton("Choose Folder")
        self.choose_folder_button.clicked.connect(self.choose_folder)
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(self.choose_folder_button)
        options.addRow("Save to:", folder_row)
        layout.addLayout(options)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(separator)
        self.status_label = self._label("Ready")
        self.status_label.setObjectName("status")
        status_row = QHBoxLayout()
        status_row.addWidget(self._label("Status:"))
        status_row.addWidget(self.status_label, 1)
        layout.addLayout(status_row)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0.0%")
        self.progress_bar.setAccessibleName("Current stream download progress")
        layout.addWidget(self.progress_bar)
        self.stats_label = self._label("Speed --  |  ETA --:--")
        self.stats_label.setObjectName("muted")
        layout.addWidget(self.stats_label)
        self.current_label = self._label("Paste a video link to begin.")
        layout.addWidget(self.current_label)

        self.title_label = self._label("--")
        self.title_label.setMaximumHeight(70)
        self.uploader_label = self._label("--")
        self.duration_label = self._label("--")
        self.resolution_label = self._label("--")
        self.size_label = self._label("--")
        details = QFormLayout()
        details.addRow("Title:", self.title_label)
        details.addRow("Channel:", self.uploader_label)
        details.addRow("Duration:", self.duration_label)
        details.addRow("Resolution:", self.resolution_label)
        details.addRow("File size:", self.size_label)
        layout.addLayout(details)
        layout.addStretch()
        self.message_label = self._label()
        self.message_label.setObjectName("message")
        self.message_label.hide()
        layout.addWidget(self.message_label)
        queue_buttons = QHBoxLayout()
        self.import_button = QPushButton('Import TXT')
        self.download_all_button = QPushButton('Download All')
        self.cancel_batch_button = QPushButton('Cancel Batch')
        self.remove_pending_button = QPushButton('Remove Pending')
        self.clear_queue_button = QPushButton('Clear Queue')
        for button, action in (
            (self.import_button, self.import_txt), (self.download_all_button, self.start_batch),
            (self.cancel_batch_button, self.cancel_batch), (self.remove_pending_button, self.remove_pending),
            (self.clear_queue_button, self.clear_queue),
        ):
            button.clicked.connect(action)
            queue_buttons.addWidget(button)
        self.cancel_batch_button.setEnabled(False)
        layout.insertLayout(5, queue_buttons)
        self.queue_view = QTableView()
        self.queue_view.setModel(self.queue_model)
        self.queue_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue_view.setMinimumHeight(130)
        self.queue_view.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.queue_view)
        self.batch_label = self._label('Queue: 0 videos')
        layout.addWidget(self.batch_label)
        self.batch_progress = QProgressBar()
        self.batch_progress.setRange(0, 1)
        self.batch_progress.setValue(0)
        self.batch_progress.setFormat('Batch: %v / %m processed')
        layout.addWidget(self.batch_progress)
        self.import_report = QPlainTextEdit()
        self.import_report.setReadOnly(True)
        self.import_report.setMaximumHeight(85)
        self.import_report.hide()
        layout.addWidget(self.import_report)
        self.refresh_ffmpeg()
        self.setStyleSheet("""
            QMainWindow { background: #f8f9fb; }
            QWidget#content, QScrollArea { background: #f8f9fb; }
            QScrollArea { border: none; }
            QWidget { font-family: 'Segoe UI'; font-size: 10pt; color: #20242c; }
            QLabel { color: #20242c; background: transparent; }
            QLabel#heading { font-size: 16pt; font-weight: 700; }
            QLabel#muted { color: #667080; font-size: 9pt; }
            QLabel#status { font-weight: 600; }
            QLabel#message { padding: 10px; border-radius: 5px; background: #eaf0f6; color: #20242c; }
            QLineEdit, QComboBox { background: white; color: #20242c; padding: 9px; border: 1px solid #cbd2dc; border-radius: 5px; selection-background-color: #dbeafe; selection-color: #17365d; }
            QLineEdit:disabled, QComboBox:disabled { background: #e9ecf1; color: #586270; }
            QComboBox QAbstractItemView { background: white; color: #20242c; selection-background-color: #dbeafe; selection-color: #17365d; }
            QLineEdit:focus { border: 1px solid #c42727; }
            QPushButton { padding: 9px 14px; background: white; color: #20242c; border: 1px solid #cbd2dc; border-radius: 5px; }
            QPushButton:hover { background: #edf0f4; }
            QPushButton:pressed { background: #e2e8f0; }
            QPushButton#downloadButton { background: #bc2727; color: white; border-color: #bc2727; font-weight: 600; }
            QPushButton#downloadButton:hover { background: #a22020; }
            QPushButton:disabled, QPushButton#downloadButton:disabled { color: #586270; background: #e9ecf1; border-color: #dce1e8; }
            QTableView { background: white; alternate-background-color: #f1f5f9; color: #20242c; gridline-color: #d5dbe4; border: 1px solid #cbd2dc; selection-background-color: #dbeafe; selection-color: #17365d; }
            QTableView::item:selected { background: #dbeafe; color: #17365d; }
            QTableView::item:selected:!active { background: #e2e8f0; color: #20242c; }
            QHeaderView, QHeaderView::section, QTableCornerButton::section { background: #eef2f7; color: #20242c; }
            QPlainTextEdit { background: white; color: #20242c; border: 1px solid #cbd2dc; selection-background-color: #dbeafe; selection-color: #17365d; }
            QToolTip { background: white; color: #20242c; border: 1px solid #cbd2dc; }
            QProgressBar { border: 1px solid #d5dbe4; background: white; color: #20242c; border-radius: 5px; text-align: center; min-height: 22px; }
            QProgressBar::chunk { background: #da8a8a; border-radius: 4px; }
        """)

    def set_folder(self, folder: Path) -> None:
        self.folder = folder.resolve()
        self.folder_label.setText(str(self.folder))
        self.folder_label.setToolTip(str(self.folder))
        self.settings.save_folder(self.folder)

    @Slot()
    def choose_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose download folder", str(self.folder))
        if selected:
            self.set_folder(Path(selected))

    def _set_busy(self, busy: bool) -> None:
        for widget in (self.url_input, self.quality_combo, self.choose_folder_button, self.download_button, self.check_ffmpeg_button):
            widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        for widget in (self.import_button, self.download_all_button, self.remove_pending_button, self.clear_queue_button):
            widget.setEnabled(not busy)

    @Slot()
    def refresh_ffmpeg(self) -> None:
        self.ffmpeg_path = find_ffmpeg()
        if self.ffmpeg_path is not None:
            self.ffmpeg_label.setText("FFmpeg: Ready")
            self.ffmpeg_label.setToolTip(str(self.ffmpeg_path))
            self.download_button.setText("Download")
            self.download_button.setToolTip("Download the selected video quality to your computer.")
        else:
            self.ffmpeg_label.setText("FFmpeg: Not found — required for high-quality video/audio merging.")
            self.ffmpeg_label.setToolTip("Install FFmpeg using the README instructions, then click Check again.")
            self.download_button.setText("Preview metadata")
            self.download_button.setToolTip("Metadata preview is available. Install FFmpeg and click Check again to enable downloads.")

    @Slot()
    def start_download(self) -> None:
        if self.worker is not None or self.batch.active or self.import_worker is not None:
            return
        try:
            url = normalize_url(self.url_input.text())
        except ValueError as exc:
            self.status_label.setText("Invalid URL")
            self.message_label.setText(str(exc))
            self.message_label.show()
            self.url_input.setFocus()
            return
        self.url_input.setText(url)
        self.message_label.clear()
        self.message_label.hide()
        self.refresh_ffmpeg()
        preview_only = self.ffmpeg_path is None
        if preview_only:
            self.message_label.setText(
                f"{MISSING_FFMPEG} Only metadata will be retrieved. "
                "Install FFmpeg, then click Check again to enable downloads."
            )
            self.message_label.show()
        for label in (self.title_label, self.uploader_label, self.duration_label, self.resolution_label, self.size_label):
            label.setText("--")
        self.stats_label.setText("Speed --  |  ETA --:--")
        self.progress_bar.setRange(0, 0)
        self.current_label.setText(str(Stage.EXTRACTING))
        self.status_label.setText("Working")
        self._set_busy(True)
        request = DownloadRequest(url, self.folder, self.quality_combo.currentData(), preview_only)
        self.worker = self.worker_factory(request)
        self.worker.stage_changed.connect(self._on_stage)
        self.worker.metadata_ready.connect(self._on_metadata)
        self.worker.progress_changed.connect(self._on_progress)
        self.worker.completed.connect(self._on_completed)
        self.worker.preview_completed.connect(self._on_previewed)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    @Slot()
    def cancel_download(self) -> None:
        if self.worker is not None:
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancelling")
            self.current_label.setText("Stopping download and cleaning temporary files...")
            self.worker.request_cancel()

    @Slot(str)
    def _on_stage(self, stage: str) -> None:
        if self.status_label.text() == "Cancelling" and stage not in {str(Stage.COMPLETED), str(Stage.CANCELLED), str(Stage.FAILED)}:
            return
        self.current_label.setText(stage)
        if stage in {str(Stage.EXTRACTING), str(Stage.MERGING), str(Stage.SAVING)}:
            self.progress_bar.setRange(0, 0)
            self.stats_label.setText("Speed --  |  ETA --:--")

    @Slot(object)
    def _on_metadata(self, metadata: VideoMetadata) -> None:
        self.title_label.setText(metadata.title)
        self.title_label.setToolTip(metadata.title)
        self.uploader_label.setText(metadata.uploader)
        self.duration_label.setText(format_time(metadata.duration))
        resolution = f"{metadata.height}p selected" if metadata.height is not None else "Unknown"
        if metadata.highest_height is not None and metadata.highest_height != metadata.height:
            resolution += f" (highest detected: {metadata.highest_height}p)"
        self.resolution_label.setText(resolution)
        size = format_bytes(metadata.size)
        if metadata.size_is_estimate and metadata.size is not None:
            size += " (estimated)"
        self.size_label.setText(size)

    @Slot(object)
    def _on_progress(self, progress: DownloadProgress) -> None:
        if self.status_label.text() == "Cancelling":
            return
        self.current_label.setText(progress.current)
        if progress.percent is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(round(progress.percent * 10))
            self.progress_bar.setFormat(f"{'~' if progress.estimated else ''}{progress.percent:.1f}%")
        speed = f"{format_bytes(progress.speed)}/s" if progress.speed is not None else "--"
        self.stats_label.setText(f"Speed {speed}  |  ETA {format_time(progress.eta)}")
        total = format_bytes(progress.total) if progress.total is not None else "unknown size"
        self.size_label.setText(f"{format_bytes(progress.downloaded)} / {total}" + (" (estimated)" if progress.estimated and progress.total else ""))

    @Slot(object)
    def _on_completed(self, result: DownloadResult) -> None:
        self.status_label.setText("Completed")
        self.current_label.setText("Completed")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(1000)
        self.progress_bar.setFormat("100.0%")
        self.stats_label.setText("Speed --  |  ETA 00:00")
        self.size_label.setText(format_bytes(result.path.stat().st_size))
        self.message_label.setText(f"{result.container_note}\n{result.path}")
        self.message_label.show()

    @Slot()
    def _on_previewed(self) -> None:
        self.status_label.setText("Metadata ready")
        self.current_label.setText("Metadata preview complete. No video or audio was downloaded.")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0.0%")
        self.stats_label.setText("Speed --  |  ETA --:--")
        self.refresh_ffmpeg()
        self.message_label.setText(
            "Click Download to save this video." if self.ffmpeg_path is not None else
            f"{MISSING_FFMPEG} Install FFmpeg, then click Check again. Your metadata preview is available."
        )
        self.message_label.show()

    @Slot()
    def _on_cancelled(self) -> None:
        self.status_label.setText("Cancelled")
        self.current_label.setText("Ready for another download.")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0.0%")
        self.stats_label.setText("Speed --  |  ETA --:--")

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.status_label.setText("Download failed")
        self.current_label.setText("Ready to try again.")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0.0%")
        self.stats_label.setText("Speed --  |  ETA --:--")
        self.message_label.setText(message)
        self.message_label.show()

    @Slot()
    def _on_finished(self) -> None:
        worker = self.worker
        self.worker = None
        self._set_busy(False)
        self.refresh_ffmpeg()
        if worker is not None:
            worker.deleteLater()
        if self._close_when_idle:
            self.close()

    @Slot()
    def import_txt(self) -> None:
        if self.worker is not None or self.batch.active or self.import_worker is not None:
            return
        filename, _ = QFileDialog.getOpenFileName(self, 'Import YouTube links', '', 'Text files (*.txt)')
        if filename:
            self.import_file(Path(filename))

    def import_file(self, path: Path) -> None:
        if self.worker is not None or self.batch.active or self.import_worker is not None:
            return
        existing = frozenset(video_key(item.url) for item in self.queue_model.items)
        self.import_worker = ImportWorker(path, existing)
        self.import_worker.imported.connect(self._imported)
        self.import_worker.failed.connect(self._on_failed)
        self.import_worker.finished.connect(self._import_finished)
        self._set_busy(True)
        self.cancel_button.setEnabled(False)
        self.import_worker.start()

    @Slot(object)
    def _imported(self, result: ImportResult) -> None:
        added = self.queue_model.add(result.urls)
        self.import_report.setPlainText(
            f'Valid unique URLs: {len(result.urls)} | Duplicates: {result.duplicates} | '
            f'Invalid: {len(result.invalid)} | Added to queue: {added}\n' + '\n'.join(result.invalid)
        )
        self.import_report.show()
        self.batch_label.setText(f'Queue: {self.queue_model.rowCount()} videos')
        self.centralWidget().ensureWidgetVisible(self.queue_view)

    @Slot()
    def _import_finished(self) -> None:
        worker, self.import_worker = self.import_worker, None
        if worker is not None:
            worker.deleteLater()
        self._set_busy(False)
        if self._close_when_idle:
            self.close()

    @Slot()
    def remove_pending(self) -> None:
        if self.worker is None and not self.batch.active and self.import_worker is None:
            self.queue_model.remove_pending([index.row() for index in self.queue_view.selectionModel().selectedRows()])
            self.batch_label.setText(f'Queue: {self.queue_model.rowCount()} videos')

    @Slot()
    def clear_queue(self) -> None:
        if self.worker is None and not self.batch.active and self.import_worker is None:
            self.queue_model.clear()
            self.batch_label.setText('Queue: 0 videos')
            self.batch_progress.setRange(0, 1)
            self.batch_progress.setValue(0)

    @Slot()
    def start_batch(self) -> None:
        if self.worker is not None or self.batch.active or self.import_worker is not None:
            return
        self.refresh_ffmpeg()
        if self.ffmpeg_path is None:
            self._on_failed(f'{MISSING_FFMPEG} Install FFmpeg, then click Check again before Download All.')
            return
        if not any(item.status == 'Pending' for item in self.queue_model.items):
            return
        self._set_busy(True)
        self.cancel_button.setEnabled(False)
        self.cancel_batch_button.setEnabled(True)
        self.message_label.hide()
        self.batch.start(BatchConfig(self.quality_combo.currentData(), self.folder))

    @Slot(object)
    def _batch_worker_started(self, worker: DownloadWorker) -> None:
        self.worker = worker
        self.status_label.setText('Working')
        self.progress_bar.setRange(0, 0)
        self.stats_label.setText('Speed --  |  ETA --:--')
        for label in (self.title_label, self.uploader_label, self.duration_label, self.resolution_label, self.size_label):
            label.setText('--')
        worker.stage_changed.connect(self._on_stage)
        worker.metadata_ready.connect(self._on_metadata)
        worker.progress_changed.connect(self._on_progress)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)

    @Slot()
    def _batch_updated(self) -> None:
        summary = self.batch.summary()
        current = min(self.batch.position + 1, summary['total'])
        self.batch_label.setText(
            f"Video {current} / {summary['total']} | Completed: {summary['completed']} | "
            f"Failed: {summary['failed']} | Cancelled: {summary['cancelled']} | Remaining: {summary['remaining']}"
        )
        self.batch_progress.setRange(0, max(1, summary['total']))
        self.batch_progress.setValue(summary['total'] - summary['remaining'])

    @Slot()
    def cancel_batch(self) -> None:
        self.batch.cancel()
        self.cancel_batch_button.setEnabled(False)

    @Slot(object)
    def _batch_finished(self, summary: dict) -> None:
        self.worker = None
        self._set_busy(False)
        self.cancel_batch_button.setEnabled(False)
        self._batch_updated()
        self.status_label.setText('Batch cancelled' if self.batch.stopping else 'Batch finished')
        self.message_label.setText(
            f"Total: {summary['total']} | Completed: {summary['completed']} | Failed: {summary['failed']} | "
            f"Cancelled: {summary['cancelled']}\nOutput: {self.batch.config.folder}"
        )
        self.message_label.show()
        if self._close_when_idle:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.settings.save_geometry(self.saveGeometry())
        if self.batch.active or self.import_worker is not None:
            self._close_when_idle = True
            self.cancel_batch()
            event.ignore()
            return
        if self.worker is not None and self.worker.isRunning():
            self._close_when_idle = True
            self.cancel_download()
            event.ignore()
            return
        super().closeEvent(event)

