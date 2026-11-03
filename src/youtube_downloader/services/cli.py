"""Gate startup until the parent has attached the process to its Windows job.

This file is executed by its absolute path; it delegates to the installed yt-dlp
CLI without importing Qt, building shell commands, or executing media files.
"""

import sys


def main() -> None:
    if sys.stdin.readline().strip() != "start":
        raise SystemExit("Downloader startup was cancelled.")
    from yt_dlp import main as yt_dlp_main

    yt_dlp_main(sys.argv[1:])


if __name__ == "__main__":
    main()

