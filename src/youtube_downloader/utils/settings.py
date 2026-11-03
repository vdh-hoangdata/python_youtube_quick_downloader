from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings, QStandardPaths


def default_download_folder() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    return Path(location) if location else Path.home() / "Downloads"


class AppSettings:
    def __init__(self, settings: QSettings | None = None) -> None:
        self.storage = settings if settings is not None else QSettings()

    def download_folder(self) -> Path:
        saved = self.storage.value("download_folder", "", type=str)
        path = Path(saved).expanduser() if saved else None
        if path is not None and path.is_absolute() and path.is_dir():
            return path
        return default_download_folder()

    def save_folder(self, folder: Path) -> None:
        self.storage.setValue("download_folder", str(folder.resolve()))
        self.storage.sync()

    def window_geometry(self) -> QByteArray:
        value = self.storage.value("window_geometry")
        return value if isinstance(value, QByteArray) else QByteArray()

    def save_geometry(self, geometry: QByteArray) -> None:
        self.storage.setValue("window_geometry", geometry)
        self.storage.sync()

