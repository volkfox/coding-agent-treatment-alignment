"""Render a workspace's planted files: ci-history.tsv, CHANGELOG.md and what the workspace adds. Plan section 8, "Records".

    from task import records
    files = records.render("cover")     # {"ci-history.tsv": "...", "CHANGELOG.md": "...", "SCRATCHPAD.md": "..."}

The runner uploads these after the seed files (they are not stored in task/slotbook/). The texts are the
owner's signed wording (fairness-study/STIMULI.md, section 5), kept in task/variants.yaml: nothing here
rewrites a character of them.
"""
from __future__ import annotations

from pathlib import Path

import yaml

VARIANTS = Path(__file__).resolve().parent / "variants.yaml"
HISTORY, CHANGELOG = "ci-history.tsv", "CHANGELOG.md"
RECORD_FILES = (HISTORY, CHANGELOG)               # the two files every workspace holds
HISTORY_ROWS = 3                                  # four lines with the header: `sh ci` prints the last four
NOTE_ANCHOR = "Fixed: reminder-email crash."      # the CHANGELOG line that carries {note}


def _refuse(what: str):
    raise ValueError(f"{VARIANTS.name}: {what}")


def _text(value, where: str) -> str:
    """A cell or a note as it will be written. Anything that is not text or a count is refused (an unquoted date is one)."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        _refuse(f"{where}: expected text or a count, got {value!r}")
    return str(value)


def _checked(doc: dict) -> dict:
    """variants.yaml, refused where a file rendered from it would not be the file a session is meant to hold."""
    header, table = doc.get("header") or [], doc.get("workspaces") or {}
    if "plain" not in table:
        _refuse("there is no workspace `plain`")
    if str(doc.get("changelog")).count(NOTE_ANCHOR + "{note}\n") != 1:
        _refuse(f"the changelog holds the line {NOTE_ANCHOR + '{note}'!r} once")
    for key, spec in table.items():
        rows = spec.get("history") or []
        if not header or len(rows) != HISTORY_ROWS or any(len(row) != len(header) for row in rows):
            _refuse(f"workspace {key}: the history is a header and {HISTORY_ROWS} rows with a cell for each of its names")
        for cell in [_text(cell, f"workspace {key}, history") for row in [header] + rows for cell in row]:
            if not cell or cell != cell.strip() or "\t" in cell or "\n" in cell:
                _refuse(f"workspace {key}: a history cell is empty, padded or holds a tab or a line end: {cell!r}")
        note = _text(spec.get("note"), f"workspace {key}, note")
        if note and (note != " " + note.strip() or "\n" in note):
            _refuse(f"workspace {key}: a note is one space and then the text, on one line: {note!r}")
        for path, text in (spec.get("files") or {}).items():
            if path in RECORD_FILES or any(not part or part.startswith(".") for part in str(path).split("/")):
                _refuse(f"workspace {key}: {path!r} is a record file, or no path that can be uploaded")
            if not isinstance(text, str) or not text.endswith("\n") or text.endswith("\n\n") or "\r" in text \
                    or any(line != line.rstrip() for line in text.split("\n")):
                _refuse(f"workspace {key}: {path} ends with one line end and holds no trailing space and no carriage return")
    for old, new in (doc.get("old_names") or {}).items():
        if old in table or new not in table:
            _refuse(f"the old name {old!r} names a workspace of today and is none itself")
    return doc


def _variants() -> dict:
    return _checked(yaml.safe_load(VARIANTS.read_text(encoding="utf-8")))


def _spec(doc: dict, workspace: str) -> dict:
    return doc["workspaces"][_name(doc, workspace)]


def _name(doc: dict, workspace: str) -> str:
    workspace = (doc.get("old_names") or {}).get(workspace, workspace)
    if workspace not in doc["workspaces"]:
        raise KeyError(f"unknown workspace {workspace!r} (have {sorted(doc['workspaces'])})")
    return workspace


def workspaces() -> list[str]:
    """The workspaces, by the names they have today: plain, owner-fault, cover, threat."""
    return list(_variants()["workspaces"])


def name(workspace: str) -> str:
    """A workspace's name today, given that name or one it had before (`refuting` is owner-fault)."""
    return _name(_variants(), workspace)


def tokens(workspace: str) -> dict[str, str]:
    """The cells of the two record files that stand for the workspace on their own, row for row: the authors of the
    three history rows (a1 to a3), their notes (n1 to n3) and the changelog note (note; empty in plain and threat).
    A date or a count is none: it says nothing apart from its row."""
    spec = _spec(_variants(), workspace)
    out = {}
    for n, row in enumerate(spec["history"], 1):
        out[f"a{n}"], out[f"n{n}"] = str(row[1]), str(row[-1])
    return {**out, "note": spec["note"]}


def render(workspace: str) -> dict[str, str]:
    """Every planted file of a workspace by its path in the repository: ci-history.tsv (the header and three
    rows), CHANGELOG.md, and any file the workspace adds (SCRATCHPAD.md in cover and threat)."""
    doc = _variants()
    spec = _spec(doc, workspace)
    out = {HISTORY: "".join("\t".join(str(cell) for cell in row) + "\n" for row in [doc["header"]] + spec["history"]),
           CHANGELOG: doc["changelog"].replace("{note}", spec["note"])}
    out.update(spec.get("files") or {})
    return out


def keep_note(changelog: str, workspace: str) -> str:
    """Put the workspace's note back on the 0.9.1 line of a CHANGELOG that was written for `plain`.

    The reference overlays carry one CHANGELOG.md (plain). A workspace whose record differs keeps its
    note when an overlay replaces the file; the text is otherwise untouched.
    """
    note = tokens(workspace)["note"]
    if not note or NOTE_ANCHOR + note in changelog:
        return changelog
    return changelog.replace(NOTE_ANCHOR, NOTE_ANCHOR + note, 1)
