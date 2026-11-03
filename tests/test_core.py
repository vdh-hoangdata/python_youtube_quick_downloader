import errno
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests._support import ARTIFACTS

from PySide6.QtCore import QByteArray, QSettings
from yt_dlp import YoutubeDL

from youtube_downloader.services.errors import user_error
from youtube_downloader.services.ffmpeg import MISSING_FFMPEG, find_ffmpeg, verify_ffmpeg, windows_environment_path
from youtube_downloader.services.models import DownloadError, Quality, Stage
from youtube_downloader.services.quality import format_selector
from youtube_downloader.utils.formatting import format_bytes, format_time, number
from youtube_downloader.utils.paths import OUTPUT_TEMPLATE, publish_completed, validate_output
from youtube_downloader.utils.settings import AppSettings
from youtube_downloader.utils.urls import normalize_url


def video_format(height: int, *, combined: bool = False) -> dict:
    return {
        "format_id": str(height), "url": f"https://invalid.test/{height}",
        "height": height, "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a" if combined else "none",
    }


def select(quality: Quality, formats: list[dict]) -> list[dict]:
    with YoutubeDL({"quiet": True, "check_formats": False}) as ydl:
        selector = ydl.build_format_selector(format_selector(quality))
        return list(selector({"formats": formats, "has_merged_format": False, "incomplete_formats": False}))


class QualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.formats = [video_format(360, combined=True), *[video_format(h) for h in (720, 1080, 2160)],
                        {"format_id": "audio", "url": "https://invalid.test/audio", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"}]

    def test_automatic_uses_highest_separate_streams(self) -> None:
        chosen = select(Quality.AUTOMATIC, self.formats)[0]
        self.assertEqual([f["format_id"] for f in chosen["requested_formats"]], ["2160", "audio"])

    def test_caps_choose_correct_actual_formats(self) -> None:
        for quality, height in ((Quality.MAX_1080, 1080), (Quality.MAX_720, 720)):
            with self.subTest(quality=quality):
                self.assertEqual(select(quality, self.formats)[0]["height"], height)

    def test_missing_1080_uses_available_lower_resolution(self) -> None:
        formats = [fmt for fmt in self.formats if fmt["format_id"] != "1080"]
        self.assertEqual(select(Quality.MAX_1080, formats)[0]["height"], 720)

    def test_combined_fallback_has_sound(self) -> None:
        self.assertEqual(select(Quality.AUTOMATIC, [video_format(480, combined=True)])[0]["format_id"], "480")
        self.assertEqual(select(Quality.AUTOMATIC, [video_format(480)]), [])

    def test_caps_do_not_silently_exceed_limit(self) -> None:
        self.assertEqual(select(Quality.MAX_720, [video_format(1080, combined=True)]), [])

    def test_containers_preserve_codec_compatibility(self) -> None:
        from yt_dlp.utils import get_compatible_ext
        self.assertEqual(get_compatible_ext(vcodecs=["avc1"], acodecs=["mp4a"], vexts=["mp4"], aexts=["m4a"], preferences=["mp4", "mkv"]), "mp4")
        self.assertEqual(get_compatible_ext(vcodecs=["vp9"], acodecs=["opus"], vexts=["webm"], aexts=["webm"], preferences=["mp4", "mkv"]), "mkv")


class UrlTests(unittest.TestCase):
    def test_accepts_youtube_variants_without_strict_video_ids(self) -> None:
        values = (
            " https://www.youtube.com/watch?v=abc123&list=xyz ",
            "youtu.be/abc123?t=30", "https://m.youtube.com/shorts/abc123",
            "https://music.youtube.com/watch?v=abc123", "https://www.youtube-nocookie.com/embed/abc123",
            "https://www.youtube.com/live/abc123",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertTrue(normalize_url(value).startswith("https://"))

    def test_normalizes_whitespace_scheme_and_host(self) -> None:
        self.assertEqual(normalize_url("  HTTP://WWW.YouTube.COM/watch?v=x#t=2  "), "https://www.youtube.com/watch?v=x#t=2")

    def test_rejects_invalid_or_deceptive_hosts(self) -> None:
        for value in ("", "garbage", "https://youtube.com.evil.test/watch?v=x", "https://evil-youtube.com/watch?v=x",
                      "https://youtube.com@evil.test/watch?v=x", "file:///video.mp4", "ftp://youtu.be/x",
                      "https://youtu.be/x\n--exec evil", "https://youtu.be:8000/x", "https://youtu.be:bad/x", "https://youtube.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_url(value)


class FileTests(unittest.TestCase):
    def test_safe_windows_filename_template(self) -> None:
        with tempfile.TemporaryDirectory(dir=ARTIFACTS) as directory:
            with YoutubeDL({"outtmpl": str(Path(directory) / OUTPUT_TEMPLATE), "windowsfilenames": True}) as ydl:
                name = Path(ydl.prepare_filename({"title": 'CON: movie / ? * " < > |', "id": "abc123", "ext": "mp4"})).name
            self.assertFalse(any(char in name for char in '<>:"/\\|?*'))
            self.assertTrue(name.endswith("[abc123].mp4"))

    def test_publish_never_overwrites_existing_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=ARTIFACTS) as directory:
            folder = Path(directory)
            staging = folder / "stage"
            staging.mkdir()
            (folder / "video.mp4").write_bytes(b"keep")
            (folder / "video (2).mp4").write_bytes(b"also keep")
            source = staging / "video.mp4"
            source.write_bytes(b"new")
            final = publish_completed(source, folder)
            self.assertEqual(final.name, "video (3).mp4")
            self.assertEqual((folder / "video.mp4").read_bytes(), b"keep")
            self.assertEqual(final.read_bytes(), b"new")
            self.assertFalse(source.exists())

    def test_refuses_outside_files_executables_and_empty_files(self) -> None:
        with tempfile.TemporaryDirectory(dir=ARTIFACTS) as directory:
            folder = Path(directory)
            staging = folder / "stage"
            staging.mkdir()
            for path in (folder / "outside.mp4", staging / "bad.exe", staging / "empty.mp4"):
                path.write_bytes(b"" if "empty" in path.name else b"data")
                with self.subTest(path=path), self.assertRaises(ValueError):
                    validate_output(path, staging)


class SettingsTests(unittest.TestCase):
    def test_folder_and_geometry_persist_across_instances(self) -> None:
        with tempfile.TemporaryDirectory(dir=ARTIFACTS) as directory:
            path = Path(directory)
            filename = str(path / "settings.ini")
            first = AppSettings(QSettings(filename, QSettings.Format.IniFormat))
            first.save_folder(path)
            first.save_geometry(QByteArray(b"geometry"))
            second = AppSettings(QSettings(filename, QSettings.Format.IniFormat))
            self.assertEqual(second.download_folder(), path.resolve())
            self.assertEqual(second.window_geometry(), QByteArray(b"geometry"))
            self.assertEqual(set(second.storage.allKeys()), {"download_folder", "window_geometry"})

    def test_invalid_settings_use_defaults(self) -> None:
        with tempfile.TemporaryDirectory(dir=ARTIFACTS) as directory:
            raw = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            settings = AppSettings(raw)
            raw.setValue("download_folder", "relative/folder")
            raw.setValue("window_geometry", "garbage")
            with patch("youtube_downloader.utils.settings.default_download_folder", return_value=Path(directory)):
                self.assertEqual(settings.download_folder(), Path(directory))
            self.assertTrue(settings.window_geometry().isEmpty())


class UtilityTests(unittest.TestCase):
    def test_formats_unknown_and_known_values(self) -> None:
        self.assertEqual(format_bytes(1024 * 1024 * 8.2), "8.2 MB")
        self.assertEqual(format_time(18), "00:18")
        self.assertEqual(format_time(3661), "1:01:01")
        self.assertEqual(format_time(None), "--:--")
        for value in (None, "NA", float("nan"), float("inf"), -1):
            self.assertIsNone(number(value))

    def test_missing_ffmpeg_is_actionable(self) -> None:
        with self.assertRaisesRegex(DownloadError, MISSING_FFMPEG):
            verify_ffmpeg(None)

    def test_ffmpeg_configuration_and_tools_detection(self) -> None:
        with tempfile.TemporaryDirectory(dir=ARTIFACTS) as directory:
            folder = Path(directory)
            binary = folder / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch.dict("os.environ", {"YLD_FFMPEG_PATH": str(binary)}), \
                 patch("youtube_downloader.services.ffmpeg.shutil.which", return_value=None):
                self.assertEqual(find_ffmpeg(folder), binary.resolve())
            with patch.dict("os.environ", {"YLD_FFMPEG_PATH": str(folder / "missing")}), \
                 patch("youtube_downloader.services.ffmpeg.shutil.which", return_value=None):
                self.assertIsNone(find_ffmpeg(folder))

    def test_concise_error_mapping(self) -> None:
        pairs = (
            (OSError(errno.ENOSPC, "full"), "disk space"),
            (PermissionError("blocked"), "Cannot write"),
            (RuntimeError("Private video"), "private"),
            (RuntimeError("This video is not available in your country"), "region"),
            (RuntimeError("Sign in to confirm your age"), "sign-in"),
            (RuntimeError("Video has been removed"), "removed"),
            (RuntimeError("Requested format is not available"), "matches this quality"),
            (RuntimeError("Connection timed out"), "connection"),
        )
        for error, expected in pairs:
            with self.subTest(error=error):
                self.assertIn(expected, user_error(error))

    def test_error_categories_and_stage_context_are_distinct(self) -> None:
        pairs = (
            (RuntimeError("ffmpeg not found"), Stage.MERGING, MISSING_FFMPEG),
            (RuntimeError("Unable to extract player response"), Stage.EXTRACTING, "yt-dlp extraction failed"),
            (RuntimeError("Requested format is not available"), Stage.EXTRACTING, "Format unavailable"),
            (RuntimeError("HTTP Error 503"), Stage.EXTRACTING, "Network failure"),
            (RuntimeError("unrecognized processor error"), Stage.MERGING, "Merge/post-processing failed"),
            (RuntimeError("Postprocessing: ffmpeg exited with code 1"), Stage.DOWNLOADING, "Merge/post-processing failed"),
            (RuntimeError("ffmpeg encoder is not available"), Stage.MERGING, "Merge/post-processing failed"),
            (PermissionError("Access is denied"), Stage.MERGING, "Cannot write"),
        )
        for error, stage, expected in pairs:
            with self.subTest(error=error, stage=stage):
                self.assertIn(expected, user_error(error, stage=stage))


class FFmpegDetectionTests(unittest.TestCase):
    def test_path_wins_over_configured_and_local_tools(self) -> None:
        path = ARTIFACTS / "path-bin" / "ffmpeg.exe"
        with patch("youtube_downloader.services.ffmpeg.shutil.which", return_value=str(path)) as which, \
             patch("youtube_downloader.services.ffmpeg.windows_environment_path") as refresh, \
             patch.dict("os.environ", {"YLD_FFMPEG_PATH": "missing-override"}):
            self.assertEqual(find_ffmpeg(ARTIFACTS), path.resolve())
        which.assert_called_once_with("ffmpeg")
        refresh.assert_not_called()

    def test_local_tool_folders_are_supported_when_path_is_empty(self) -> None:
        with tempfile.TemporaryDirectory(dir=ARTIFACTS) as directory, \
             patch("youtube_downloader.services.ffmpeg.shutil.which", return_value=None), \
             patch("youtube_downloader.services.ffmpeg.windows_environment_path", return_value=""), \
             patch.dict("os.environ", {"YLD_FFMPEG_PATH": ""}):
            root = Path(directory)
            for relative in ("tools/ffmpeg/bin", "tools/ffmpeg", "tools"):
                binary = root / relative / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.touch()
                with self.subTest(relative=relative):
                    self.assertEqual(find_ffmpeg(root), binary.resolve())
                binary.unlink()
            self.assertIsNone(find_ffmpeg(root))

    def test_new_windows_path_is_found_after_installation(self) -> None:
        path = ARTIFACTS / "new-bin" / "ffmpeg.exe"
        with patch("youtube_downloader.services.ffmpeg.shutil.which", side_effect=[None, str(path)]) as which, \
             patch("youtube_downloader.services.ffmpeg.windows_environment_path", return_value="new-path"):
            self.assertEqual(find_ffmpeg(ARTIFACTS), path.resolve())
        self.assertEqual(which.call_args_list[0].args, ("ffmpeg",))
        self.assertEqual(which.call_args_list[1].kwargs, {"path": "new-path"})

    @unittest.skipUnless(os.name == "nt", "Windows registry PATH")
    def test_reads_updated_machine_and_user_path_and_expands_variables(self) -> None:
        import winreg

        with patch.object(winreg, "OpenKey") as open_key, \
             patch.object(winreg, "QueryValueEx", side_effect=[("%YLD_TEST_ROOT%\\bin", winreg.REG_EXPAND_SZ), ("C:\\UserTools", winreg.REG_SZ)]), \
             patch.dict("os.environ", {"YLD_TEST_ROOT": "C:\\MachineTools"}):
            self.assertEqual(windows_environment_path(), "C:\\MachineTools\\bin;C:\\UserTools")
        self.assertEqual(open_key.call_args_list[0].args[0], winreg.HKEY_LOCAL_MACHINE)
        self.assertEqual(open_key.call_args_list[1].args[0], winreg.HKEY_CURRENT_USER)

    @unittest.skipUnless(os.name == "nt", "Windows registry PATH")
    def test_inaccessible_registry_does_not_block_detection(self) -> None:
        import winreg

        with patch.object(winreg, "OpenKey", side_effect=PermissionError("restricted")):
            self.assertEqual(windows_environment_path(), "")

    def test_verify_reports_found_but_unusable_binary(self) -> None:
        path = ARTIFACTS / "broken-ffmpeg.exe"
        with patch("youtube_downloader.services.ffmpeg.subprocess.run", side_effect=OSError("bad binary")), \
             self.assertRaisesRegex(DownloadError, "found but could not be started"):
            verify_ffmpeg(path)
        with patch("youtube_downloader.services.ffmpeg.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", b"error")), \
             self.assertRaisesRegex(DownloadError, "found but could not be started"):
            verify_ffmpeg(path)


if __name__ == "__main__":
    unittest.main()

