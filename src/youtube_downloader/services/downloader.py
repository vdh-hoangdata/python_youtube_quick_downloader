from collections.abc import Callable
import importlib.metadata
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from typing import Any

from youtube_downloader.services.ffmpeg import find_ffmpeg, verify_ffmpeg
from youtube_downloader.services.models import (
    DownloadCancelled, DownloadError, DownloadProgress, DownloadRequest, DownloadResult,
    Stage, VideoMetadata,
)
from youtube_downloader.services.process import ProcessRunner
from youtube_downloader.services.quality import format_selector
from youtube_downloader.utils.formatting import number
from youtube_downloader.utils.paths import OUTPUT_TEMPLATE, publish_completed, validate_output
from youtube_downloader.utils.urls import normalize_url


logger = logging.getLogger(__name__)
PROGRESS_PREFIX = "__YLD_PROGRESS__"
POSTPROCESS_PREFIX = "__YLD_POST__"
RESULT_PREFIX = "__YLD_RESULT__"
DOWNLOAD_TEMPLATE = (
    "download:" + PROGRESS_PREFIX
    + '{"progress": %(progress.{status,downloaded_bytes,total_bytes,total_bytes_estimate,speed,eta})j, '
      '"vcodec": %(info.vcodec)j, "acodec": %(info.acodec)j}'
)


