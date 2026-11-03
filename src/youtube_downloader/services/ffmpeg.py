import os
from pathlib import Path
import shutil
import subprocess

from youtube_downloader.services.models import DownloadError


MISSING_FFMPEG = "FFmpeg is required to merge high-quality video and audio streams."
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def windows_environment_path() -> str:
    """Read updated Windows PATH values without changing this process's environment."""
    if os.name != "nt":
        return ""
    import winreg

    paths: list[str] = []
    for hive, key in (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    ):
        try:
            with winreg.OpenKey(hive, key) as registry_key:
                value, _ = winreg.QueryValueEx(registry_key, "Path")
            if isinstance(value, str) and value:
                paths.append(os.path.expandvars(value))
        except OSError:
            # A missing/inaccessible registry key must not prevent local detection.
            continue
    return os.pathsep.join(paths)


def find_ffmpeg(project_root: Path = PROJECT_ROOT) -> Path | None:
    discovered = shutil.which("ffmpeg")
    if not discovered:
        # Installing into the user/system PATH does not update a running app's PATH.
        refreshed_path = windows_environment_path()
        if refreshed_path:
            discovered = shutil.which("ffmpeg", path=refreshed_path)
    if discovered:
        return Path(discovered).resolve()
    configured = os.environ.get("YLD_FFMPEG_PATH")
    executable = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if configured:
        path = Path(configured).expanduser()
        path = path / executable if path.is_dir() else path
        # Preserve the explicit fallback's behavior when PATH has no FFmpeg.
        return path.resolve() if path.is_file() else None
    for directory in (
        project_root / "tools" / "ffmpeg" / "bin",
        project_root / "tools" / "ffmpeg",
        project_root / "tools",
    ):
        path = directory / executable
        if path.is_file():
            return path.resolve()
    return None


def verify_ffmpeg(path: Path | None) -> Path:
    if path is None:
        raise DownloadError(MISSING_FFMPEG)
    try:
        checked = subprocess.run(
            [str(path), "-version"], shell=False, capture_output=True,
            timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DownloadError("FFmpeg was found but could not be started. Check your FFmpeg installation.") from exc
    if checked.returncode != 0 or b"ffmpeg version" not in checked.stdout.lower():
        raise DownloadError("FFmpeg was found but could not be started. Check your FFmpeg installation.")
    return path

