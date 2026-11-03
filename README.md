# YouTube Local Downloader

A small Python 3.12+ / PySide6 desktop app for downloading YouTube videos
directly to a folder on your own computer. Paste a video link, choose a quality
limit if desired, and click **Download**. **Cancel** stops the active job. If
FFmpeg is missing, **Preview metadata** remains available without downloading
video or audio.

Use this tool only for content you are permitted to download.

## Local design

The app runs locally. An internet connection is still needed to retrieve video
information and media from YouTube. There is no hosted downloader, external web
API, cloud storage, telemetry, account system, advertising, or browser integration.
Media is never executed or automatically opened.

The GUI uses PySide6's **Essentials** distribution, which provides QtCore, QtGui,
QtWidgets and QtTest without Qt's optional WebEngine/PDF/Addons packages. The other
runtime requirement is `yt-dlp[default]`. Its upstream default dependency group
includes the matching `yt-dlp-ejs` challenge solver and supported networking
dependencies. A local JavaScript runtime and FFmpeg are separate executables.

The app never installs or updates software itself. yt-dlp configuration files,
third-party plugins, remote JavaScript components and automatic updates are
disabled. URLs are passed as individual arguments with `shell=False`; the
extractor is limited to single YouTube videos.

## Windows setup

1. Install **Python 3.12 or newer** from [python.org](https://www.python.org/downloads/windows/).
   The examples use 3.12; substitute your installed version, such as `-3.13`.
2. In PowerShell, enter this project directory and create a virtual environment:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   No environment activation or administrator rights are needed.

3. Install **FFmpeg and ffprobe** for high-quality video/audio merging.
   Start from the [official FFmpeg download page](https://ffmpeg.org/download.html)
   and follow its Windows build links. Download and extract a build yourself;
   this app does not download executable binaries. Keep `ffmpeg.exe` and
   `ffprobe.exe` together. Either:

   - Add the extracted `bin` directory to your **user** PATH, then click **Check again**.
   - Copy those executables and required companion DLLs, if any, into
     `tools\ffmpeg\bin\` in this checkout.
   - Point to your own installation for the current PowerShell session:

     ```powershell
     $env:YLD_FFMPEG_PATH = 'C:\Tools\ffmpeg\bin'
     ```

   Detection searches **PATH first**, including the current Windows user/system
   PATH from the registry when the running process has a stale PATH. Then it
   checks `YLD_FFMPEG_PATH`, followed by `tools\ffmpeg\bin`, `tools\ffmpeg`, and
   `tools`. Set `YLD_FFMPEG_PATH` before launching; it can be a directory or the
   full path to `ffmpeg.exe`. If that fallback is set but invalid and PATH has no
   FFmpeg, detection reports it missing. After installing FFmpeg into PATH or
   a supported tools directory, **Check again** detects it without restarting.

   Check a PATH installation with:

   ```powershell
   ffmpeg -version
   ffprobe -version
   ```

   The Python package named `ffmpeg` is **not** the required executable.

4. Provide a supported **local JavaScript runtime** for current YouTube extraction.
   [yt-dlp's official EJS setup guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
   explains this requirement. Use **Deno 2.3+** from the
   [official Deno installation instructions](https://docs.deno.com/runtime/getting_started/installation/),
   or an existing **Node.js 22+** installation from [nodejs.org](https://nodejs.org/en/download/).
   Put its executable on PATH. yt-dlp enables Deno by default; this app also
   explicitly enables Node when it is on PATH. The challenge solver ships through
   the Python requirements; fetching remote scripts at runtime is disabled.

5. Run the app:

   ```powershell
   .\.venv\Scripts\python.exe run.py
   ```

## Quality and containers

**Automatic / Best** uses this yt-dlp format selector:

```text
bestvideo+bestaudio/bestvideo[height<=1080]+bestaudio/bestvideo[height<=720]+bestaudio/best
```

`+` pairs video-only and audio-only streams. `/` chooses the first available
alternative. **Max 1080p** and **Max 720p** use:

```text
bestvideo[height<=1080]+bestaudio/best[height<=1080]
bestvideo[height<=720]+bestaudio/best[height<=720]
```

The limits stay in effect on the combined-format fallback. If a 1080p video is
not available, an available 720p or lower format can be selected. Unknown heights
do not bypass an explicit cap. Resolution, frame rate and bitrate are prioritized
with `--format-sort-force --format-sort res,fps,br`.

The unbounded video/audio pair already includes lower resolutions, so the capped
Automatic alternatives will usually be redundant. They are not four separate
download attempts. `--check-formats` tests candidate availability during extraction
and lets yt-dlp skip unavailable streams. Within a download job, metadata is
extracted once and reused through `--load-info-json`. Metadata-only previews
disable stream availability checks and do not start a media transfer. A later
Download retrieves fresh metadata so it does not reuse expired stream URLs.

**A slash fallback handles format selection, not a network failure after a
download starts.** Network errors get bounded yt-dlp retries. Disk, permission or
merging failures are reported; the app does not redownload progressively smaller
copies of the same video. Container incompatibility is handled by choosing MKV
rather than lowering resolution or converting codecs. See
[yt-dlp's format documentation](https://github.com/yt-dlp/yt-dlp#format-selection).

`--merge-output-format mp4/mkv` asks yt-dlp to prefer MP4 when its compatibility
rules permit it, otherwise MKV. FFmpeg merges with stream copying, without video
transcoding. A combined stream retains its original container, which may be
WebM. The completion message shows the actual container and file path. There is
no conversion tool or forced codec change; some newer codecs require a compatible
local player.

FFmpeg is detected at startup. A compact status beside the controls shows
**FFmpeg: Ready** or **FFmpeg: Not found — required for high-quality video/audio
merging.** Detection does not run FFmpeg or block the GUI waiting for a process.

When it is missing, the main action becomes **Preview metadata**. You can still
see the title, channel, duration and selected/highest resolution, including
2160p, without downloading video or audio. Install FFmpeg and click **Check
again** to restore **Download**; Automatic / Best keeps its highest-quality
video + best audio strategy.

If FFmpeg disappears before a split-stream download starts, the existing
download preflight still stops before downloading the streams with:

> FFmpeg is required to merge high-quality video and audio streams.

The app does not silently substitute a lower-quality combined stream. The GUI
offers metadata previews while FFmpeg is missing; the downloader service retains
its support for combined streams that need no merging.

## Files, progress and cancellation

The default folder comes from Qt's Windows Downloads location, including a
redirected Downloads folder. **Choose Folder** saves the selection locally.
Only the last chosen folder and window size/position are remembered with
`QSettings`. On Windows these settings are under:

```text
HKEY_CURRENT_USER\Software\YouTubeLocalDownloader\YouTube Local Downloader
```

The filename template is `%(title).120B [%(id)s].%(ext)s`: title length is bounded
and yt-dlp sanitizes Windows filenames. If the destination already exists, the
new file receives ` (2)`, ` (3)`, etc. Publishing the final file is atomic and does
not overwrite an existing file.

Each job gets a new `.youtube-local-...` folder inside the destination. Downloads,
metadata and merging stay there until the finished video is moved into the main
folder. On completion, cancellation or failure, the app removes only that job's
temporary folder. Completed videos and unrelated files are left untouched. An
external file lock can prevent cleanup; this is recorded in the local log. An
abrupt power loss or forced app termination can leave a temporary folder behind.

Progress comes from yt-dlp's structured progress output. Percentages are **per
stream**: video and audio each have their own progress. Estimated totals are
marked with `~`; unknown totals show an indeterminate bar. Download speed and ETA
are reported when yt-dlp provides them. Extraction and merging use an indeterminate
bar because their percentage is not available.

Extraction, downloading and processing run outside the GUI thread. Cancel also
works during extraction and merging. On Windows, a private Job Object owns the
yt-dlp process and its FFmpeg/JavaScript children. The child waits for the parent
to attach it before starting yt-dlp. Only that job is terminated. On POSIX, a
private process group provides the equivalent behavior. Closing the window cancels
the job and waits asynchronously for cleanup. Once the final file move commits,
the job is complete and the file is preserved even if Cancel was just pressed.

## Troubleshooting and local logs

Errors are shown in short messages; technical details go to a rotating local log:

```text
%LOCALAPPDATA%\YouTubeLocalDownloader\YouTube Local Downloader\downloader.log
```

Logs stay on your machine and may contain filenames and yt-dlp diagnostics.
To use a different log directory, set `YLD_DATA_DIR` before launching the app.

- **FFmpeg missing:** use the Windows setup instructions above, then click
  **Check again**. Metadata preview remains available.
- **FFmpeg found but cannot start:** check that the configured executable and
  its companion DLLs are valid. This is reported separately from missing FFmpeg.
- **yt-dlp extraction failed:** update yt-dlp and its matched dependencies
  manually, check the JavaScript runtime, and retry.

  ```powershell
  .\.venv\Scripts\python.exe -m pip install --upgrade "yt-dlp[default]"
  ```

- **Private, age-restricted, member-only or login-required video:** version 1
  has no cookies or login UI and cannot access videos requiring authentication.
- **Removed or region-restricted video:** the app reports the restriction.
- **Format unavailable:** try Automatic / Best or update yt-dlp. This is distinct
  from a video being private or removed.
- **Network failure / HTTP errors:** check your connection and retry. YouTube
  may change its delivery behavior or restrict access, so current yt-dlp may be
  required.
- **Merge/post-processing failed:** check the FFmpeg installation and local log.
  Disk space and write permission errors retain their specific messages.
- **Disk full / access denied:** free disk space or select a writable folder.
- **Output path too long:** use a destination with a shorter path.
- **Playlist, channel, live or upcoming link:** use a single prerecorded video.
  A normal `watch?v=...&list=...` link downloads only that video.

## Architecture and verification

```text
run.py
src/youtube_downloader/
    app.py
    ui/main_window.py
    services/{downloader,process,cli,quality,ffmpeg,errors,models}.py
    workers/download_worker.py
    utils/{urls,paths,formatting,settings}.py
tests/
```

The CLI approach is intentional: a `QThread` supervises an owned subprocess,
allowing reliable cancellation while yt-dlp or FFmpeg is blocked. No thread is
forcibly terminated. The tiny `cli.py` startup gate delegates to the installed
yt-dlp CLI. Metadata (`--dump-single-json`), progress (`--progress-template`) and
the final output path (`--print after_move`) are parsed as JSON; ordinary log text
is never used to determine progress, success or filenames.

Run the tests using the standard library, without another test dependency:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover actual yt-dlp selection with synthetic formats, URLs, filenames,
container compatibility, collision handling, settings, errors, cleanup,
state transitions, Qt event-loop responsiveness and cancellation. The process
integration tests invoke the installed CLI, verify Windows child-process
cancellation while preserving an unrelated process, and transfer a **local file
fixture** through real yt-dlp progress templates. File URLs are enabled only in
that test. No automated test contacts a real YouTube video, and the fixture is
not playable media.

Verified on Windows with Python 3.13, Qt 6.12 Essentials and yt-dlp 2026.08.19:
**68 offline tests passed** for Phase 2. Seven batch GUI tests also passed using
native Windows widgets and mocked/local transfers; native startup recorded 60
event-loop timer ticks. FFmpeg is detected on the current machine, but a real
YouTube download and an actual FFmpeg merge remain **NOT TESTED**.

The app deliberately has no thumbnails, history, playlists,
audio-only mode, subtitles, browser cookies or conversion tools.

## Phase 2: TXT batch queue

Single-video Download / Preview metadata still works as before. For a batch:

1. Use **Import TXT** and choose a UTF-8 or UTF-8 BOM file.
2. Put one single-video URL on each line; blank lines and surrounding spaces are ignored.
   CRLF and LF both work. Commas are not separators. Copy `links.example.txt` to
   `links.txt` and replace its video IDs; your personal `links.txt` is ignored by Git.
3. Review the import report: unique valid URLs, duplicates, invalid lines with line
   numbers, and items added. Import never retrieves metadata or starts downloads.
4. Choose the global Quality and Save to folder, then **Download All**.
5. Use **Cancel Batch** to stop the current owned process tree and cancel pending items.

Import is bounded to **1 MiB / 5,000 lines** and the in-memory queue to **5,000
unique videos**. Parsing runs off the GUI thread. Duplicate IDs are removed across
watch, short, embed and youtu.be URLs, including IDs already in the queue. Valid
items retain their order; an invalid URL does not discard the other lines. Playlist
and channel links are rejected, and a watch link with a playlist parameter still
represents only one video. Invalid UTF-8, oversized files and queue overflow reject
that import without changing the existing queue.

Downloads are **sequential: concurrency = 1**. A controller starts the next existing
DownloadWorker only after the previous QThread has finished cleanup. Single-video
downloads, TXT import and batch runs cannot overlap. Each batch freezes its quality
and output directory when Download All is pressed. Automatic / Best remains
unbounded (including 1440p, 2160p and higher); the existing 1080p/720p caps and
MP4/MKV rules are reused without transcoding.

The table shows each item's title/URL, quality, status, stream percentage, output
path or error. Hover a row to read its full URL and result. Failures remain visible
and do not stop subsequent jobs. The counters show Completed, Failed, Cancelled
and Remaining; **batch progress counts terminal items**, separately from the current
video/audio stream's real percentage. It is not a byte-weighted batch estimate.

Remove Pending removes selected pending records; Clear Queue removes all records
only while idle. Neither action deletes media. Cancel Batch marks all not-yet-started
items **Cancelled**; completed files stay untouched, including a file whose atomic
publish has just committed. Closing waits asynchronously for active cancellation
and cleanup (or for a bounded TXT import to finish). Cancelled/failed entries are
not restarted automatically; to retry, clear the idle queue and import a TXT
containing only the desired failed/cancelled URLs. There is no Retry Failed button.

FFmpeg must be detected before Download All starts. Install it manually and use
Check again; no binaries or dependencies are downloaded by this feature. The
original process isolation, bounded retries, safe filenames, per-job temporary
folders, collision suffixes and atomic publication remain in use. Queue state is
not persisted across restarts. TXT import does not verify video availability online;
yt-dlp performs final extraction and validation when each item starts.

Manual UAT: copy `links.example.txt` to `links.txt`, then replace the IDs with 3–5 videos you are permitted
to download, optionally add a duplicate and an invalid line, import, and check the
report/order. Choose Max 720p and a writable folder, start Download All, inspect
progress and the final summary. Repeat with Automatic on a video offering 2160p,
and test Cancel Batch while extracting/downloading/merging. Check that completed
files remain, pending items become Cancelled, and closing exits after cleanup.
Real YouTube downloads and actual FFmpeg merging are **NOT TESTED** in the offline
suite; native Windows checks use mocked transfers/local fixtures only.
