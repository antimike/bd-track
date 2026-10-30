"""Tests for triage.py — lenient ticket inbox parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bd_track.triage import (
    note_id,
    parse_note,
    parse_notes,
    read_inbox,
    split_frontmatter,
)

FIXTURE = Path(__file__).parent / "fixtures" / "triage_inbox"


@pytest.fixture
def tickets() -> dict:
    return {t.id: t for t in read_inbox(FIXTURE, include_handled=True)}


# ---------------------------------------------------------------------------
# read_inbox over the fixture
# ---------------------------------------------------------------------------


def test_read_inbox_open_only_by_default():
    ids = [t.id for t in read_inbox(FIXTURE)]
    assert "handled-not-moved" not in ids
    assert "2026-10-01-old" not in ids  # done/ is never read
    assert "notes" not in ids  # only *.md


def test_read_inbox_files_sorted_then_notes():
    found = read_inbox(FIXTURE)
    files = [t.id for t in found if not t.is_note]
    assert files == sorted(files)
    assert len(files) == 7
    kinds = [t.is_note for t in found]
    assert kinds == sorted(kinds)  # all files before all notes
    assert sum(kinds) == 5


def test_read_inbox_include_handled(tickets):
    assert tickets["handled-not-moved"].status == "done"
    assert tickets["handled-not-moved"].meta["outcome"] == "bead bd-timew-xyz"


def test_read_inbox_missing_dir(tmp_path: Path):
    assert read_inbox(tmp_path) == []


def test_structured_ticket(tickets):
    t = tickets["2026-10-29-structured"]
    assert t.title == "Structured ticket with full frontmatter"
    assert (t.sender, t.date, t.type, t.urgency, t.status) == (
        "claude@chezmoi",
        "2026-10-29",
        "feature",
        "high",
        "open",
    )
    assert t.body.startswith("# Structured ticket")
    assert "## Done when" in t.body
    assert not t.is_note


def test_freeform_oneline(tickets):
    t = tickets["freeform-oneline"]
    assert t.title == "Show A/C icon on bar when laptop is plugged in"
    assert t.meta == {}
    assert t.status == "open"
    assert t.date is None


def test_heading_beats_first_line(tickets):
    assert tickets["heading-only"].title == "Heading wins over the first line"


def test_partial_frontmatter_unknown_values(tickets):
    t = tickets["partial-frontmatter"]
    assert t.sender == "MH"
    assert t.type is None  # "Chore" is not a recognised type
    assert t.urgency is None
    assert t.meta["type"] == "Chore"  # raw value kept
    assert t.title == "Partial frontmatter, unknown type and urgency"


def test_malformed_frontmatter_is_freeform(tickets):
    t = tickets["malformed-frontmatter"]
    assert t.meta == {}
    assert t.title == "Malformed frontmatter falls back to free-form"
    assert t.body.startswith("---")


def test_unclosed_frontmatter_is_freeform(tickets):
    t = tickets["unclosed-frontmatter"]
    assert t.meta == {}
    assert t.title == "from: MH"  # first line of text, skipping the rule


def test_empty_file_falls_back_to_stem(tickets):
    assert tickets["empty"].title == "empty"


def test_to_dict_is_json_ready(tickets):
    data = json.loads(json.dumps(tickets["2026-10-29-structured"].to_dict()))
    assert data["kind"] == "file"
    assert data["type"] == "feature"


# ---------------------------------------------------------------------------
# inbox.md notes
# ---------------------------------------------------------------------------


def test_notes_full_stamp():
    notes = parse_notes(FIXTURE / "triage" / "inbox.md")
    t = notes[0]
    assert t.meta == {"date": "2026-10-30", "from": "MH", "type": "bug", "urgency": "high"}
    assert t.title == "waybar clock drifts after resume?"
    assert t.text == "2026-10-30 MH (bug, high): waybar clock drifts after resume?"
    assert t.is_note


def test_notes_date_only_stamp():
    t = parse_notes(FIXTURE / "triage" / "inbox.md")[1]
    assert t.meta == {"date": "2026-10-28"}
    assert t.body == "hand-stamped note with a date only"


def test_notes_unstamped_multiline():
    t = parse_notes(FIXTURE / "triage" / "inbox.md")[2]
    assert t.meta == {}
    assert t.body == "unstamped note\nthat spans two lines"
    assert t.title == "unstamped note that spans two lines"


def test_notes_repeated_text_gets_distinct_ids():
    a, b = parse_notes(FIXTURE / "triage" / "inbox.md")[3:]
    assert a.id != b.id
    assert b.id == f"{a.id}-2"


def test_note_id_stable_across_whitespace():
    assert note_id("a  b\nc") == note_id("a b c")
    assert note_id("a b c").startswith("inbox-")


def test_notes_crlf_and_whitespace_only_separator(tmp_path: Path):
    path = tmp_path / "inbox.md"
    path.write_bytes(b"first\r\n \r\nsecond\r\n")
    assert [t.body for t in parse_notes(path)] == ["first", "second"]


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-30 fix the thing: details",  # words before the colon are not a sender
        "2026-10-30 waybar broke",  # no colon
        "10/30/2026: not ISO",
    ],
)
def test_note_non_stamps_stay_freeform(text: str, tmp_path: Path):
    t = parse_note(text, tmp_path / "inbox.md")
    assert t.meta == {}
    assert t.body == text


def test_note_stamp_type_without_urgency(tmp_path: Path):
    t = parse_note("2026-10-30 MH (task): do it", tmp_path / "inbox.md")
    assert t.meta == {"date": "2026-10-30", "from": "MH", "type": "task"}
    assert t.body == "do it"


# ---------------------------------------------------------------------------
# split_frontmatter
# ---------------------------------------------------------------------------


def test_frontmatter_empty_block():
    assert split_frontmatter("---\n---\nbody") == ({}, "body")


def test_frontmatter_non_mapping_is_freeform():
    text = "---\n- a\n- b\n---\nbody"
    assert split_frontmatter(text) == ({}, text)


def test_frontmatter_drops_nested_and_null():
    meta, _ = split_frontmatter("---\nfrom: MH\nextra: {a: 1}\nlist: [1]\nnone:\n---\n")
    assert meta == {"from": "MH"}
