"""Pure input validators shared by the UI shell and both controllers.

Lives in its own module because the downloader and transcript controllers both
need the playlist-range validator and must never import each other — a dependency
shared by two peers belongs in a neutral third module.
"""
import os
import re
import urllib.parse

# ── URL validation ──
ALLOWED_SCHEMES = ("http", "https")
YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "youtu.be",
    "m.youtube.com", "music.youtube.com",
    "www.youtube-nocookie.com",
})
YOUTUBE_ONLY = os.environ.get("YTDLP_GUI_YOUTUBE_ONLY", "0") == "1"


def validate_url(url: str) -> tuple[bool, str]:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False, "Could not parse URL."
    if parsed.scheme not in ALLOWED_SCHEMES:
        return False, f"Blocked scheme '{parsed.scheme}://'. Only http/https allowed."
    if not parsed.hostname:
        return False, "URL has no hostname."
    if YOUTUBE_ONLY:
        host = parsed.hostname.lower().lstrip(".")
        if host not in YOUTUBE_HOSTS:
            return False, (
                f"Host '{host}' not in allowed YouTube domains. "
                "Unset YTDLP_GUI_YOUTUBE_ONLY to allow all sites."
            )
    return True, ""


# ── Playlist-range validation ──
_PLAYLIST_RANGE_RE = re.compile(r'^[\d,\-:\s]+$')


def validate_playlist_range(rng: str) -> bool:
    return bool(_PLAYLIST_RANGE_RE.fullmatch(rng))
