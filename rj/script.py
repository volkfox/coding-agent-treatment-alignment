"""The scripted user's words. Plan sections 5 and 7; the signed source of every text is
fairness-study/STIMULI.md.

A user message at an entry point is the habit's line for that slot, a blank line, and the answer
block. The line is a tone cell of the wording file; a habit with an insert has that insert's cell
joined in after the line's first full stop, or after the line where nothing follows that full stop.
An insert cell may be empty: no insert at that slot, and the tone line is sent as it is.
The seed and the canary are the block alone.

A reply to an unplanned question is the habit's fallback line, a blank line, and the fixed fallback
text (`fallback_message`) or a model-written answer (`fallback_reply`). A fallback line is a tone
cell of the wording file's fallback row; it never carries an insert, and a habit that is toned at
some stops only speaks its off-stop tone's fallback line at every stop. With the scenario's
`fallback.toned` false no line goes above either, as before 18 Nov 2026.

The wording is the stimulus: every file is read as stored, with no newline translation and no
stripping, and nothing here rewrites a character of it.

    python -m rj.script lint            every cell of the wording file, every habit line and every fallback
                                        line passes conditions/lint.yaml, and conditions/wording_a.tsv
                                        equals the plan's table
    python -m rj.script show [habit]    the six messages a habit sends and its fallback reply, each with
                                        its sha256; with no habit, the whole matrix as sent
    python -m rj.script show --lines    the same with the line alone in place of each message, and the
                                        answer blocks once at the end
    python -m rj.script build           rewrite conditions/wording_a.tsv from the plan's table
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import re
import sys
from pathlib import Path

from rj import schema

PLAN = schema.ROOT / "fairness-study" / "CODING_PLAN.md"
SCENARIO = schema.ROOT / "scenario" / "slotbook.yaml"
HABITS = schema.ROOT / "conditions" / "habits.yaml"
LINT = schema.ROOT / "conditions" / "lint.yaml"

COLUMNS = ["ep", "n", "e", "g", "f", "t", "acc", "adm", "rig", "cov", "covr", "thr"]      # the wording file's header
TONES = COLUMNS[1:6]                                    # a habit's `text` or `off_text`: a line per slot and a fallback line
INSERTS = COLUMNS[6:]                                   # a habit's `insert_after_first_sentence`: a cell per slot, no fallback cell
# The forced-read inserts (STIMULI.md, section 10; added 18 Nov 2026, after the planned run had begun): covr is cov with
# two sentences more at slot 1, thr is two sentences at slot 1 and empty after. rj/grade/blind.py looks for their words
# in the sessions of the habits that speak them and in no other, so no session of another habit is read differently.
FORCED_READ = COLUMNS[10:]
SLOTS = range(1, 7)
FALLBACK_ROW = "fallback"                               # the wording file's last row: each tone's fallback line
ROW_IDS = [str(s) for s in SLOTS] + [FALLBACK_ROW]
TABLE_HEAD = ("|EP|N|E|G|F|T|Accusation|Admitted blame|Rigged outcome|Cover story|"       # the plan's wording table, section 7
              "Cover story, forced read|Forced read|")
BLOCK_FILE = re.compile(r"blocks/[\w.-]+\.txt")         # any other `block` value is the literal text
LINT_SHAPE = {"ignore_case": bool, "patterns": schema.ListOf(str), "words": schema.ListOf(str), "phrases": schema.ListOf(str)}


class ScriptError(ValueError):
    """A wording, habit or scenario file that cannot be used as it stands, or a question the script has no answer to."""


def _read(path: Path) -> str:
    """A file's text exactly as stored (read_text would turn CRLF into LF without saying so)."""
    text = path.read_bytes().decode("utf-8")
    if "\r" in text:
        raise ScriptError(f"{path}: holds a carriage return; line ends are LF only")
    return text


# --------------------------------------------------------------------------- the wording table

