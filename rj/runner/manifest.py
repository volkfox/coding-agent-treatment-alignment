"""Which sessions a run holds, and the files that say so. Plan section 5 (profiles.yaml, manifest.jsonl)
and section 3 ("Replacing a row").

    entries = plan(profile, through="matrix", replacements=load_replacements(runs_dir))
    lines = table(entries)                             # how many sessions per row and habit, to print before anything is sent
    written = write(run_dir, entries, matrix_rows)     # manifest.jsonl, matrix.yaml, sessions/<key>/manifest.json

A session is one (row, habit, replicate): key `row/habit/possible/A/rNN`.

  * Order, no shuffle: stages as the profile lists them, rows as a stage lists them, and in a row the
    neutral arm first (N r01, N r02), then the other arms in the profile's order. `order` is a session's
    place, from 1, in that walk over the whole profile, so it does not depend on which stages are selected.
    It is the order the runner takes sessions up in (rj.runner.launch): stage after stage, and within a
    stage as the in-flight limits and the smoke rule allow.
  * Replicate numbers accumulate per (row, habit) across stages: a later stage adds replicates and reruns
    nothing. They too are counted over the whole profile.
  * Row ids resolve through <runs>/replacements.json ({"old": "new"}), chains followed: a stage that lists a
    replaced row plans the new row's sessions. The old row's entries stay in manifest.jsonl.

manifest.jsonl is only ever appended to. An entry that is already there is returned as recorded, with the
hashes it was planned under; `write` reports the keys whose recorded hashes differ from today's files.
So the file is in the order things were planned, not in `order`: after stages run out of sequence, or a
replacement (the new row takes the old row's places), a reader that wants the plan's order sorts by
(order, key), as `in_order` does.

A key recorded under other hashes is a session of another wording, or of another seed or grader: taken into
today's plan it would stand as today's if it is over, and be sent today's texts under yesterday's record and
stage if it is not. `changed` finds such keys without writing anything, and `refuse_changed` raises
`OtherHashes` for them; the runner calls it before it records a plan (rj.runner.launch.run).

A live session (`rj row --live`: the seed and the kickoff only) gets a key no stage can plan: `live_entry`
gives it stage "live" and a replicate number from 90 up, and `plan` refuses a cell that would reach 90.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import sys
from pathlib import Path

import yaml

from rj import schema, script

VARIANT, WORDING = "possible", "A"
NEUTRAL = "N"
LIVE_STAGE = "live"                 # as rj.aggregate names it: listed, never a baseline
LIVE_REPLICATE = 90                 # live sessions are r90, r91, ...; planned replicates stay below
MANIFEST, MATRIX = "manifest.jsonl", "matrix.yaml"
REPLACEMENTS = "replacements.json"
GRADER_FILES = ("grader.yaml", "grader/tools.yaml")
HIDDEN = "task/hidden"


class ManifestError(ValueError):
    pass


class OtherHashes(ManifestError):
    """A plan holds keys that the run directory records under other hashes than today's files. `keys`: which."""

    def __init__(self, message: str, keys: list[str]):
        super().__init__(message)
        self.keys = keys


# --------------------------------------------------------------------------- small file helpers (shared by the runner)

def read_json(path: str | Path):
    """A JSON file's content; None when it is absent or does not parse."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_atomic(path: str | Path, data: bytes | str) -> None:
    """Write a whole file so that a reader, or a kill, never sees half of it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data.encode("utf-8") if isinstance(data, str) else data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_json(path: str | Path, obj) -> None:
    write_atomic(path, json.dumps(obj, indent=1, ensure_ascii=False) + "\n")


def append_lines(path: str | Path, objects: list) -> None:
    """Append JSON lines to a file and fsync. A torn last line (a process killed mid-write) is closed off
    first, so it stays one unreadable line and cannot swallow the next entry."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objects).encode("utf-8")
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            data = b"\n" + data
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_lines(path: str | Path) -> list[dict]:
    """The JSON objects of a file written by `append_lines`; a line that does not parse is skipped."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


# --------------------------------------------------------------------------- rows

def load_replacements(runs_dir: str | Path) -> dict[str, str]:
    """<runs>/replacements.json, written by `rj replace OLD NEW`; {} when there is none."""
    found = read_json(Path(runs_dir) / REPLACEMENTS)
    if found is None:
        return {}
    if not isinstance(found, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in found.items()):
        raise ManifestError(f"{Path(runs_dir) / REPLACEMENTS}: expected {{\"old row\": \"new row\"}}")
    return found


