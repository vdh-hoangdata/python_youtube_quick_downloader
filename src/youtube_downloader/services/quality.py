from youtube_downloader.services.models import Quality


def format_selector(quality: Quality) -> str:
    """Slash alternatives are selection fallbacks, not download-error retries."""
    if quality == Quality.AUTOMATIC:
        return (
            "bestvideo+bestaudio/"
            "bestvideo[height<=1080]+bestaudio/"
            "bestvideo[height<=720]+bestaudio/best"
        )
    height = {Quality.MAX_1080: 1080, Quality.MAX_720: 720}[quality]
    return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"

