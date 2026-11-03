import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests._support import ARTIFACTS

from youtube_downloader.services.downloader import (
    Downloader, POSTPROCESS_PREFIX, PROGRESS_PREFIX, RESULT_PREFIX, base_command, metadata_preview, parse_progress,
)
from youtube_downloader.services.ffmpeg import MISSING_FFMPEG
from youtube_downloader.services.models import DownloadCancelled, DownloadError, DownloadRequest, Quality, Stage


INFO = {
    "id": "abc123", "title": "Example video", "uploader": "Example channel", "duration": 90,
    "height": 2160, "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a",
    "formats": [{"height": 720}, {"height": 2160}],
    "requested_formats": [
        {"height": 2160, "filesize": 100, "vcodec": "avc1", "acodec": "none"},
        {"filesize_approx": 20, "vcodec": "none", "acodec": "mp4a"},
    ],
}


class FakeRunner:
    def __init__(self, info: dict | None = None, *, error: Exception | None = None, cancel: bool = False, suffix: str = ".mp4") -> None:
        self.info = copy.deepcopy(INFO if info is None else info)
        self.error = error
        self.cancel_download = cancel
        self.suffix = suffix
        self.commands: list[list[str]] = []
        self.cancel_called = False

    def cancel(self) -> None:
        self.cancel_called = True

    def run(self, command, on_line, cancelled, cwd) -> None:
        self.commands.append(command)
        if "--dump-single-json" in command:
            on_line(json.dumps(self.info))
            return
        (cwd / "temporary.part").write_bytes(b"partial")
        if self.cancel_download:
            cancelled.set()
            raise DownloadCancelled()
        if self.error:
            raise self.error
        for vcodec, acodec in (("avc1", "none"), ("none", "mp4a")):
            on_line(PROGRESS_PREFIX + json.dumps({
                "progress": {"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "speed": 10, "eta": 5},
                "vcodec": vcodec, "acodec": acodec,
            }))
        on_line(POSTPROCESS_PREFIX + '"Merger"')
        path = cwd / f"Example video [abc123]{self.suffix}"
        path.write_bytes(b"completed fixture")
        on_line(RESULT_PREFIX + json.dumps(str(path)))


class ProgressTests(unittest.TestCase):
    def test_real_numeric_progress_with_stream_stage(self) -> None:
        progress = parse_progress(PROGRESS_PREFIX + json.dumps({
            "progress": {"status": "downloading", "downloaded_bytes": 734, "total_bytes": 1000, "speed": 8.2 * 1024**2, "eta": 18},
            "vcodec": "none", "acodec": "mp4a",
        }))
        self.assertEqual(progress.current, "Downloading audio")
        self.assertAlmostEqual(progress.percent, 73.4)
        self.assertEqual(progress.eta, 18)
        self.assertFalse(progress.estimated)

    def test_unknown_size_and_estimated_size(self) -> None:
        for total, expected in ((None, None), (200, 50)):
            progress = parse_progress(PROGRESS_PREFIX + json.dumps({
                "progress": {"downloaded_bytes": 100, "total_bytes_estimate": total, "speed": "NA", "eta": "NA"},
                "vcodec": "avc1", "acodec": "none",
            }))
            self.assertEqual(progress.percent, expected)
            self.assertIsNone(progress.speed)
            self.assertTrue(progress.estimated)

    def test_malformed_event_is_ignored(self) -> None:
        for line in ("ordinary log", PROGRESS_PREFIX + "not json", PROGRESS_PREFIX + "null"):
            self.assertIsNone(parse_progress(line))

    def test_metadata_size_estimate_and_highest_resolution(self) -> None:
        metadata = metadata_preview(INFO)
        self.assertEqual(metadata.size, 120)
        self.assertTrue(metadata.size_is_estimate)
        self.assertEqual(metadata.highest_height, 2160)


class DownloaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=ARTIFACTS)
        self.folder = Path(self.temporary.name)
        self.request = DownloadRequest("https://youtu.be/abc123", self.folder)
        self.stages = []
        self.metadata = []
        self.progress = []
        self.ffmpeg_patch = patch("youtube_downloader.services.downloader.find_ffmpeg", return_value=None)
        self.ffmpeg_patch.start()

    def tearDown(self) -> None:
        self.ffmpeg_patch.stop()
        self.temporary.cleanup()

    def run_download(self, runner: FakeRunner):
        return Downloader(runner).download(self.request, self.stages.append, self.metadata.append, self.progress.append)

    def test_missing_ffmpeg_stops_before_actual_download(self) -> None:
        runner = FakeRunner()
        with self.assertRaisesRegex(DownloadError, MISSING_FFMPEG):
            self.run_download(runner)
        self.assertEqual(len(runner.commands), 1)
        self.assertEqual(self.metadata[0].title, "Example video")
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_metadata_only_request_never_checks_or_downloads_media_streams(self) -> None:
        runner = FakeRunner()
        request = DownloadRequest(self.request.url, self.folder, preview_only=True)
        with patch("youtube_downloader.services.downloader.verify_ffmpeg") as verify:
            result = Downloader(runner).download(request, self.stages.append, self.metadata.append, self.progress.append)
        self.assertIsNone(result)
        verify.assert_not_called()
        self.assertEqual(len(runner.commands), 1)
        self.assertIn("--no-check-formats", runner.commands[0])
        self.assertNotIn("--check-formats", runner.commands[0])
        self.assertEqual(self.metadata[0].height, 2160)
        self.assertEqual(self.stages, [Stage.EXTRACTING, Stage.PREVIEWED])
        self.assertEqual(self.progress, [])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_completion_stages_and_cached_metadata(self) -> None:
        runner = FakeRunner(suffix=".mkv")
        with patch("youtube_downloader.services.downloader.verify_ffmpeg"):
            result = self.run_download(runner)
        self.assertEqual(self.stages, [Stage.EXTRACTING, Stage.DOWNLOADING, Stage.MERGING, Stage.SAVING, Stage.COMPLETED])
        self.assertEqual([p.current for p in self.progress], ["Downloading video", "Downloading audio"])
        self.assertTrue(result.path.exists())
        self.assertIn("MKV", result.container_note)
        self.assertIn("--load-info-json", runner.commands[1])
        self.assertNotIn(self.request.url, runner.commands[1])
        self.assertEqual(list(self.folder.iterdir()), [result.path])

    def test_combined_format_works_without_ffmpeg(self) -> None:
        info = copy.deepcopy(INFO)
        info.pop("requested_formats")
        result = self.run_download(FakeRunner(info))
        self.assertTrue(result.path.exists())
        self.assertNotIn(Stage.MERGING, self.stages)

    def test_cancellation_cleans_only_job_files(self) -> None:
        existing = self.folder / "keep.mp4"
        existing.write_bytes(b"keep")
        runner = FakeRunner(cancel=True)
        with patch("youtube_downloader.services.downloader.verify_ffmpeg"), self.assertRaises(DownloadCancelled):
            self.run_download(runner)
        self.assertEqual(list(self.folder.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b"keep")

    def test_failure_cleans_partials(self) -> None:
        with patch("youtube_downloader.services.downloader.verify_ffmpeg"), self.assertRaisesRegex(RuntimeError, "network"):
            self.run_download(FakeRunner(error=RuntimeError("network failure")))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_duplicate_completed_files_receive_suffix(self) -> None:
        with patch("youtube_downloader.services.downloader.verify_ffmpeg"):
            first = self.run_download(FakeRunner())
            second = self.run_download(FakeRunner())
        self.assertNotEqual(first.path, second.path)
        self.assertTrue(second.path.name.endswith("(2).mp4"))
        self.assertTrue(first.path.exists())

    def test_playlist_and_live_metadata_are_rejected(self) -> None:
        for extra in ({"_type": "playlist", "entries": []}, {"is_live": True}):
            with self.subTest(extra=extra), self.assertRaises(DownloadError):
                self.run_download(FakeRunner({**INFO, **extra}))
            self.assertEqual(list(self.folder.iterdir()), [])

    def test_command_disables_config_plugins_remote_scripts_and_shell_escapes(self) -> None:
        request = DownloadRequest("https://youtu.be/abc123?x=$(unsafe)", self.folder, Quality.MAX_720)
        command = base_command(request, None)
        self.assertIsInstance(command, list)
        for option in ("--ignore-config", "--no-plugin-dirs", "--no-remote-components", "--no-overwrites"):
            self.assertIn(option, command)
        self.assertNotIn("--exec", command)
        self.assertEqual(command[command.index("--merge-output-format") + 1], "mp4/mkv")


if __name__ == "__main__":
    unittest.main()