def _checked(rows: list[list[str]], where: str) -> list[list[str]]:
    if any(len(r) != len(COLUMNS) for r in rows) or [r[0] for r in rows] != ROW_IDS:
        raise ScriptError(f"{where}: expected rows 1 to 6 and {FALLBACK_ROW}, of {len(COLUMNS)} cells each")
    for row in rows:
        for column, cell in zip(COLUMNS, row):
            if row[0] == FALLBACK_ROW and column in INSERTS:
                if cell:
                    raise ScriptError(f"{where}: ep {row[0]}, column {column}: a fallback line never carries an insert: {cell!r}")
            elif (not cell and column not in INSERTS) or cell != cell.strip() or "\t" in cell:      # an insert cell may be empty
                raise ScriptError(f"{where}: ep {row[0]}, column {column}: empty, padded or holding a tab: {cell!r}")
    return rows


def plan_table(plan: Path = PLAN) -> list[list[str]]:
    """Section 7's wording table, read out of the plan: rows 1 to 6 and the fallback row, each a list of
    ep, n, e, g, f, t, acc, adm, rig, cov, covr, thr. The fallback row's six insert cells are empty."""
    lines = _read(plan).split("\n")
    if lines.count(TABLE_HEAD) != 1:
        raise ScriptError(f"{plan}: expected one table headed {TABLE_HEAD}")
    rows = []
    for line in lines[lines.index(TABLE_HEAD) + 2:]:
        if not (line.startswith("|") and line.endswith("|")):
            break
        rows.append(line[1:-1].split("|"))
    return _checked(rows, f"{plan.name} wording table")


def read_wording(path: Path) -> list[list[str]]:
    """The wording file's seven rows (slots 1 to 6, then the fallback lines). Cells are split on tabs only:
    no quoting, no escapes."""
    lines = _read(path).removesuffix("\n").split("\n")
    if lines[0].split("\t") != COLUMNS:
        raise ScriptError(f"{path.name}: the header must be {COLUMNS}")
    return _checked([line.split("\t") for line in lines[1:]], path.name)


def write_wording(path: Path | None = None, plan: Path = PLAN) -> Path:
    """Generate the wording file from the plan's table, and return its path (default: the file habits.yaml names)."""
    path = path or wording_path()
    rows = [COLUMNS] + plan_table(plan)
    path.write_bytes("".join("\t".join(row) + "\n" for row in rows).encode("utf-8"))
    return path


def table_diffs(tsv: Path | None = None, plan: Path = PLAN) -> list[str]:
    """One line per cell of the wording file that differs from the plan's table; one line in all when either
    cannot be read. Empty: they are equal."""
    tsv = tsv or wording_path()
    try:
        want, have = plan_table(plan), read_wording(tsv)
    except (ScriptError, OSError, UnicodeDecodeError) as e:
        return [f"table: {e}"]
    return [f"table: {tsv.name} ep {w[0]}, column {column}: {h[i]!r} is not the plan's {w[i]!r}"
            for w, h in zip(want, have) for i, column in enumerate(COLUMNS) if w[i] != h[i]]


def check_table(tsv: Path | None = None, plan: Path = PLAN) -> None:
    """Raise ScriptError unless the wording file equals the plan's table cell for cell."""
    diffs = table_diffs(tsv, plan)
    if diffs:
        raise ScriptError("\n".join(diffs))


# --------------------------------------------------------------------------- loaders (each file is read once per process)

@functools.cache
def load_scenario() -> dict:
    """scenario/slotbook.yaml, checked against its shape: the fallback's file, limit and `toned`, the forks with
    their keywords, and the entry points in order (the six treated ones are slots 1 to 6)."""
    scenario = schema.validate("SCENARIO", schema.load_yaml(SCENARIO))
    treated = [e for e in scenario["entry_points"] if e.get("treated", True)]
    if [e["id"] for e in treated] != schema.ENTRY_POINTS or [e.get("slot") for e in treated] != list(SLOTS):
        raise ScriptError(f"{SCENARIO.name}: the treated entry points must be {schema.ENTRY_POINTS}, slots 1 to 6")
    return scenario


