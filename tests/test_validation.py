"""Behavior tests for validation.py (URL + playlist-range validators)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _harness import case, run  # noqa: E402
import validation  # noqa: E402


@case
def http_and_https_pass():
    ok, msg = validation.validate_url("https://youtube.com/watch?v=abc")
    assert ok is True, (ok, msg)
    assert msg == "", msg
    ok, _ = validation.validate_url("http://example.com/x")
    assert ok is True


@case
def non_http_scheme_blocked():
    ok, msg = validation.validate_url("ftp://example.com/x")
    assert ok is False
    assert "ftp" in msg and "http/https" in msg, msg


@case
def missing_scheme_blocked():
    ok, msg = validation.validate_url("notaurl")
    assert ok is False
    assert "Blocked scheme" in msg, msg


@case
def scheme_but_no_host_blocked():
    ok, msg = validation.validate_url("https://")
    assert ok is False
    assert "no hostname" in msg, msg


@case
def range_accepts_dash_comma_colon():
    assert validation.validate_playlist_range("1-10") is True
    assert validation.validate_playlist_range("1,3,5") is True
    assert validation.validate_playlist_range("1:5") is True
    assert validation.validate_playlist_range("5-") is True


@case
def range_rejects_letters_and_empty():
    assert validation.validate_playlist_range("abc") is False
    assert validation.validate_playlist_range("") is False
    assert validation.validate_playlist_range("1;2") is False


if __name__ == "__main__":
    run()
