from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Quality(StrEnum):
    AUTOMATIC = "Automatic / Best"
    MAX_1080 = "Max 1080p"
    MAX_720 = "Max 720p"


class Stage(StrEnum):
    EXTRACTING = "Retrieving video information"
    PREVIEWED = "Metadata ready"
    DOWNLOADING = "Downloading"
    MERGING = "Merging streams"
    SAVING = "Saving file"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"
    FAILED = "Download failed"


@dataclass(frozen=True)
class DownloadRequest:
    url: str
    folder: Path
    quality: Quality = Quality.AUTOMATIC
    preview_only: bool = False


@dataclass(frozen=True)
class VideoMetadata:
    title: str
    uploader: str
    duration: float | None
    height: int | None
    highest_height: int | None
    size: float | None
    size_is_estimate: bool


@dataclass(frozen=True)
class DownloadProgress:
    current: str
    percent: float | None
    downloaded: float
    total: float | None
    speed: float | None
    eta: float | None
    estimated: bool = False


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    title: str

    @property
    def container_note(self) -> str:
        if self.path.suffix.lower() == ".mp4":
            return "Saved as MP4. No video re-encoding was performed."
        return (
            f"Saved as {self.path.suffix[1:].upper()} to preserve the original quality "
            "without re-encoding."
        )


class DownloadCancelled(Exception):
    """The user cancelled an owned download job."""


class DownloadError(Exception):
    """An actionable message safe to display directly."""

