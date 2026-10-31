"""Tests for triage.py — ticket inbox parser, list and resolve."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bd_track.triage import (
    cmd_triage_ask,
    cmd_triage_interactive,
    cmd_triage_list,
    cmd_triage_resolve,
    find_inbox,
    note_id,
    parse_note,
    parse_notes,
    read_inbox,
    run_interactive,
    set_frontmatter,
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


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    """A writable copy of the fixture inbox."""
    target = tmp_path / "inbox"
    shutil.copytree(FIXTURE, target)
    return target


def _resolve(inbox: Path, ticket_id: str, **kwargs) -> None:
    cmd_triage_resolve(ticket_id, inbox=inbox, project_dir=inbox, **kwargs)


def _done(inbox: Path, name: str) -> tuple[dict[str, str], str]:
    return split_frontmatter((inbox / "done" / name).read_text())


def test_resolve_reject_keeps_other_keys(inbox: Path):
    _resolve(inbox, "2026-10-29-structured", reject="out of scope: not ours")
    assert not (inbox / "triage" / "2026-10-29-structured.md").exists()
    meta, body = _done(inbox, "2026-10-29-structured.md")
    assert meta["status"] == "rejected"
    assert meta["outcome"] == "out of scope: not ours"  # quoted, round-trips
    assert meta["resolved"] == dt.date.today().isoformat()
    assert meta["from"] == "claude@chezmoi"
    assert meta["urgency"] == "high"
    assert body.startswith("# Structured ticket")


def test_resolve_replaces_existing_status_line(inbox: Path):
    _resolve(inbox, "2026-10-29-structured.md", done="docs/x.md")
    text = (inbox / "done" / "2026-10-29-structured.md").read_text()
    assert text.count("status:") == 1
    assert "status: done" in text


def test_resolve_freeform_gets_frontmatter_block(inbox: Path):
    _resolve(inbox, "freeform-oneline", duplicate="bd-timew-abc")
    meta, body = _done(inbox, "freeform-oneline.md")
    assert meta["status"] == "duplicate"
    assert meta["outcome"] == "duplicate of bd-timew-abc"
    assert body == "Show A/C icon on bar when laptop is plugged in"


def test_resolve_malformed_frontmatter_prepends_block(inbox: Path):
    _resolve(inbox, "malformed-frontmatter", reject="no")
    meta, body = _done(inbox, "malformed-frontmatter.md")
    assert meta["status"] == "rejected"
    assert "from: [unclosed" in body  # original text kept below the new block


def test_resolve_note_writes_own_file_and_leaves_others(inbox: Path):
    note = parse_notes(inbox / "triage" / "inbox.md")[0]
    _resolve(inbox, note.id, reject="cannot reproduce")
    meta, body = _done(inbox, "2026-10-30-waybar-clock-drifts-after-resume.md")
    assert meta == {
        "from": "MH",
        "date": "2026-10-30",
        "type": "bug",
        "urgency": "high",
        "status": "rejected",
        "resolved": dt.date.today().isoformat(),
        "outcome": "cannot reproduce",
    }
    assert body == "waybar clock drifts after resume?"
    left = parse_notes(inbox / "triage" / "inbox.md")
    assert note.id not in [t.id for t in left]
    assert len(left) == 4
    assert left[1].body == "unstamped note\nthat spans two lines"


def test_resolve_repeated_note_removes_one(inbox: Path):
    _resolve(inbox, parse_notes(inbox / "triage" / "inbox.md")[4].id, reject="dup")
    bodies = [t.body for t in parse_notes(inbox / "triage" / "inbox.md")]
    assert bodies.count("same text twice") == 1


def test_resolve_last_note_deletes_inbox_md(inbox: Path):
    path = inbox / "triage" / "inbox.md"
    path.write_text("only note\n")
    _resolve(inbox, note_id("only note"), done="fixed")
    assert not path.exists()
    assert len(list((inbox / "done").glob("*-only-note.md"))) == 1


def test_resolve_undated_note_uses_today(inbox: Path):
    _resolve(inbox, parse_notes(inbox / "triage" / "inbox.md")[2].id, done="ok")
    today = dt.date.today().isoformat()
    assert (inbox / "done" / f"{today}-unstamped-note-that-spans-two-lines.md").exists()


def test_resolve_refuses_to_overwrite_done(inbox: Path):
    (inbox / "done" / "freeform-oneline.md").write_text("existing\n")
    with pytest.raises(SystemExit, match="already exists"):
        _resolve(inbox, "freeform-oneline", reject="x")
    assert (inbox / "triage" / "freeform-oneline.md").exists()
    assert (inbox / "done" / "freeform-oneline.md").read_text() == "existing\n"


def test_resolve_unknown_or_handled_ticket(inbox: Path):
    with pytest.raises(SystemExit, match="no open ticket"):
        _resolve(inbox, "nope", reject="x")
    with pytest.raises(SystemExit, match="no open ticket"):
        _resolve(inbox, "handled-not-moved", reject="x")


def test_resolve_dry_run_writes_nothing(inbox: Path, capsys):
    before = sorted(p.relative_to(inbox) for p in inbox.rglob("*"))
    _resolve(inbox, "2026-10-29-structured", bead=True, dry_run=True)
    _resolve(inbox, "freeform-oneline", reject="x", dry_run=True)
    assert sorted(p.relative_to(inbox) for p in inbox.rglob("*")) == before
    out = capsys.readouterr().out
    assert (
        "would create bead: 'Structured ticket with full frontmatter' (type feature, priority 1"
        in out
    )
    assert "would move freeform-oneline" in out


def _opts(args: list[str]) -> dict[str, str]:
    """``--flag value`` pairs of a ``bd create`` call (between ``create`` and ``--silent``)."""
    assert args[-1] == "--silent"
    return dict(zip(args[2:-1:2], args[3:-1:2], strict=True))


def _bd_ok(stdout: str = "bd-x-1\n"):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def test_resolve_bead_creates_and_records(inbox: Path, capsys):
    with patch("bd_track.triage.subprocess.run", return_value=_bd_ok()) as run:
        _resolve(inbox, "2026-10-29-structured", bead=True)
    args = run.call_args.args[0]
    assert args[:2] == ["bd", "create"]
    opts = _opts(args)
    assert opts["--title"] == "Structured ticket with full frontmatter"
    assert opts["--type"] == "feature"
    assert opts["--priority"] == "1"
    assert opts["--labels"] == "triage"
    assert opts["--description"].endswith(
        "ticket: done/2026-10-29-structured.md (from claude@chezmoi, 2026-10-29)"
    )
    assert run.call_args.kwargs["cwd"] == inbox
    meta, _ = _done(inbox, "2026-10-29-structured.md")
    assert (meta["status"], meta["outcome"]) == ("accepted", "bead bd-x-1")
    assert capsys.readouterr().out.strip().endswith("done/2026-10-29-structured.md")


def test_resolve_bead_overrides_and_defaults(inbox: Path):
    note = parse_notes(inbox / "triage" / "inbox.md")[2]  # no stamp: task, P2
    with patch("bd_track.triage.subprocess.run", return_value=_bd_ok()) as run:
        _resolve(inbox, note.id, bead=True)
    opts = _opts(run.call_args.args[0])
    assert (opts["--type"], opts["--priority"]) == ("task", "2")
    assert opts["--description"].endswith(".md")  # no "(from ...)" without a stamp

    with patch("bd_track.triage.subprocess.run", return_value=_bd_ok()) as run:
        _resolve(
            inbox,
            "freeform-oneline",
            bead=True,
            title="T",
            description="D",
            bead_type="chore",
            priority=4,
        )
    opts = _opts(run.call_args.args[0])
    assert (opts["--title"], opts["--type"], opts["--priority"]) == ("T", "chore", "4")
    assert opts["--description"] == "D\n\nticket: done/freeform-oneline.md"


def test_resolve_bead_failure_leaves_ticket(inbox: Path):
    failed = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
    with (
        patch("bd_track.triage.subprocess.run", return_value=failed),
        pytest.raises(SystemExit, match="boom"),
    ):
        _resolve(inbox, "freeform-oneline", bead=True)
    assert (inbox / "triage" / "freeform-oneline.md").exists()
    assert not (inbox / "done" / "freeform-oneline.md").exists()


# ---------------------------------------------------------------------------
# find_inbox
# ---------------------------------------------------------------------------


def test_find_inbox_precedence(tmp_path: Path, monkeypatch):
    project = tmp_path / "myproj"
    project.mkdir()
    assert find_inbox(tmp_path / "x", project) == tmp_path / "x"
    monkeypatch.setenv("BD_TRACK_INBOX_ROOT", str(tmp_path / "root"))
    assert find_inbox(None, project) == tmp_path / "root" / "myproj"
    monkeypatch.delenv("BD_TRACK_INBOX_ROOT")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert find_inbox(None, project) == tmp_path / "state" / "triage" / "myproj"


def test_set_frontmatter_dates_bare_other_values_quoted():
    text = set_frontmatter("body\n", {"resolved": "2026-10-30", "outcome": "a: b"})
    assert "resolved: 2026-10-30\n" in text
    assert split_frontmatter(text)[0] == {"resolved": "2026-10-30", "outcome": "a: b"}


def test_resolve_keeps_file_modes(inbox: Path):
    ticket = inbox / "triage" / "freeform-oneline.md"
    notes = inbox / "triage" / "inbox.md"
    ticket.chmod(0o644)
    notes.chmod(0o640)
    _resolve(inbox, "freeform-oneline", reject="x")
    _resolve(inbox, parse_notes(notes)[0].id, reject="x")
    assert (inbox / "done" / "freeform-oneline.md").stat().st_mode & 0o777 == 0o644
    assert notes.stat().st_mode & 0o777 == 0o640
    note_done = inbox / "done" / "2026-10-30-waybar-clock-drifts-after-resume.md"
    assert note_done.stat().st_mode & 0o777 == 0o640


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_json_contract(capsys):
    cmd_triage_list(inbox=FIXTURE, as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert data["inbox"] == str(FIXTURE.resolve())
    tickets = {t["id"]: t for t in data["tickets"]}
    assert len(tickets) == 12  # 7 files + 5 notes; handled file excluded
    assert set(tickets["2026-10-29-structured"]) == {
        "id",
        "path",
        "source",
        "kind",
        "title",
        "from",
        "date",
        "type",
        "urgency",
        "status",
        "meta",
        "body",
    }
    note = tickets[note_id("2026-10-30 MH (bug, high): waybar clock drifts after resume?")]
    assert (note["kind"], note["source"], note["type"]) == ("note", "inbox.md", "bug")
    assert Path(note["path"]).is_absolute()
    partial = tickets["partial-frontmatter"]
    assert (partial["type"], partial["meta"]["type"]) == (None, "Chore")


def test_list_table(capsys):
    cmd_triage_list(inbox=FIXTURE)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 12
    assert lines[0].split()[:4] == [
        "2026-10-29-structured",
        "2026-10-29",
        "claude@chezmoi",
        "feature/high",
    ]
    assert lines[0].endswith("Structured ticket with full frontmatter")
    assert lines[1].split()[:4] == ["empty", "-", "-", "-"]
    starts = {line.index(line.split()[-1]) for line in lines if line.endswith("twice")}
    assert len(starts) == 1  # columns aligned


def test_list_empty_inbox(tmp_path: Path, capsys):
    cmd_triage_list(inbox=tmp_path)
    assert capsys.readouterr().out == ""
    cmd_triage_list(inbox=tmp_path, as_json=True)
    assert json.loads(capsys.readouterr().out)["tickets"] == []


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------


def test_ask_file_appends_section_and_stays_open(inbox: Path):
    cmd_triage_ask("heading-only", "Which bar? (MH)", inbox=inbox)
    text = (inbox / "triage" / "heading-only.md").read_text()
    assert text.endswith(f"Body.\n\n## Triage {dt.date.today().isoformat()}\n\nWhich bar? (MH)\n")
    assert "heading-only" in [t.id for t in read_inbox(inbox)]


def test_ask_note_promotes_to_file(inbox: Path):
    note = parse_notes(inbox / "triage" / "inbox.md")[0]
    cmd_triage_ask(note.id, "repro steps?", inbox=inbox)
    promoted = inbox / "triage" / "2026-10-30-waybar-clock-drifts-after-resume.md"
    meta, body = split_frontmatter(promoted.read_text())
    assert meta == {
        "from": "MH",
        "date": "2026-10-30",
        "type": "bug",
        "urgency": "high",
        "status": "open",
    }
    assert body.startswith("waybar clock drifts after resume?\n\n## Triage ")
    assert note.id not in [t.id for t in parse_notes(inbox / "triage" / "inbox.md")]
    assert promoted.stem in [t.id for t in read_inbox(inbox)]


def test_ask_dry_run_writes_nothing(inbox: Path, capsys):
    before = {p: p.read_bytes() for p in inbox.rglob("*") if p.is_file()}
    cmd_triage_ask("heading-only", "q", inbox=inbox, dry_run=True)
    cmd_triage_ask(parse_notes(inbox / "triage" / "inbox.md")[0].id, "q", inbox=inbox, dry_run=True)
    assert {p: p.read_bytes() for p in inbox.rglob("*") if p.is_file()} == before
    assert "promote it to its own file" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# interactive loop
# ---------------------------------------------------------------------------


def _scripted(answers: list[str]):
    """An ``ask`` stand-in that returns ``answers`` in order and records each prompt."""
    prompts: list[str] = []
    queue = iter(answers)

    def ask(message: str, *, choices=None, default=None) -> str:
        prompts.append(message)
        answer = next(queue)
        if choices is not None:
            assert answer in choices
        return answer

    return ask, prompts


def test_interactive_walks_and_acts(inbox: Path):
    ask, _ = _scripted(
        [
            "r",
            "not ours",  # 2026-10-29-structured
            "s",  # empty
            "d",
            "bd-timew-abc",  # freeform-oneline
            "k",
            "which bar?",  # heading-only
            "a",
            "Malformed ticket",
            "bug",
            "1",  # malformed-frontmatter
            "q",
        ]
    )
    with patch("bd_track.triage.subprocess.run", return_value=_bd_ok()) as run:
        counts = run_interactive(inbox, project_dir=inbox, dry_run=False, ask=ask)
    assert counts == {"accept": 1, "reject": 1, "duplicate": 1, "skip": 1, "ask": 1}
    done = {p.name: split_frontmatter(p.read_text())[0] for p in (inbox / "done").glob("*.md")}
    assert done["2026-10-29-structured.md"]["status"] == "rejected"
    assert done["freeform-oneline.md"]["outcome"] == "duplicate of bd-timew-abc"
    assert done["malformed-frontmatter.md"]["outcome"] == "bead bd-x-1"
    opts = _opts(run.call_args.args[0])
    assert (opts["--title"], opts["--type"], opts["--priority"]) == ("Malformed ticket", "bug", "1")
    assert "## Triage" in (inbox / "triage" / "heading-only.md").read_text()
    assert (inbox / "triage" / "empty.md").exists()


def test_interactive_note_ids_stay_current(inbox: Path):
    (inbox / "triage").joinpath("inbox.md").write_text("same\n\nsame\n\nother\n")
    for path in (inbox / "triage").glob("*.md"):
        if path.name != "inbox.md":
            path.unlink()
    ask, _ = _scripted(["r", "x", "r", "y", "s"])
    counts = run_interactive(inbox, project_dir=inbox, dry_run=False, ask=ask)
    assert counts["reject"] == 2 and counts["skip"] == 1
    assert [t.body for t in read_inbox(inbox)] == ["other"]


def test_interactive_ask_on_note_does_not_repeat(inbox: Path):
    (inbox / "triage").joinpath("inbox.md").write_text("only note\n")
    for path in (inbox / "triage").glob("*.md"):
        if path.name != "inbox.md":
            path.unlink()
    ask, _ = _scripted(["k", "why?"])  # a repeat would exhaust the script
    counts = run_interactive(inbox, project_dir=inbox, dry_run=False, ask=ask)
    assert counts["ask"] == 1
    assert [t.id for t in read_inbox(inbox)] == [f"{dt.date.today().isoformat()}-only-note"]


def test_interactive_dry_run_writes_nothing_and_ends(inbox: Path, capsys):
    before = {p: p.read_bytes() for p in inbox.rglob("*") if p.is_file()}
    ask, _ = _scripted(["r", "x", "a", "T", "task", "2", "k", "q?"] + ["s"] * 9)
    with patch("bd_track.triage.subprocess.run") as run:
        counts = run_interactive(inbox, project_dir=inbox, dry_run=True, ask=ask)
    run.assert_not_called()
    assert sum(counts.values()) == 12  # every open ticket shown once, then the loop ends
    assert {p: p.read_bytes() for p in inbox.rglob("*") if p.is_file()} == before
    assert "would create bead: 'T'" in capsys.readouterr().out


def test_interactive_requires_tty(inbox: Path):
    with (
        patch("bd_track.triage.is_interactive", return_value=False),
        pytest.raises(SystemExit, match="needs a terminal"),
    ):
        cmd_triage_interactive(inbox=inbox)
