"""Ticket inbox parser and resolver for ``bd-track triage``.

An inbox is one project's directory of tickets (see docs/triage.md):

    <inbox>/
      triage/*.md        # one ticket per file
      triage/inbox.md    # quick notes, one ticket per blank-line-separated paragraph
      done/              # handled tickets (never read when listing)

Parsing is lenient. Every frontmatter key is optional, malformed frontmatter falls back to a
free-form ticket, and a paragraph with no stamp is still a ticket.

``resolve_ticket`` is the only writer: it records a decision in the ticket's frontmatter
(``status``, ``resolved``, ``outcome``) and moves the ticket to ``done/``, optionally creating a
bead first.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from bd_track.util import is_interactive, root_log

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
            "source": self.path.name,
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


def _frontmatter(lines: list[str]) -> tuple[dict, int] | None:
    """Parse a leading ``---`` block: ``(data, closing line index)``, or ``None`` if unusable."""
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        root_log.debug("unclosed frontmatter; treating as free-form")
        return None
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as err:
        root_log.debug("malformed frontmatter (%s); treating as free-form", err)
        return None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        root_log.debug("frontmatter is not a mapping; treating as free-form")
        return None
    return data, end


def split_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Return ``(meta, body)``; ``meta`` is ``{}`` when the frontmatter is absent or unusable.

    Values are stringified (YAML dates become ISO strings); nested or null values are dropped.
    """
    lines = content.splitlines()
    parsed = _frontmatter(lines)
    if parsed is None:
        return {}, content
    data, end = parsed
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


# ---------------------------------------------------------------------------
# Locating an inbox
# ---------------------------------------------------------------------------


def inbox_root() -> Path:
    """``$BD_TRACK_INBOX_ROOT``, else ``$XDG_STATE_HOME/triage`` (``~/.local/state/triage``)."""
    explicit = os.environ.get("BD_TRACK_INBOX_ROOT")
    if explicit:
        return Path(explicit).expanduser()
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state).expanduser() / "triage"


def project_root(project_dir: Path | None = None) -> Path:
    """``project_dir``, else the git top level of the working directory, else the cwd."""
    if project_dir is not None:
        return project_dir.expanduser().resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], check=False, text=True, capture_output=True
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return Path.cwd()


def project_name(root: Path) -> str:
    """The registry name of the project at ``root``, else its directory basename.

    A registry that cannot be read is reported and ignored: the inbox works without one.
    """
    from bd_track.registry import RegistryError, load_registry

    try:
        project = load_registry().for_directory(root)
    except RegistryError as err:
        root_log.warning("project registry unusable (%s); naming the inbox by directory", err)
        return root.name
    return project.name if project is not None else root.name


def find_inbox(inbox: Path | None = None, project_dir: Path | None = None) -> Path:
    """The project's inbox: ``inbox`` if given, else ``<inbox_root>/<project name>``."""
    if inbox is not None:
        return inbox.expanduser().resolve()
    return inbox_root() / project_name(project_root(project_dir))


def find_ticket(inbox: Path, ticket_id: str) -> Ticket:
    """Return the open ticket ``ticket_id`` (a file stem, file name or ``inbox-<hash>``)."""
    wanted = ticket_id.removesuffix(".md")
    for ticket in read_inbox(inbox):
        if ticket.id == wanted:
            return ticket
    sys.exit(f"bd-track: no open ticket {ticket_id!r} in {inbox / TRIAGE_DIR}")


# ---------------------------------------------------------------------------
# Listing tickets
# ---------------------------------------------------------------------------


