"""Ticket inbox parser for ``bd-track triage``.

An inbox is one project's directory of tickets (see docs/triage.md):

    <inbox>/
      triage/*.md        # one ticket per file
      triage/inbox.md    # quick notes, one ticket per blank-line-separated paragraph
      done/              # handled tickets (not read here)

Parsing is lenient. Every frontmatter key is optional, malformed frontmatter falls back to a
free-form ticket, and a paragraph with no stamp is still a ticket. Nothing here mutates the inbox.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path

import yaml

logger = getLogger(__name__)

TRIAGE_DIR = "triage"
DONE_DIR = "done"
NOTES_FILE = "inbox.md"

TICKET_TYPES = ("bug", "feature", "task", "idea", "question")
URGENCIES = ("low", "normal", "high")

TITLE_LIMIT = 80

# Quick-note stamp written by `ticket --quick`: "2026-10-30 MH (bug, high): text".
# Hand-written notes may carry only the date, or no stamp at all.
_STAMP = re.compile(
    r"""^(?P<date>\d{4}-\d{2}-\d{2})
        (?:[ \t]+(?P<from>[^\s(:]+))?
        (?:[ \t]*\((?P<type>[^,)]*)(?:,[ \t]*(?P<urgency>[^)]*))?\))?
        [ \t]*:[ \t]*""",
    re.VERBOSE,
)
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
_RULE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")


@dataclass
class Ticket:
    """One open or handled ticket, from a file or an ``inbox.md`` paragraph.

    ``id`` is stable across runs: the file stem for a ticket file, and ``inbox-<hash>`` of the
    whitespace-normalised paragraph for a quick note. ``text`` is the raw paragraph for a note,
    so a later step can remove exactly that paragraph from ``inbox.md``.
    """

    id: str
    path: Path
    title: str
    body: str
    meta: dict[str, str] = field(default_factory=dict)
    text: str | None = None

    @property
    def is_note(self) -> bool:
        return self.text is not None

    @property
    def sender(self) -> str | None:
        return self.meta.get("from")

    @property
    def date(self) -> str | None:
        return self.meta.get("date")

    @property
    def status(self) -> str:
        return self.meta.get("status", "open")

    @property
    def type(self) -> str | None:
        """The ticket type if it is a recognised one, else ``None``."""
        value = self.meta.get("type", "").lower()
        return value if value in TICKET_TYPES else None

    @property
    def urgency(self) -> str | None:
        """The urgency if it is a recognised one, else ``None``."""
        value = self.meta.get("urgency", "").lower()
        return value if value in URGENCIES else None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": str(self.path),
            "kind": "note" if self.is_note else "file",
            "title": self.title,
            "from": self.sender,
            "date": self.date,
            "type": self.type,
            "urgency": self.urgency,
            "status": self.status,
            "meta": self.meta,
            "body": self.body,
        }


def _scalar(value: object) -> str:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def split_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Return ``(meta, body)``; ``meta`` is ``{}`` when the frontmatter is absent or unusable.

    Values are stringified (YAML dates become ISO strings); nested or null values are dropped.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        logger.debug("unclosed frontmatter; treating as free-form")
        return {}, content
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as err:
        logger.debug("malformed frontmatter (%s); treating as free-form", err)
        return {}, content
    if data is None:
        data = {}
    if not isinstance(data, dict):
        logger.debug("frontmatter is not a mapping; treating as free-form")
        return {}, content
    meta = {
        str(key): _scalar(value)
        for key, value in data.items()
        if value is not None and not isinstance(value, (dict, list))
    }
    return meta, "\n".join(lines[end + 1 :]).strip("\n")


def _shorten(text: str, limit: int = TITLE_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def file_title(body: str, fallback: str) -> str:
    """The first ``# `` heading, else the first line of text (not a rule), else ``fallback``."""
    lines = [line.strip() for line in body.splitlines()]
    heading = next((line[2:] for line in lines if line.startswith("# ")), None)
    if heading is not None:
        return _shorten(heading)
    texts = (line.lstrip("#").strip() for line in lines if not _RULE.match(line))
    first = next((text for text in texts if text), "")
    return _shorten(first) if first else fallback


def parse_file(path: Path) -> Ticket:
    """Parse one ticket file in ``triage/``."""
    meta, body = split_frontmatter(path.read_text(errors="replace"))
    return Ticket(id=path.stem, path=path, title=file_title(body, path.stem), body=body, meta=meta)


def note_id(text: str) -> str:
    digest = hashlib.sha1(" ".join(text.split()).encode()).hexdigest()
    return f"inbox-{digest[:8]}"


def parse_note(text: str, path: Path) -> Ticket:
    """Parse one ``inbox.md`` paragraph, with or without a leading stamp."""
    body = text.strip()
    meta: dict[str, str] = {}
    stamp = _STAMP.match(body)
    if stamp:
        meta = {key: value.strip() for key, value in stamp.groupdict().items() if value}
        body = body[stamp.end() :].strip()
    return Ticket(
        id=note_id(text),
        path=path,
        title=_shorten(body) or "(empty note)",
        body=body,
        meta=meta,
        text=text,
    )


def parse_notes(path: Path) -> list[Ticket]:
    """Parse ``inbox.md`` into one ticket per paragraph."""
    content = path.read_text(errors="replace").replace("\r\n", "\n")
    paragraphs = [p.strip("\n") for p in _PARAGRAPH_BREAK.split(content) if p.strip()]
    notes = [parse_note(p, path) for p in paragraphs]
    seen: dict[str, int] = {}
    for note in notes:
        # Identical paragraphs hash alike; number the repeats in file order.
        seen[note.id] = seen.get(note.id, 0) + 1
        if seen[note.id] > 1:
            note.id = f"{note.id}-{seen[note.id]}"
    return notes


def read_inbox(inbox: Path, *, include_handled: bool = False) -> list[Ticket]:
    """Return the tickets in ``<inbox>/triage/``: files in name order, then quick notes.

    By default only open tickets are returned; a file whose ``status`` is anything other than
    ``open`` (e.g. one marked handled but not yet moved to ``done/``) is skipped.
    """
    folder = inbox / TRIAGE_DIR
    if not folder.is_dir():
        return []
    tickets: list[Ticket] = []
    notes: list[Ticket] = []
    for path in sorted(folder.glob("*.md")):
        if not path.is_file():
            continue
        if path.name == NOTES_FILE:
            notes = parse_notes(path)
        else:
            tickets.append(parse_file(path))
    tickets.extend(notes)
    if not include_handled:
        tickets = [t for t in tickets if t.status == "open"]
    return tickets
