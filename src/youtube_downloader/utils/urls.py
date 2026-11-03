from urllib.parse import urlsplit, urlunsplit


def normalize_url(value: str) -> str:
    """Accept YouTube hosts and leave video-specific validation to yt-dlp."""
    value = value.strip()
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Paste a valid YouTube video URL.")
    if "://" not in value:
        value = f"https://{value}"
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError as exc:
        raise ValueError("Paste a valid YouTube video URL.") from exc
    if parts.scheme.lower() not in {"http", "https"} or parts.username or parts.password:
        raise ValueError("Use an HTTP or HTTPS YouTube video URL.")
    if not (host in {"youtube.com", "youtu.be", "youtube-nocookie.com"}
            or host.endswith(".youtube.com") or host.endswith(".youtube-nocookie.com")):
        raise ValueError("Use a youtube.com or youtu.be video link.")
    if port not in {None, 80, 443}:
        raise ValueError("Use a standard YouTube video URL without a custom port.")
    if not parts.path or parts.path == "/":
        raise ValueError("Paste a link to a single YouTube video.")
    # Upgrade normal YouTube links to HTTPS; retain query parameters and fragments.
    return urlunsplit(("https", host, parts.path, parts.query, parts.fragment))

