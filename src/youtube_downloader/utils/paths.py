import os
from pathlib import Path


OUTPUT_TEMPLATE = "%(title).120B [%(id)s].%(ext)s"
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv", ".3gp", ".avi"}


def validate_output(path: Path, staging: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(staging.resolve()) or not resolved.is_file():
        raise ValueError("The downloader returned a file outside its temporary folder.")
    if resolved.suffix.lower() not in MEDIA_EXTENSIONS:
        raise ValueError("The downloader did not return a supported video file.")
    if resolved.stat().st_size == 0:
        raise ValueError("The downloader returned an empty file.")
    return resolved


def publish_completed(source: Path, folder: Path) -> Path:
    """Atomically publish on the same filesystem, never replacing an existing file."""
    for index in range(1, 10000):
        name = source.name if index == 1 else f"{source.stem} ({index}){source.suffix}"
        target = folder / name
        try:
            if os.name == "nt":
                # Windows rename fails if the destination already exists.
                source.rename(target)
            else:
                # POSIX rename would overwrite; an exclusive hard link does not.
                os.link(source, target)
                source.unlink()
        except FileExistsError:
            continue
        return target
    raise FileExistsError("Too many files with the same name in the destination folder.")