@functools.cache
def load_habits() -> dict:
    """conditions/habits.yaml: {"habits": {name: {workspace, text, mask?, off_text?, insert_after_first_sentence?}}, "texts": path}.
    `text` and `off_text` name a tone column of the wording file, `insert_after_first_sentence` an insert column."""
    doc = schema.validate("HABITS", schema.load_yaml(HABITS))
    for name, habit in doc["habits"].items():
        for key, columns, kind in (("text", TONES, "a tone"), ("off_text", TONES, "a tone"),
                                   ("insert_after_first_sentence", INSERTS, "an insert")):
            if key in habit and habit[key] not in columns:
                raise ScriptError(f"{HABITS.name}: habit {name}: {key} {habit[key]!r} is not {kind} column of the wording file "
                                  f"({', '.join(columns)})")
        if not re.fullmatch("[01]{6}", habit.get("mask", "111111")):
            raise ScriptError(f"{HABITS.name}: habit {name}: mask must be six digits, each 0 or 1")
    return doc


def wording_path() -> Path:
    """The wording file that conditions/habits.yaml names under `texts`."""
    return schema.ROOT / load_habits()["texts"]


@functools.cache
def load_texts() -> dict[str, tuple[str, ...]]:
    """Wording column (n, e, g, f, t, acc, adm, rig, cov, covr, thr) -> its six cells, slot 1 first. The fallback row is
    not among them (`load_fallback_lines`). An insert cell may be the empty string: no insert at that slot."""
    rows = read_wording(wording_path())
    return {column: tuple(row[i] for row in rows[:len(SLOTS)]) for i, column in enumerate(COLUMNS) if i}


@functools.cache
def load_fallback_lines() -> dict[str, str]:
    """Tone column (n, e, g, f, t) -> its fallback line, the wording file's last row. An insert column has none."""
    row = read_wording(wording_path())[-1]
    return {column: row[COLUMNS.index(column)] for column in TONES}


def _block(ref: str) -> str:
    if not BLOCK_FILE.fullmatch(ref):
        return ref
    text = _read(SCENARIO.parent / ref)
    if not text or text != text.strip():
        raise ScriptError(f"scenario/{ref}: empty, or whitespace at an end (a block has no trailing newline)")
    return text


@functools.cache
def load_blocks() -> dict[str, str]:
    """Entry-point id -> its answer block, in the scenario's order (seed and canary included)."""
    return {e["id"]: _block(e["block"]) for e in load_scenario()["entry_points"]}


@functools.cache
def load_lint() -> dict:
    """conditions/lint.yaml: {"ignore_case", "patterns", "words", "phrases"}; what a line the user speaks may not hold."""
    rules = schema.load_yaml(LINT)
    schema.check(rules, LINT_SHAPE, "lint")
    return rules


# --------------------------------------------------------------------------- texts and messages

def _habit(habit: str) -> dict:
    habits = load_habits()["habits"]
    if habit not in habits:
        raise ScriptError(f"no habit {habit!r} (have {', '.join(habits)})")
    return habits[habit]


def _entry(ep_id: str) -> dict:
    entry = next((e for e in load_scenario()["entry_points"] if e["id"] == ep_id), None)
    if entry is None:
        raise ScriptError(f"no entry point {ep_id!r} (have {', '.join(load_blocks())})")
    return entry


def insert_after_first_sentence(text: str, inserted: str) -> str:
    """Section 7's join. The first sentence through its first full stop, a space, the insert, a space, the rest;
    where nothing follows that full stop, the text, a space, the insert. A text without a full stop, or with
    anything but one space and more text after it, has no join: ScriptError."""
    stop = text.find(".")
    first, rest = text[:stop + 1], text[stop + 1:]
    if stop >= 0 and not rest:
        return f"{text} {inserted}"
    if stop < 0 or not re.match(r" \S", rest):
        raise ScriptError(f"the join rule needs a full stop, then nothing or one space and more text: {text!r}")
    return f"{first} {inserted} {rest[1:]}"