def parse_progress(line: str) -> DownloadProgress | None:
    if not line.startswith(PROGRESS_PREFIX):
        return None
    try:
        event = json.loads(line[len(PROGRESS_PREFIX):])
        data = event["progress"]
        downloaded = number(data.get("downloaded_bytes")) or 0
        total = number(data.get("total_bytes"))
        estimated = total is None
        total = total if total is not None else number(data.get("total_bytes_estimate"))
        percent = min(100.0, downloaded / total * 100) if total else None
        if data.get("status") == "finished":
            percent = 100.0
        current = (
            "Downloading audio" if event.get("vcodec") == "none" else
            "Downloading video" if event.get("acodec") == "none" else
            "Downloading video and audio"
        )
        return DownloadProgress(
            current, percent, downloaded, total,
            number(data.get("speed")), number(data.get("eta")), estimated,
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        logger.warning("Ignoring a malformed progress event.")
        return None


def metadata_preview(info: dict[str, Any]) -> VideoMetadata:
    streams = info.get("requested_formats") or [info]
    heights = [number(fmt.get("height")) for fmt in info.get("formats", [])]
    highest = max((height for height in heights if height is not None), default=None)
    sizes = [number(fmt.get("filesize")) for fmt in streams]
    estimates = [number(fmt.get("filesize")) or number(fmt.get("filesize_approx")) for fmt in streams]
    size = sum(sizes) if all(value is not None for value in sizes) else (
        sum(estimates) if all(value is not None for value in estimates) else None
    )
    height = number(info.get("height"))
    return VideoMetadata(
        str(info.get("title") or "Untitled video"),
        str(info.get("uploader") or info.get("channel") or "Unknown"),
        number(info.get("duration")), int(height) if height is not None else None,
        int(highest) if highest is not None else None, size,
        any(value is None for value in sizes),
    )


def base_command(request: DownloadRequest, ffmpeg: Path | None) -> list[str]:
    command = [
        sys.executable, "-u", str(Path(__file__).with_name("cli.py")),
        "--ignore-config", "--no-plugin-dirs", "--no-remote-components", "--no-update",
        "--no-playlist", "--use-extractors", "youtube$", "--abort-on-error",
        "--no-cache-dir", "--no-colors", "--encoding", "utf-8",
        "--socket-timeout", "20", "--retries", "3", "--fragment-retries", "3",
        "--abort-on-unavailable-fragments", "--no-overwrites", "--windows-filenames",
        "--trim-filenames", "160", "--output", OUTPUT_TEMPLATE,
        "--format", format_selector(request.quality),
        "--format-sort-force", "--format-sort", "res,fps,br",
        "--merge-output-format", "mp4/mkv",
        "--no-video-multistreams", "--no-audio-multistreams",
    ]
    if ffmpeg is not None:
        command.extend(["--ffmpeg-location", str(ffmpeg)])
    # Deno is enabled by yt-dlp by default. Enable a local Node as an alternative.
    node = shutil.which("node")
    if node:
        command.extend(["--js-runtimes", f"node:{node}"])
    return command


class Downloader:
    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner if runner is not None else ProcessRunner()
        self.cancelled = threading.Event()

    def cancel(self) -> None:
        self.cancelled.set()
        self.runner.cancel()

    def _check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise DownloadCancelled()

    def download(
        self,
        request: DownloadRequest,
        on_stage: Callable[[Stage], None],
        on_metadata: Callable[[VideoMetadata], None],
        on_progress: Callable[[DownloadProgress], None],
    ) -> DownloadResult | None:
        request = DownloadRequest(
            normalize_url(request.url), request.folder.resolve(), request.quality, request.preview_only,
        )
        try:
            version = importlib.metadata.version("yt-dlp")
        except importlib.metadata.PackageNotFoundError as exc:
            raise DownloadError("yt-dlp is missing. Install requirements.txt in your virtual environment.") from exc
        logger.info("Starting job with yt-dlp %s; quality=%s", version, request.quality)
        self._check_cancelled()
        request.folder.mkdir(parents=True, exist_ok=True)
        # Staging on the destination volume allows an atomic, exclusive final move.
        staging = Path(tempfile.mkdtemp(prefix=".youtube-local-", dir=request.folder))
        try:
            return self._download_in_staging(request, staging, on_stage, on_metadata, on_progress)
        finally:
            # Only this job's newly created directory is ever removed.
            if staging.parent.resolve() != request.folder or not staging.name.startswith(".youtube-local-"):
                raise RuntimeError("Refusing to clean an unexpected temporary directory.")
            for attempt in range(10):
                try:
                    shutil.rmtree(staging)
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    if attempt == 9:
                        logger.exception("Could not remove temporary directory %s", staging)
                    else:
                        time.sleep(0.1)

    def _download_in_staging(
        self, request: DownloadRequest, staging: Path,
        on_stage: Callable[[Stage], None], on_metadata: Callable[[VideoMetadata], None],
        on_progress: Callable[[DownloadProgress], None],
    ) -> DownloadResult | None:
        self._check_cancelled()
        ffmpeg = find_ffmpeg()
        command = base_command(request, ffmpeg)
        on_stage(Stage.EXTRACTING)
        extracted: dict[str, Any] | None = None

        def receive_metadata(line: str) -> None:
            nonlocal extracted
            if line.startswith("{"):
                try:
                    extracted = json.loads(line)
                except ValueError:
                    logger.warning("Invalid metadata JSON from yt-dlp.")

        self.runner.run(
            [*command, "--no-check-formats" if request.preview_only else "--check-formats",
             "--dump-single-json", "--", request.url],
            receive_metadata, self.cancelled, staging,
        )
        self._check_cancelled()
        if not isinstance(extracted, dict):
            raise DownloadError("yt-dlp extraction failed: no video information was returned. Update yt-dlp and try again.")
        if extracted.get("_type", "video") != "video" or "entries" in extracted:
            raise DownloadError("Paste a single video link. Playlists and channels are not supported.")
        if extracted.get("is_live") or extracted.get("live_status") in {"is_live", "is_upcoming"}:
            raise DownloadError("Live and upcoming streams are not supported in version 1.")
        metadata = metadata_preview(extracted)
        on_metadata(metadata)
        self._check_cancelled()
        if request.preview_only:
            on_stage(Stage.PREVIEWED)
            return None
        merging = len(extracted.get("requested_formats") or []) > 1
        if merging:
            verify_ffmpeg(ffmpeg)
        self._check_cancelled()
        info_file = staging / "video.info.json"
        # Clear extractor-suggested output paths; this job controls all destinations.
        for key in ("_filename", "filename", "filepath", "__files_to_move", "requested_downloads"):
            extracted.pop(key, None)
        info_file.write_text(json.dumps(extracted, ensure_ascii=False), encoding="utf-8")
        output: Path | None = None

        def receive_download(line: str) -> None:
            nonlocal output
            progress = parse_progress(line)
            if progress is not None:
                on_progress(progress)
            elif line.startswith(POSTPROCESS_PREFIX):
                on_stage(Stage.MERGING if merging else Stage.SAVING)
            elif line.startswith(RESULT_PREFIX):
                try:
                    path = json.loads(line[len(RESULT_PREFIX):])
                    if isinstance(path, str):
                        output = staging / path
                except ValueError:
                    logger.warning("Invalid final path JSON from yt-dlp.")

        on_stage(Stage.DOWNLOADING)
        self.runner.run(
            [
                *command, "--load-info-json", str(info_file), "--no-simulate", "--newline",
                "--progress", "--progress-delta", "0.2", "--progress-template", DOWNLOAD_TEMPLATE,
                "--progress-template", f"postprocess:{POSTPROCESS_PREFIX}%(progress.postprocessor)j",
                "--print", f"after_move:{RESULT_PREFIX}%(filepath)j",
            ],
            receive_download, self.cancelled, staging,
        )
        self._check_cancelled()
        if output is None:
            raise DownloadError("The downloader did not report a completed video file. See the local log.")
        output = validate_output(output, staging)
        on_stage(Stage.SAVING)
        self._check_cancelled()
        # Completion wins once the atomic move commits; never delete a completed file.
        final_path = publish_completed(output, request.folder)
        logger.info("Completed: %s", final_path)
        on_stage(Stage.COMPLETED)
        return DownloadResult(final_path, metadata.title)

