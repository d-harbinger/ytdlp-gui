"""Behavior tests for the pure functions in transcript.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _harness import case, run  # noqa: E402
import transcript as T  # noqa: E402


def _snips(*triples):
    return [T.TranscriptSnippet(text=t, start=s, duration=d) for (t, s, d) in triples]


@case
def parse_json3_basic():
    raw = (
        '{"events":['
        '{"tStartMs":1000,"dDurationMs":2000,"segs":[{"utf8":"Hello"},{"utf8":" world"}]},'
        '{"tStartMs":3000,"segs":[]},'           # no text -> skipped
        '{"tStartMs":4000,"dDurationMs":500}'    # no segs key -> skipped
        ']}'
    )
    out = T.parse_json3_captions(raw)
    assert len(out) == 1, out
    assert out[0].text == "Hello world"
    assert out[0].start == 1.0
    assert out[0].duration == 2.0


@case
def parse_json3_strips_newlines():
    raw = '{"events":[{"tStartMs":0,"dDurationMs":1000,"segs":[{"utf8":"a\\nb"}]}]}'
    out = T.parse_json3_captions(raw)
    assert out[0].text == "a b", repr(out[0].text)


@case
def fmt_ts_minutes_and_hours():
    assert T.fmt_ts(5) == "0:05"
    assert T.fmt_ts(65) == "1:05"
    assert T.fmt_ts(3665) == "1:01:05"


@case
def safe_filename_replaces_specials():
    assert T.safe_filename("a/b:c") == "a_b_c"
    assert T.safe_filename("  spaced  ") == "spaced"
    assert T.safe_filename("dots...") == "dots"


@case
def yaml_str_escapes_quotes_and_newlines():
    assert T.yaml_str('he said "hi"') == 'he said \\"hi\\"'
    assert T.yaml_str("a\nb") == "a b"
    assert T.yaml_str("back\\slash") == "back\\\\slash"


@case
def build_body_flat_with_and_without_ts():
    data = _snips(("one", 0.0, 1.0), ("two", 65.0, 1.0))
    assert T.build_body_flat(data, include_ts=False) == "one two"
    assert T.build_body_flat(data, include_ts=True) == "[0:00] one\n[1:05] two"


@case
def build_body_paragraphs_groups_small_run():
    data = _snips(("a", 0.0, 1.0), ("b", 1.0, 1.0), ("c", 2.0, 1.0))
    # under 5 snippets and under 30s gap -> single paragraph
    assert T.build_body_paragraphs(data, include_ts=False) == "a b c"


@case
def apply_range_dash_and_list():
    entries = [("v1", "t1"), ("v2", "t2"), ("v3", "t3"), ("v4", "t4")]
    assert T.apply_range(entries, "1-2") == [("v1", "t1"), ("v2", "t2")]
    assert T.apply_range(entries, "1,3") == [("v1", "t1"), ("v3", "t3")]
    assert T.apply_range(entries, "3-") == [("v3", "t3"), ("v4", "t4")]
    # colon slice syntax is intentionally skipped by apply_range
    assert T.apply_range(entries, "1:2") == []


@case
def format_single_plain_is_flat_body():
    data = _snips(("hello", 0.0, 1.0), ("there", 1.0, 1.0))
    out = T.format_single_transcript("plain", False, "vid123", "My Title", data)
    assert out == "hello there", out


@case
def format_single_markdown_has_header_markers():
    data = _snips(("hello", 0.0, 1.0))
    out = T.format_single_transcript("markdown", False, "vid123", "My Title", data)
    assert out.startswith("# My Title")
    assert "vid123" in out
    assert "https://youtube.com/watch?v=vid123" in out


if __name__ == "__main__":
    run()