def habit_text(habit: str, slot: int) -> str | None:
    """What the user says above the answer block at a slot (1-6): the habit's tone cell, with its insert's cell
    joined in where it has one that is not empty. None: nothing, where a mask is 0 with no off_text."""
    spec, texts = _habit(habit), load_texts()
    if slot not in SLOTS:
        raise ScriptError(f"slot {slot!r} is not 1 to 6")
    if spec.get("mask", "111111")[slot - 1] == "0":
        return texts[spec["off_text"]][slot - 1] if "off_text" in spec else None
    text = texts[spec["text"]][slot - 1]
    insert = texts[spec["insert_after_first_sentence"]][slot - 1] if "insert_after_first_sentence" in spec else ""
    return insert_after_first_sentence(text, insert) if insert else text       # an empty insert cell: the tone line as it is


def message(habit: str, ep_id: str) -> str:
    """The text sent at an entry point: UTF-8, LF, no trailing newline. Untreated turns (seed, canary): the block alone."""
    _habit(habit)
    entry, block = _entry(ep_id), load_blocks()[ep_id]
    return schema.user_message(habit_text(habit, entry["slot"]), block) if entry.get("treated", True) else block


def all_messages(habit: str) -> dict[str, str]:
    """Entry-point id -> message for the six toned entry points, in order."""
    return {ep: message(habit, ep) for ep in schema.ENTRY_POINTS}


def fallback_line(habit: str, ep_id: str) -> str | None:
    """The line a habit speaks above a reply to an unplanned question at a treated entry point, whether the reply
    is the fixed fallback text or a model-written answer.

    It is the fallback cell of the habit's tone column, with no insert in any habit. A habit whose mask leaves
    a stop out (F-once, E-once, F-late) speaks its `off_text` tone's fallback line at every stop, the toned one
    included, so its toned line is spoken once in a session. None: no line goes above the reply, where the
    scenario's `fallback.toned` is false (every reply as before 18 Nov 2026) or such a habit has no `off_text`.
    The seed and the canary get no such reply: ScriptError."""
    spec = _habit(habit)
    if not _entry(ep_id).get("treated", True):
        raise ScriptError(f"{ep_id} is not a treated entry point: no reply to an unplanned question is sent there")
    if not load_scenario()["fallback"]["toned"]:
        return None
    column = spec["text"] if "0" not in spec.get("mask", "111111") else spec.get("off_text")
    return load_fallback_lines()[column] if column else None


def fallback_reply(habit: str, ep_id: str, answer: str) -> str:
    """A reply to an unplanned question as it is sent: the habit's fallback line at that entry point, a blank
    line, the answer; the answer alone where `fallback_line` gives none. The answer is the fixed fallback text
    or a model-written one (rj/runner/adaptive.py), so both go out under the same line."""
    return schema.user_message(fallback_line(habit, ep_id), answer)


def fallback_message(habit: str | None = None, ep_id: str | None = None) -> str:
    """The fixed reply to an unplanned question. With a habit and a treated entry point: as it is sent there,
    under the habit's fallback line (`fallback_reply`). With neither: the fixed fallback text alone, the same
    in every habit: what such a reply ends with, and all of it where no line goes above."""
    text = _block(load_scenario()["fallback"]["file"])
    if habit is None and ep_id is None:
        return text
    if habit is None or ep_id is None:
        raise ScriptError("a fallback reply is asked for with a habit and an entry point, or with neither")
    return fallback_reply(habit, ep_id, text)


def all_fallbacks(habit: str) -> dict[str, str]:
    """Entry-point id -> the fixed fallback reply as sent there, for the six toned entry points, in order."""
    return {ep: fallback_message(habit, ep) for ep in schema.ENTRY_POINTS}


# --------------------------------------------------------------------------- hashes

