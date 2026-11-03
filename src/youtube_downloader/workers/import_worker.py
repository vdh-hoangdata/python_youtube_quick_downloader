from pathlib import Path
from PySide6.QtCore import QThread, Signal
from youtube_downloader.services.txt_import import parse_txt


class ImportWorker(QThread):
    imported = Signal(object)
    failed = Signal(str)

    def __init__(self, path: Path, existing: frozenset[str]) -> None:
        super().__init__()
        self.path, self.existing = path, existing

    def run(self) -> None:
        try:
            result = parse_txt(self.path, self.existing)
        except (OSError, ValueError) as exc:
            self.failed.emit(f'Cannot import TXT: {exc}')
        else:
            self.imported.emit(result)