def format_table(tickets: list[Ticket]) -> str:
    """One line per ticket: id, date, sender, type/urgency, title (``-`` for unknowns)."""
    rows = [
        (
            t.id,
            t.date or "-",
            t.sender or "-",
            "/".join(v for v in (t.type, t.urgency) if v) or "-",
            t.title,
        )
        for t in tickets
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    return "\n".join(
        "  ".join([*(cell.ljust(w) for cell, w in zip(row[:4], widths, strict=True)), row[4]])
        for row in rows
    )


def cmd_triage_list(
    *,
    inbox: Path | None = None,
    project_dir: Path | None = None,
    as_json: bool = False,
) -> None:
    """Print the project's open tickets: a table, or ``{"inbox", "tickets"}`` as JSON."""
    folder = find_inbox(inbox, project_dir)
    tickets = read_inbox(folder)
    if as_json:
        print(
            json.dumps({"inbox": str(folder), "tickets": [t.to_dict() for t in tickets]}, indent=2)
        )
        return
    if not tickets:
        root_log.info("no open tickets in %s", folder / TRIAGE_DIR)
        return
    print(format_table(tickets))


# ---------------------------------------------------------------------------
# Resolving a ticket
# ---------------------------------------------------------------------------

RESOLUTIONS = ("accepted", "rejected", "duplicate", "done")

# bd types and priorities a ticket maps to; overridable per resolve.
TYPE_TO_BEAD = {
    "bug": "bug",
    "feature": "feature",
    "task": "task",
    "idea": "task",
    "question": "task",
}
URGENCY_TO_PRIORITY = {"high": 1, "normal": 2, "low": 3}


@dataclass
class Resolution:
    """What to record on a ticket: ``status`` is one of RESOLUTIONS."""

    status: str
    outcome: str
    resolved: str = field(default_factory=lambda: dt.date.today().isoformat())


def _yaml_line(key: str, value: str) -> str:
    # ISO dates stay bare, as hand-written tickets have them; parsing reads either form back.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return f"{key}: {value}"
    return yaml.safe_dump({key: value}, default_flow_style=False, width=10_000).strip()


def set_frontmatter(content: str, values: dict[str, str]) -> str:
    """Set top-level ``values`` in the frontmatter, keeping every other line as written.

    A ticket without usable frontmatter gets a new block prepended.
    """
    lines = content.splitlines()
    parsed = _frontmatter(lines)
    if parsed is not None:
        end = parsed[1]
        head = lines[1:end]
        for key, value in values.items():
            new = _yaml_line(key, value)
            index = next(
                (i for i, line in enumerate(head) if re.match(rf"{re.escape(key)}\s*:", line)),
                None,
            )
            if index is None:
                head.append(new)
            else:
                head[index] = new
        rest = lines[end + 1 :]
        return "\n".join(["---", *head, "---", *rest]) + "\n"
    block = [_yaml_line(key, value) for key, value in values.items()]
    return "\n".join(["---", *block, "---", "", content.rstrip("\n")]) + "\n"


def _slugify(text: str, limit: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "note"


def done_path(inbox: Path, ticket: Ticket) -> Path:
    """Where a ticket lands in ``done/``: same name for a file, ``<date>-<slug>.md`` for a note."""
    if not ticket.is_note:
        return inbox / DONE_DIR / ticket.path.name
    return note_path(inbox / DONE_DIR, ticket)


def note_path(folder: Path, ticket: Ticket) -> Path:
    """A free ``<date>-<slug>.md`` in ``folder`` for a quick note (``-2``, ``-3`` on clashes)."""
    stem = f"{ticket.date or dt.date.today().isoformat()}-{_slugify(ticket.title)}"
    path = folder / f"{stem}.md"
    n = 2
    while path.exists():
        path = folder / f"{stem}-{n}.md"
        n += 1
    return path


def _write_temp(folder: Path, name: str, content: str, mode: int) -> str:
    """Write ``content`` to a temp file in ``folder`` with ``mode`` (mkstemp alone gives 0600)."""
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(tmp, mode)
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp


def _write_new(path: Path, content: str, mode: int) -> None:
    """Write ``content`` to ``path`` atomically, refusing to replace an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _write_temp(path.parent, path.name, content, mode)
    try:
        os.link(tmp, path)  # fails if path exists, unlike os.replace
    finally:
        os.unlink(tmp)


def _replace(path: Path, content: str) -> None:
    """Atomically replace ``path``, keeping its permissions."""
    mode = path.stat().st_mode & 0o777
    os.replace(_write_temp(path.parent, path.name, content, mode), path)


def _remove_note(ticket: Ticket) -> None:
    """Remove the note's paragraph from ``inbox.md``, re-read just before writing."""
    notes = parse_notes(ticket.path)
    remaining = [note.text for note in notes if note.id != ticket.id]
    if len(remaining) == len(notes):
        sys.exit(f"bd-track: {ticket.id} changed in {ticket.path} while resolving; not removed")
    if remaining:
        _replace(ticket.path, "\n\n".join(t for t in remaining if t) + "\n")
    else:
        ticket.path.unlink()


def done_content(ticket: Ticket, resolution: Resolution) -> str:
    """The ticket as it will be written to ``done/``, with the resolution keys set."""
    values = {
        "status": resolution.status,
        "resolved": resolution.resolved,
        "outcome": resolution.outcome,
    }
    if ticket.is_note:
        stamp = {k: ticket.meta[k] for k in ("from", "date", "type", "urgency") if k in ticket.meta}
        return set_frontmatter("", {**stamp, **values}).rstrip("\n") + "\n\n" + ticket.body + "\n"
    return set_frontmatter(ticket.path.read_text(errors="replace"), values)


def move_to_done(inbox: Path, ticket: Ticket, resolution: Resolution) -> Path:
    """Write the resolved ticket to ``done/``, then remove it from ``triage/``."""
    target = done_path(inbox, ticket)
    try:
        _write_new(target, done_content(ticket, resolution), ticket.path.stat().st_mode & 0o777)
    except FileExistsError:
        sys.exit(f"bd-track: {target} already exists; not overwriting")
    if ticket.is_note:
        _remove_note(ticket)
    else:
        ticket.path.unlink()
    return target


def bead_args(
    ticket: Ticket,
    target: Path,
    *,
    title: str | None = None,
    description: str | None = None,
    bead_type: str | None = None,
    priority: int | None = None,
) -> list[str]:
    """``bd create`` arguments for an accepted ticket (label ``triage``)."""
    origin = ", ".join(v for v in (ticket.sender, ticket.date) if v)
    trailer = f"ticket: {DONE_DIR}/{target.name}" + (f" (from {origin})" if origin else "")
    body = description if description is not None else ticket.body
    return [
        "bd",
        "create",
        "--title",
        title or ticket.title,
        "--type",
        bead_type or TYPE_TO_BEAD.get(ticket.type or "", "task"),
        "--priority",
        str(priority if priority is not None else URGENCY_TO_PRIORITY.get(ticket.urgency or "", 2)),
        "--labels",
        "triage",
        "--description",
        f"{body.rstrip()}\n\n{trailer}".lstrip(),
        "--silent",
    ]


def create_bead(args: list[str], project: Path) -> str:
    result = subprocess.run(args, check=False, text=True, capture_output=True, cwd=project)
    if result.returncode != 0:
        sys.exit(f"bd-track: `bd create` failed; ticket left open:\n{result.stderr.strip()}")
    bead_id = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
    if not bead_id:
        sys.exit("bd-track: `bd create` printed no issue id; ticket left open")
    return bead_id


def cmd_triage_resolve(
    ticket_id: str,
    *,
    bead: bool = False,
    reject: str | None = None,
    duplicate: str | None = None,
    done: str | None = None,
    title: str | None = None,
    description: str | None = None,
    bead_type: str | None = None,
    priority: int | None = None,
    inbox: Path | None = None,
    project_dir: Path | None = None,
    dry_run: bool = False,
) -> None:
    """Resolve one open ticket and move it to ``done/``; print where it went."""
    folder = find_inbox(inbox, project_dir)
    ticket = find_ticket(folder, ticket_id)
    target = done_path(folder, ticket)

    if bead:
        args = bead_args(
            ticket,
            target,
            title=title,
            description=description,
            bead_type=bead_type,
            priority=priority,
        )
        if dry_run:
            print(
                f"would create bead: {args[3]!r} (type {args[5]}, priority {args[7]}, label triage)"
            )
            print(f"would move {ticket.id} to {target} (status: accepted, outcome: bead <id>)")
            return
        bead_id = create_bead(args, project_root(project_dir))
        resolution = Resolution("accepted", f"bead {bead_id}")
        root_log.info("created %s from ticket %s", bead_id, ticket.id)
    elif reject is not None:
        resolution = Resolution("rejected", reject)
    elif duplicate is not None:
        resolution = Resolution("duplicate", f"duplicate of {duplicate}")
    else:
        resolution = Resolution("done", done or "")

    if dry_run:
        print(
            f"would move {ticket.id} to {target} "
            f"(status: {resolution.status}, outcome: {resolution.outcome})"
        )
        return
    try:
        moved = move_to_done(folder, ticket, resolution)
    except SystemExit:
        if bead:
            root_log.error(
                "bead %s was created; record it on ticket %s by hand",
                resolution.outcome.removeprefix("bead "),
                ticket.id,
            )
        raise
    print(moved)


# ---------------------------------------------------------------------------
# Asking about a ticket (it stays open)
# ---------------------------------------------------------------------------


def triage_section(question: str, day: str | None = None) -> str:
    return f"## Triage {day or dt.date.today().isoformat()}\n\n{question.strip()}\n"


def ask_ticket(inbox: Path, ticket: Ticket, question: str) -> Path:
    """Append a ``## Triage <date>`` section to the ticket and leave it open.

    A quick note has no room for a section, so it is promoted to its own ticket file in
    ``triage/`` (keeping its stamp's keys) and its paragraph leaves ``inbox.md``.
    """
    section = triage_section(question)
    if not ticket.is_note:
        content = ticket.path.read_text(errors="replace").rstrip("\n")
        _replace(ticket.path, f"{content}\n\n{section}")
        return ticket.path
    stamp = {k: ticket.meta[k] for k in ("from", "date", "type", "urgency") if k in ticket.meta}
    head = set_frontmatter("", {**stamp, "status": "open"}).rstrip("\n")
    target = note_path(inbox / TRIAGE_DIR, ticket)
    try:
        _write_new(
            target,
            f"{head}\n\n{ticket.body}\n\n{section}",
            ticket.path.stat().st_mode & 0o777,
        )
    except FileExistsError:
        sys.exit(f"bd-track: {target} already exists; not overwriting")
    _remove_note(ticket)
    return target


def cmd_triage_ask(
    ticket_id: str,
    question: str,
    *,
    inbox: Path | None = None,
    project_dir: Path | None = None,
    dry_run: bool = False,
) -> None:
    """Record a question on one open ticket; print the ticket's path."""
    folder = find_inbox(inbox, project_dir)
    ticket = find_ticket(folder, ticket_id)
    if dry_run:
        where = "promote it to its own file and " if ticket.is_note else ""
        print(f"would {where}append a Triage section to {ticket.id}: {question!r}")
        return
    print(ask_ticket(folder, ticket, question))


# ---------------------------------------------------------------------------
# Interactive loop (built on list, resolve and ask)
# ---------------------------------------------------------------------------

ACTIONS = {
    "a": "accept",
    "r": "reject",
    "d": "duplicate",
    "s": "skip",
    "k": "ask",
    "q": "quit",
}
PROMPT = "[a]ccept  [r]eject  [d]uplicate  [s]kip  as[k]  [q]uit"


def _show(ticket: Ticket, index: int, total: int) -> None:
    origin = ", ".join(v for v in (ticket.sender, ticket.date, ticket.type, ticket.urgency) if v)
    print(f"\n── [{index}/{total}] {ticket.id}" + (f"  ({origin})" if origin else ""))
    print(ticket.title)
    if ticket.body.strip() and ticket.body.strip() != ticket.title:
        print()
        print(ticket.body)
    print()


def run_interactive(
    folder: Path,
    *,
    project_dir: Path | None,
    dry_run: bool,
    ask: Callable[..., str],
) -> dict[str, int]:
    """Walk the open tickets, prompting via ``ask(message, choices=..., default=...)``.

    The inbox is re-read after every action, so ids stay current when a quick note leaves
    ``inbox.md``. Tickets skipped, asked about, or acted on in a dry run are not shown again.
    Returns a count per action.
    """
    common = {"inbox": folder, "project_dir": project_dir, "dry_run": dry_run}
    counts = {name: 0 for name in ACTIONS.values() if name != "quit"}
    seen: set[str] = set()
    while True:
        pending = [t for t in read_inbox(folder) if t.id not in seen]
        if not pending:
            break
        ticket = pending[0]
        _show(ticket, len(seen) + 1, len(seen) + len(pending))
        action = ACTIONS[ask(PROMPT, choices=list(ACTIONS), default="s")]
        if action == "quit":
            break
        counts[action] += 1
        if action == "accept":
            default_type = TYPE_TO_BEAD.get(ticket.type or "", "task")
            default_priority = str(URGENCY_TO_PRIORITY.get(ticket.urgency or "", 2))
            cmd_triage_resolve(
                ticket.id,
                bead=True,
                title=ask("Bead title", default=ticket.title),
                bead_type=ask("Bead type", default=default_type),
                priority=int(ask("Priority", choices=list("01234"), default=default_priority)),
                **common,
            )
        elif action == "reject":
            cmd_triage_resolve(ticket.id, reject=ask("Reason"), **common)
        elif action == "duplicate":
            cmd_triage_resolve(
                ticket.id, duplicate=ask("Duplicate of (bead or ticket id)"), **common
            )
        elif action == "ask":
            question = ask("Question (who must answer, and what)")
            if dry_run or not ticket.is_note:
                cmd_triage_ask(ticket.id, question, **common)
            else:
                seen.add(ask_ticket(folder, ticket, question).stem)
                print(f"promoted {ticket.id} to its own ticket file")
        if dry_run or action in ("skip", "ask"):
            seen.add(ticket.id)
    return counts


def cmd_triage_interactive(
    *,
    inbox: Path | None = None,
    project_dir: Path | None = None,
    dry_run: bool = False,
) -> None:
    """``bd-track triage``: decide each open ticket in turn."""
    if not is_interactive():
        sys.exit(
            "bd-track: triage needs a terminal; use `triage list --json` and "
            "`triage resolve` / `triage ask` from scripts"
        )
    from rich.markup import escape
    from rich.prompt import Prompt

    def ask(message: str, **kwargs: object) -> str:
        # The action prompt lists its choices itself; escape keeps "[a]ccept" from being markup.
        return Prompt.ask(escape(message), show_choices=False, **kwargs)

    folder = find_inbox(inbox, project_dir)
    if dry_run:
        root_log.info("dry run: nothing will be written")
    counts = run_interactive(folder, project_dir=project_dir, dry_run=dry_run, ask=ask)
    summary = ", ".join(f"{name} {n}" for name, n in counts.items() if n)
    root_log.info("triage of %s: %s", folder, summary or "no open tickets")