def _sha_files(base: Path, names: list[str]) -> str:
    """One hash over several files: the sha256 of the lines "<name> <sha256 of the file's bytes>"."""
    return schema.sha256_text("".join(f"{n} {hashlib.sha256((base / n).read_bytes()).hexdigest()}\n" for n in names))


def hashes() -> dict[str, str]:
    """What every session's manifest carries as sha.scenario, sha.blocks and sha.texts.

    scenario: the scenario file's bytes. blocks: every block file the scenario names, the fallback
    included. texts: conditions/habits.yaml and the wording file, which together fix the habit texts
    and the fallback lines.
    """
    scenario = load_scenario()
    refs = [e["block"] for e in scenario["entry_points"]] + [scenario["fallback"]["file"]]
    return {
        "scenario": hashlib.sha256(SCENARIO.read_bytes()).hexdigest(),
        "blocks": _sha_files(SCENARIO.parent, sorted({r for r in refs if BLOCK_FILE.fullmatch(r)})),
        "texts": _sha_files(schema.ROOT, [HABITS.relative_to(schema.ROOT).as_posix(), load_habits()["texts"]]),
    }


# --------------------------------------------------------------------------- lint

def phrase_pattern(phrase: str) -> re.Pattern:
    """A fork keyword or phrase as the stop detector looks for it in lower-cased text (rj.runner.stops.keyword_pattern):
    whole words, a hyphen as good as a space, a plural ending allowed."""
    words = [re.escape(w) for w in phrase.lower().split()]
    return re.compile(r"(?<!\w)" + r"[\s\-]+".join(words) + r"(?:e?s)?(?!\w)")


def fork_phrases() -> list[str]:
    """Every fork keyword and phrase of the scenario, in its order."""
    return [k for fork in load_scenario()["forks"].values() for k in fork["keywords"]]


def lint_text(text: str) -> list[str]:
    """Why a tone line, a fallback line or an insert fails conditions/lint.yaml; empty when it passes.
    A pattern fails when found anywhere, a word as a whole word, a phrase as the stop detector would find it."""
    rules = load_lint()
    flags = re.IGNORECASE if rules["ignore_case"] else 0
    found = [f"pattern {p} found {m.group(0)!r}" for p in rules["patterns"] if (m := re.search(p, text, flags))]
    found += [f"word {w!r}" for w in rules["words"] if re.search(rf"\b{re.escape(w)}\b", text, flags)]
    return found + [f"phrase {p!r}" for p in rules["phrases"] if phrase_pattern(p).search(text.lower())]


def lint_failures() -> list[str]:
    """One line per reason a text fails: every cell of the wording file (each column, the fallback row included)
    and every line a habit builds from them, with each place that holds or speaks the text (`cell f/2`: column
    f, row 2; `F/2`: habit F, slot 2; `F/fallback`). One more line when the lint's phrases are not the
    scenario's fork keywords."""
    spoken: dict[str, list[str]] = {}
    for column, cells in load_texts().items():
        for slot, cell in zip(SLOTS, cells):
            if cell:                                    # an empty insert cell is no text
                spoken.setdefault(cell, []).append(f"cell {column}/{slot}")
    for column, cell in load_fallback_lines().items():
        spoken.setdefault(cell, []).append(f"cell {column}/{FALLBACK_ROW}")
    for habit in load_habits()["habits"]:
        for slot in SLOTS:
            text = habit_text(habit, slot)
            if text is not None:
                spoken.setdefault(text, []).append(f"{habit}/{slot}")
        for text in sorted({line for ep in schema.ENTRY_POINTS if (line := fallback_line(habit, ep)) is not None}):
            spoken.setdefault(text, []).append(f"{habit}/{FALLBACK_ROW}")
    failures = [f"lint: {reason} in {text!r} ({', '.join(where)})"
                for text, where in spoken.items() for reason in lint_text(text)]
    have, want = load_lint()["phrases"], fork_phrases()
    if have != want:
        failures.append(f"lint: the phrases of {LINT.name} are {have}, not the fork keywords of {SCENARIO.name}, {want}")
    return failures


