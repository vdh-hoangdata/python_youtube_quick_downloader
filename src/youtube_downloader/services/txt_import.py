"""Bounded, offline TXT parsing. Input is data, never executable content."""
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from youtube_downloader.utils.urls import normalize_url

MAX_TXT_BYTES = 1024 * 1024
MAX_LINES = 5000
MAX_QUEUE_ITEMS = 5000


def video_key(value: str) -> str:
    url = normalize_url(value)
    parts = urlsplit(url)
    segments = parts.path.strip('/').split('/')
    if parts.hostname == 'youtu.be' and len(segments) == 1:
        video_id = segments[0]
    elif segments[0] == 'watch' and len(segments) == 1:
        video_id = parse_qs(parts.query).get('v', [''])[0]
    elif len(segments) == 2 and segments[0] in {'shorts', 'embed', 'live', 'v'}:
        video_id = segments[1]
    else:
        raise ValueError('Unsupported URL: use a single video, not a playlist or channel.')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', video_id):
        raise ValueError('Invalid video ID or multiple URLs on one line.')
    return video_id


@dataclass(frozen=True)
class ImportResult:
    urls: tuple[str, ...]
    duplicates: int
    invalid: tuple[str, ...]


def parse_txt(path: Path, existing: frozenset[str] = frozenset()) -> ImportResult:
    with path.open('rb') as stream:
        data = stream.read(MAX_TXT_BYTES + 1)
    if len(data) > MAX_TXT_BYTES:
        raise ValueError('TXT exceeds the 1 MiB limit.')
    lines = data.decode('utf-8-sig').splitlines()
    if len(lines) > MAX_LINES:
        raise ValueError('TXT exceeds the 5,000 line limit.')
    urls, invalid = [], []
    seen = set(existing)
    duplicates = 0
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            url = normalize_url(line)
            key = video_key(url)
        except ValueError as exc:
            invalid.append(f'Line {line_number}: {line.strip()} — {exc}')
            continue
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
            urls.append(url)
    if len(existing) + len(urls) > MAX_QUEUE_ITEMS:
        raise ValueError('Queue would exceed the 5,000 item limit. Clear or remove items first.')
    return ImportResult(tuple(urls), duplicates, tuple(invalid))
