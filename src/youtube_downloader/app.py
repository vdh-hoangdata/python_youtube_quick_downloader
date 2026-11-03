import importlib.util
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

from PySide6.QtCore import QCoreApplication, QStandardPaths
from PySide6.QtWidgets import QApplication, QMessageBox

from youtube_downloader.ui.main_window import MainWindow
from youtube_downloader.ui.theme import light_palette


def configure_logging() -> Path | None:
    override = os.environ.get("YLD_DATA_DIR")
    directory = Path(override) if override else Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / "downloader.log"
        handler = RotatingFileHandler(log_path, maxBytes=512_000, backupCount=2, encoding="utf-8")
    except OSError:
        logging.basicConfig(level=logging.INFO)
        logging.getLogger(__name__).exception("Unable to create the local log file")
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    return log_path


def main() -> int:
    QCoreApplication.setOrganizationName("YouTubeLocalDownloader")
    QCoreApplication.setApplicationName("YouTube Local Downloader")
    app = QApplication(sys.argv)
    app.setPalette(light_palette())
    configure_logging()
    if importlib.util.find_spec("yt_dlp") is None:
        QMessageBox.critical(None, "Missing dependency", "Install requirements.txt in your virtual environment before running the app.")
        return 1
    window = MainWindow()
    window.show()
    return app.exec()

