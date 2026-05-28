"""Behavior tests for downloader.py pure parts (parse_rate + build_ydl_opts)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _harness import case, run  # noqa: E402
import downloader as D  # noqa: E402


def _req(**over):
    base = dict(
        url="https://example.com/v", output_dir="/tmp/out", mode="video",
        video_format="bv*+ba/b", audio_codec="mp3", audio_quality="192",
        playlist_range="", subs=False, subs_lang="en", subs_auto=True, subs_embed=True,
        thumb=False, thumb_embed=False, meta=False, chapters_split=False,
        sb=False, sb_action="skip", sb_categories=[], rate_limit="No limit",
        cookie_browser="-- none --", archive=False,
    )
    base.update(over)
    return D.DownloadRequest(**base)


def _opts(**over):
    logs = []
    o = D.build_ydl_opts(_req(**over), on_log=logs.append)
    return o, logs


@case
def parse_rate_units():
    assert D.parse_rate("5M") == 5 * 1048576
    assert D.parse_rate("500K") == 500 * 1024
    assert D.parse_rate("2G") == 2 * 1073741824
    assert D.parse_rate("7") == 7
    assert D.parse_rate("nonsense") is None


@case
def video_mode_basics():
    o, _ = _opts(mode="video", video_format="bv*+ba/b")
    assert o["format"] == "bv*+ba/b"
    assert o["paths"] == {"home": "/tmp/out"}
    assert o["outtmpl"]["default"] == "%(title)s [%(id)s].%(ext)s"
    assert o["noplaylist"] is True
    assert o["merge_output_format"] == "mp4"
    assert o["max_downloads"] == D.MAX_PLAYLIST_DOWNLOADS
    assert o["postprocessors"] == []


@case
def audio_mp3_adds_quality_and_drops_merge():
    o, _ = _opts(mode="audio", audio_codec="mp3", audio_quality="320")
    assert o["format"] == "bestaudio/best"
    assert "merge_output_format" not in o
    pp = o["postprocessors"][0]
    assert pp["key"] == "FFmpegExtractAudio"
    assert pp["preferredcodec"] == "mp3"
    assert pp["preferredquality"] == "320"


@case
def audio_flac_omits_quality():
    o, _ = _opts(mode="audio", audio_codec="flac")
    pp = o["postprocessors"][0]
    assert pp["preferredcodec"] == "flac"
    assert "preferredquality" not in pp


@case
def playlist_valid_range_sets_items():
    o, _ = _opts(mode="playlist", playlist_range="1-5")
    assert o["noplaylist"] is False
    assert o["playlist_items"] == "1-5"
    assert "%(playlist_index)03d" in o["outtmpl"]["default"]


@case
def playlist_invalid_range_warns_and_skips():
    o, logs = _opts(mode="playlist", playlist_range="bad;range")
    assert "playlist_items" not in o
    assert any("Invalid playlist range" in m for m in logs), logs


@case
def subs_options():
    o, _ = _opts(subs=True, subs_lang="es", subs_auto=True, subs_embed=True)
    assert o["writesubtitles"] is True
    assert o["subtitleslangs"] == ["es"]
    assert o["writeautomaticsub"] is True
    assert {"key": "FFmpegEmbedSubtitle"} in o["postprocessors"]


@case
def sponsorblock_remove_adds_two_pps():
    o, _ = _opts(sb=True, sb_action="remove", sb_categories=["sponsor", "intro"])
    keys = [pp["key"] for pp in o["postprocessors"]]
    assert "SponsorBlock" in keys and "ModifyChapters" in keys


@case
def rate_and_cookies_and_archive():
    o, _ = _opts(rate_limit="2M", cookie_browser="firefox", archive=True)
    assert o["ratelimit"] == 2 * 1048576
    assert o["cookiesfrombrowser"] == ("firefox",)
    assert o["download_archive"] == os.path.join("/tmp/out", ".ytdlp_archive.txt")


if __name__ == "__main__":
    run()