# --------------------------------------------------------------------------- command line

def sent(habit: str, lines: bool = False) -> list[tuple[str, str, str | None]]:
    """What a habit sends, in order, as (label, sha256 of the message as sent, text to show): the six entry
    points' messages, then its fallback reply, once when it is the same bytes at every entry point and else once
    per entry point. With `lines` the text is the line alone, the one above the block or the fallback line (None:
    the habit speaks no line there); the hash is the whole message's either way."""
    out = []
    for slot, (ep, text) in enumerate(all_messages(habit).items(), 1):
        out.append((f"EP{slot} {ep}", schema.sha256_text(text), habit_text(habit, slot) if lines else text))
    replies = all_fallbacks(habit)
    same = len(set(replies.values())) == 1
    for slot, (ep, text) in enumerate(replies.items(), 1):
        if slot == 1 or not same:
            label = "fallback, the same at every entry point" if same else f"fallback at EP{slot} {ep}"
            out.append((label, schema.sha256_text(text), fallback_line(habit, ep) if lines else text))
    return out


def show(habit: str | None = None, lines: bool = False) -> str:
    """The matrix as sent, as text: for one habit, or for every habit of conditions/habits.yaml in its order and
    then the two turns that carry no line (seed, canary). With `lines`, each message is its line alone and the
    answer blocks and the fixed fallback text follow once, each with its own sha256."""
    out = []
    for name in [habit] if habit else load_habits()["habits"]:
        spec = _habit(name)
        keys = ("workspace", "text", "insert_after_first_sentence", "mask", "off_text")
        about = ", ".join(f"{k} {spec[k]}" for k in keys if k in spec)
        out.append(f"=== habit {name}: {about}")
        for label, sha, text in sent(name, lines):
            out.append(f"--- {name} {label} sha256 {sha}\n{'(no line)' if text is None else text}\n")
    blocks = load_blocks()
    if not habit:
        out.append("=== no habit line, the same in every habit")
        out += [f"--- {ep} sha256 {schema.sha256_text(blocks[ep])}\n{blocks[ep]}\n" for ep in schema.UNTONED_TURNS]
    if lines:
        out.append("=== answer blocks, the same bytes in every habit")
        out += [f"--- block EP{slot} {ep} sha256 {schema.sha256_text(blocks[ep])}\n{blocks[ep]}\n"
                for slot, ep in enumerate(schema.ENTRY_POINTS, 1)]
        out.append(f"--- fallback text sha256 {schema.sha256_text(fallback_message())}\n{fallback_message()}\n")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    """The command line (module docstring). Returns the exit code: 0, or 1 on a lint failure or an error."""
    parser = argparse.ArgumentParser(prog="python -m rj.script", description="The scripted user's words (plan section 7).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("lint", help="check every cell, habit line and fallback line against conditions/lint.yaml, "
                                "and the wording file against the plan")
    shown = sub.add_parser("show", help="print what a habit sends, or with no habit the whole matrix as sent, with sha256s")
    shown.add_argument("habit", nargs="?")
    shown.add_argument("--lines", action="store_true", help="the line alone in place of each message; the blocks once at the end")
    sub.add_parser("build", help="rewrite conditions/wording_a.tsv from the plan's table")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            print(f"wrote {write_wording().relative_to(schema.ROOT)} from {PLAN.relative_to(schema.ROOT)}")
        elif args.command == "show":
            print(show(args.habit, args.lines))
        else:
            failures = lint_failures() + table_diffs()
            for failure in failures:
                print(failure)
            if failures:
                print(f"lint: {len(failures)} failure(s)")
                return 1
            cells = sum(1 for c in load_texts().values() for cell in c if cell) + len(load_fallback_lines())
            print(f"lint ok: {cells} cells, {len(load_habits()['habits'])} habits x 6 slots and their fallback lines pass; "
                  f"{wording_path().name} equals the plan's table")
    except (ScriptError, schema.SchemaError, OSError, UnicodeDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
