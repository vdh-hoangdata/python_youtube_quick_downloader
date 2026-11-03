import errno
import re

from youtube_downloader.services.ffmpeg import MISSING_FFMPEG
from youtube_downloader.services.models import DownloadError, Stage


def user_error(exc: Exception, *, stage: Stage | None = None) -> str:
    if isinstance(exc, DownloadError):
        return str(exc)
    if isinstance(exc, PermissionError) or getattr(exc, "errno", None) in {errno.EACCES, errno.EPERM}:
        return "Cannot write to this folder. Choose a folder you can write to."
    if getattr(exc, "errno", None) == errno.ENOSPC:
        return "There is not enough free disk space. Free space or choose another folder."
    detail = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).lower()
    cases = (
        (("no space left", "disk full", "not enough space", "errno 28"),
         "There is not enough free disk space. Free space or choose another folder."),
        (("permission denied", "access is denied", "errno 13", "winerror 5"),
         "Cannot write to this folder. Choose a folder you can write to."),
        (("ffmpeg is not installed", "ffmpeg not found", "ffmpeg could not be found"), MISSING_FFMPEG),
        (("requested format", "no video formats", "no suitable formats"),
         "Format unavailable: no downloadable format matches this quality. Try Automatic / Best or update yt-dlp."),
        (("private video", "video is private"), "This video is private and cannot be downloaded without access."),
        (("not available in your country", "geo restricted", "geo-restricted", "geographic restriction"),
         "This video is unavailable in your region."),
        (("age-restricted", "confirm your age", "sign in", "login required", "members-only", "join this channel"),
         "This video requires sign-in, age verification, or membership. Version 1 does not support login."),
        (("has been removed", "video unavailable", "video is not available", "copyright", "deleted video"),
         "This video is unavailable or has been removed."),
        (("javascript runtime", "challenge solver", "yt-dlp-ejs", "n challenge", "signature extraction"),
         "YouTube extraction needs a supported local JavaScript runtime and current yt-dlp. See README.md."),
        (("unsupported url", "invalid url", "not a valid url"),
         "This is not a supported single YouTube video link."),
        (("timed out", "timeout", "connection", "urlopen", "http error", "name resolution", "network is unreachable", "getaddrinfo", "certificate verify failed"),
         "Network failure: could not reach YouTube or its media server. Check your connection and try again."),
        (("file name too long", "filename or extension is too long", "winerror 206"),
         "The output path is too long. Choose a folder with a shorter path."),
    )
    for fragments, message in cases:
        if any(fragment in detail for fragment in fragments):
            return message
    if stage in {Stage.MERGING, Stage.SAVING} or any(fragment in detail for fragment in (
        "postprocessing:", "post-processing", "ffmpeg exited", "unable to merge", "merging failed",
    )):
        return "Merge/post-processing failed. Check your FFmpeg installation and the local log for details."
    if stage == Stage.EXTRACTING or any(fragment in detail for fragment in ("unable to extract", "extraction failed")):
        return "yt-dlp extraction failed. Update yt-dlp and see the local log for details."
    return "The download could not be completed. See the local log for technical details."

