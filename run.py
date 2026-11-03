"""Run directly from the checkout without installing the project."""

from pathlib import Path
import sys

if sys.version_info < (3, 12):
    raise SystemExit("YouTube Local Downloader requires Python 3.12 or newer.")

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

try:
    from youtube_downloader.app import main
except ModuleNotFoundError as exc:
    raise SystemExit(
        f"Missing dependency: {exc.name}. Install requirements.txt in your virtual environment."
    ) from exc

if __name__ == "__main__":
    raise SystemExit(main())