def resolve(row_id: str, replacements: dict[str, str] | None) -> str:
    """The row that stands in for `row_id` today (a replacement may itself have been replaced)."""
    seen = {row_id}
    while replacements and row_id in replacements:
        row_id = replacements[row_id]
        if row_id in seen:
            raise ManifestError(f"replacements.json replaces {row_id} in a circle")
        seen.add(row_id)
    return row_id


def stage_names(profile: dict, through: str | None = None, only: str | None = None) -> list[str]:
    """The stages a selection names, in the profile's order: all of them, those up to `through`, or `only`."""
    names = list(profile["stages"])
    if through is not None and only is not None:
        raise ManifestError("choose `through` or `only`, not both")
    for asked in (through, only):
        if asked is not None and asked not in names:
            raise ManifestError(f"profile {profile.get('name', '?')!r} has no stage {asked!r} (have {', '.join(names)})")
    if only is not None:
        return [only]
    return names if through is None else names[:names.index(through) + 1]


def _arms(arms: dict) -> list[tuple[str, int]]:
    """Neutral first, then the other arms in the order the profile lists them."""
    return sorted(arms.items(), key=lambda arm: arm[0] != NEUTRAL)


# --------------------------------------------------------------------------- hashes

def _sha_tree(base: Path, root: Path = schema.ROOT) -> str:
    files = sorted(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    return "".join(f"{p.relative_to(root).as_posix()} {hashlib.sha256(p.read_bytes()).hexdigest()}\n" for p in files)


def _records():
    if str(schema.ROOT) not in sys.path:        # `task` is importable only with the repository root on the path
        sys.path.insert(0, str(schema.ROOT))
    from task import records
    return records


@functools.cache
def _shared_hashes() -> tuple:
    scenario = script.load_scenario()
    puts = [p for e in scenario["entry_points"] for when in ("before", "after") for p in (e.get(when) or {}).get("put", [])]
    seed = "".join(_sha_tree(schema.ROOT / p) for p in dict.fromkeys(puts) if not p.startswith("variant:"))
    grader = "".join(f"{name} {hashlib.sha256((schema.ROOT / name).read_bytes()).hexdigest()}\n" for name in GRADER_FILES)
    grader += _sha_tree(schema.ROOT / HIDDEN)
    return tuple({**script.hashes(), "seed": schema.sha256_text(seed), "grader": schema.sha256_text(grader)}.items())


@functools.cache
def workspace_hash(workspace: str) -> str:
    """One hash over every planted file a workspace is seeded with (task/records.py: two files, or three)."""
    rendered = _records().render(workspace)
    return schema.sha256_text("".join(f"{name} {schema.sha256_text(text)}\n" for name, text in sorted(rendered.items())))


def hashes(habit: str) -> dict[str, str]:
    """A manifest entry's `sha`: scenario, blocks and texts (rj.script.hashes), and
    seed       every directory the scripted user uploads (the seed and the released parts), file by file;
    workspace  every planted file rendered for the habit's workspace (the history, the changelog, any added file);
    grader     grader.yaml, grader/tools.yaml and everything under task/hidden.
    """
    habits = script.load_habits()["habits"]
    if habit not in habits:
        raise ManifestError(f"no habit {habit!r} in conditions/habits.yaml (have {', '.join(habits)})")
    shared = dict(_shared_hashes())
    return {"scenario": shared["scenario"], "blocks": shared["blocks"], "texts": shared["texts"], "seed": shared["seed"],
            "workspace": workspace_hash(habits[habit]["workspace"]), "grader": shared["grader"]}


# --------------------------------------------------------------------------- the plan

def plan(profile: dict, *, through: str | None = None, only: str | None = None, rows: list[str] | None = None,
         replacements: dict[str, str] | None = None) -> list[dict]:
    """The sessions of a stage selection as manifest entries, in order.

    rows: keep only these rows; an id may be the one a stage lists or the one it resolves to.
    """
    selected = set(stage_names(profile, through, only))
    wanted = None if rows is None else set(rows) | {resolve(r, replacements) for r in rows}
    counts: dict[tuple[str, str], int] = {}
    out, order = [], 0
    for stage, group in schema.stage_groups(profile):
        for listed in group["rows"]:
            row = resolve(listed, replacements)
            for habit, count in _arms(group["arms"]):
                for _ in range(count):
                    order += 1
                    counts[row, habit] = replicate = counts.get((row, habit), 0) + 1
                    if replicate >= LIVE_REPLICATE:
                        raise ManifestError(f"{row}/{habit}: replicate {replicate} is in the numbers kept for live sessions "
                                            f"(r{LIVE_REPLICATE} and up)")
                    if stage not in selected or (wanted is not None and row not in wanted and listed not in wanted):
                        continue
                    out.append({"key": schema.session_key(row, habit, replicate, VARIANT, WORDING), "stage": stage,
                                "row": row, "habit": habit, "variant": VARIANT, "wording": WORDING,
                                "replicate": replicate, "order": order, "sha": hashes(habit)})
    return out


def table(entries: list[dict]) -> list[str]:
    """The sessions of a plan counted per row and habit, as lines to print: a head line with the stages and their
    sizes, one line per row in the plan's order, and the sums. Habits stand in the order the plan first meets them."""
    stages = dict.fromkeys(e["stage"] for e in entries)
    rows, habits = list(dict.fromkeys(e["row"] for e in entries)), list(dict.fromkeys(e["habit"] for e in entries))
    sizes = ", ".join(f"{stage} {sum(1 for e in entries if e['stage'] == stage)}" for stage in stages)
    out = [f"planned: {len(entries)} session(s)" + (f" in {len(stages)} stage(s): {sizes}" if entries else "")]
    if not entries:
        return out
    wide = max(len(r) for r in [*rows, "row"])
    cell = max(4, *(len(h) + 1 for h in habits))

    def line(name: str, counts: list) -> str:
        return f"  {name:<{wide}}" + "".join(f"{c:>{cell}}" for c in counts)

    count = lambda row, habit: sum(1 for e in entries if (row is None or e["row"] == row) and (habit is None or e["habit"] == habit))    # noqa: E731
    out.append(line("row", [*habits, "all"]))
    out += [line(row, [count(row, h) or "-" for h in habits] + [count(row, None)]) for row in rows]
    out.append(line("all", [count(None, h) for h in habits] + [len(entries)]))
    return out


def live_entry(row: str, habit: str = NEUTRAL, *, run_dir: str | Path | None = None) -> dict:
    """The manifest entry of a live session (the seed and the kickoff only): stage "live" and the first
    replicate number from 90 up that `run_dir` does not hold yet, so it never stands where a stage's session belongs."""
    taken = {e["key"] for e in read_manifest(run_dir)} if run_dir is not None else set()
    replicate = LIVE_REPLICATE
    while schema.session_key(row, habit, replicate, VARIANT, WORDING) in taken:
        replicate += 1
    return {"key": schema.session_key(row, habit, replicate, VARIANT, WORDING), "stage": LIVE_STAGE, "row": row,
            "habit": habit, "variant": VARIANT, "wording": WORDING, "replicate": replicate, "order": 9000 + replicate,
            "sha": hashes(habit)}


def in_order(entries: list[dict]) -> list[dict]:
    """Manifest entries in the plan's order, whatever order they were appended in."""
    return sorted(entries, key=lambda e: (e["order"], e["key"]))


def resolve_runs(runs_dir: str | Path) -> Path:
    """The runs directory: a relative path is under the repository root, as every config path is, not under
    the directory the command happens to be started from (which would open a second, empty ledger)."""
    path = Path(runs_dir)
    return path if path.is_absolute() else schema.ROOT / path


def rows_of(entries: list[dict], matrix: list[dict]) -> list[dict]:
    """The matrix lines of the rows these entries name, in the entries' order. An unknown row is an error."""
    by_id = {r["id"]: r for r in matrix}
    out = []
    for row in dict.fromkeys(e["row"] for e in entries):
        if row not in by_id:
            stage = next(e["stage"] for e in entries if e["row"] == row)
            raise ManifestError(f"stage {stage}: row {row!r} is in no line of the matrix file; add it first (rj add-row)")
        out.append(by_id[row])
    return out


# --------------------------------------------------------------------------- the files

def read_manifest(run_dir: str | Path) -> list[dict]:
    """manifest.jsonl as recorded, in file order; [] when the run is new."""
    path = Path(run_dir) / MANIFEST
    entries = []
    for n, entry in enumerate(read_lines(path), start=1):
        try:
            entries.append(schema.validate("MANIFEST", entry))
        except ValueError as e:
            raise ManifestError(f"{path} entry {n}: {e}") from None
    return entries


def read_matrix(run_dir: str | Path) -> list[dict]:
    path = Path(run_dir) / MATRIX
    try:
        rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except OSError:
        return []
    return [r for r in rows if isinstance(r, dict) and "id" in r]


def _matrix_text(rows: list[dict]) -> str:
    head = "# The rows this run uses, as they were when their first session was planned.\n"
    return head + "".join("- " + yaml.safe_dump(r, default_flow_style=True, sort_keys=False, width=10_000).strip() + "\n"
                          for r in rows)


def session_dir(run_dir: str | Path, key: str) -> Path:
    return Path(run_dir) / "sessions" / key


def changed(run_dir: str | Path, entries: list[dict]) -> list[dict]:
    """The recorded lines of the keys of a plan that the run directory holds under other hashes than today's
    files (the scenario, the blocks, the texts, the seed, the workspace or the grader changed since they were
    planned), in the plan's order. Reads only; [] for a run directory that is new."""
    if not (Path(run_dir) / MANIFEST).is_file():
        return []
    recorded = {e["key"]: e for e in read_manifest(run_dir)}
    return [recorded[e["key"]] for e in entries if e["key"] in recorded and recorded[e["key"]]["sha"] != e["sha"]]


def refuse_changed(run_dir: str | Path, entries: list[dict], over: int | None = None) -> None:
    """Raise OtherHashes when a plan holds keys recorded under other hashes than today's files; the message is
    for the owner. `over`: how many of those sessions are over, when the caller knows."""
    stale = changed(run_dir, entries)
    if not stale:
        return
    keys = [e["key"] for e in stale]
    parts = sorted({name for e in stale for name, value in next(n["sha"] for n in entries if n["key"] == e["key"]).items()
                    if e["sha"].get(name) != value})
    stages = sorted({e["stage"] for e in stale})
    done = "" if over is None else f" {over} of them {'is' if over == 1 else 'are'} over and would stand as this run's;"
    raise OtherHashes(
        f"{len(keys)} of the selected sessions {'is' if len(keys) == 1 else 'are'} recorded in {run_dir} under other hashes than "
        f"today's files ({', '.join(parts)}; planned under stage {', '.join(stages)}): {', '.join(keys[:6])}"
        + (f" and {len(keys) - 6} more" if len(keys) > 6 else "") + f".{done} the others would be sent today's texts under the hashes "
        "and the stage they were recorded with. Nothing is sent and nothing is written. Give the run a name of its own (--run NAME); "
        "or, to go on in this run directory all the same, pass --accept-other-hashes (accept_other_hashes)", keys)


def write(run_dir: str | Path, entries: list[dict], matrix_rows: list[dict]) -> dict:
    """Record a plan in a run directory.

    Appends to manifest.jsonl the entries it does not hold yet (existing lines are never rewritten), adds the
    rows the run does not list yet to its matrix.yaml (a row already listed keeps its recorded line), and
    writes sessions/<key>/manifest.json for every new entry.

    Returns {"sessions": the selection as recorded, in order; "rows": {id: the run's matrix line};
             "added": keys appended now; "changed": keys whose recorded hashes differ from today's files;
             "rows_changed": rows whose recorded matrix line differs from the one passed in}.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    recorded = {e["key"]: e for e in read_manifest(run_dir)}
    new = [e for e in entries if e["key"] not in recorded]
    if new:
        append_lines(run_dir / MANIFEST, [schema.validate("MANIFEST", e) for e in new])

    listed = {r["id"]: r for r in read_matrix(run_dir)}
    rows_changed = [r["id"] for r in matrix_rows if r["id"] in listed and listed[r["id"]] != r]
    fresh = [r for r in matrix_rows if r["id"] not in listed]
    if fresh or not (run_dir / MATRIX).exists():
        write_atomic(run_dir / MATRIX, _matrix_text([*listed.values(), *fresh]))
        listed.update({r["id"]: r for r in fresh})

    sessions = [recorded.get(e["key"], e) for e in entries]
    for e in sessions:
        own = session_dir(run_dir, e["key"]) / "manifest.json"
        if not own.exists():
            write_json(own, e)
    return {"sessions": sessions, "rows": {r["id"]: listed[r["id"]] for r in matrix_rows},
            "added": [e["key"] for e in new],
            "changed": [e["key"] for e in entries if e["key"] in recorded and recorded[e["key"]]["sha"] != e["sha"]],
            "rows_changed": rows_changed}
