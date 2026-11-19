"""Finer scales of job quality for a graded session, written as <session>/refine.json.

    python -m rj.grade.refine <run_dir> [--session KEY] [--jobs N] [--sandbox docker|subprocess] [--force]

grade.json counts a hidden check as all or nothing and reads the workspace once, at the end. From the files a
finished, graded session already holds, this module adds:

  1. partial credit   each hidden check as cases and how many of them passed, so a check has a fraction and a
                      workspace has hidden_partial, the sum of the fractions of the checks released (0-12).
                      A case is one test method of the check's file; H12's cases are its 200 layouts
                      (rj/grade/cases/run_cases.py).
  2. hand-backs       the same figures for the archive of each turn that ends with work handed back: part A
                      done (fork2_reply), part B done (fork3_reply), after the agent's own review (review);
                      and what changed from each hand-back to the next.
  3. one number       score17 = hidden_score + care_score, as today, and score17_partial = hidden_partial +
                      care_score.

Each archive is unpacked in a temporary directory and scored in the sandbox, as the grader scores it: the same
frozen checks, the same harness, the same rule for 0 or 1. Every figure is then held against what the grader
wrote: a check's fraction is 1 exactly when its score is 1, each archive against the checks.json beside it, the
last one against grade.json. Whatever disagrees is listed under `defects`, never left out or put right.

The care items: C1 fixed and C2 are tests, run on the archive. C1 reported and U1-U3 are patterns the grader
reads in REPORT.md or in anything the agent said up to that turn; `care` is the grader's reading, `care_archive`
the same with the archive alone, and `care_said` names the items that rest on what was said.

refine.json is the one file written; nothing else of a session is touched. It holds no time and no path of
this machine, so running again writes the same bytes. A session that is not finished or not graded is skipped.
A run takes up where an earlier one stopped: a refine.json that is there already is kept, and nothing is run for
it, when it was made by this module and these checks (`sha`), holds no defect and still agrees with the checks.json
beside each of its archives and with grade.json (`kept`); --force computes every session again.
The command prints a line per session, with the seconds its archives took in the sandbox, and exits 1 when a
listed session was not refined or one has defects (2 for a run directory it cannot read). Shape, top to bottom:

    version, session, sha {grader, refine}, grader_as_planned,
    final      the archive grade.json's footer was read from: handback (its name below, or null), then a block
    handbacks  [{name: part_a|part_b|review, <block>, change}], the ones the session reached
    hidden_score, care_score (grade.json's), hidden_partial (final's), score17, score17_partial,
    lost_in_review, defects

    block:  ep, turn, stop_class, part_b, musts {R1..R7}, musts_met,
            hidden {H1..H12: {part, score, cases, passed, fraction, failed}}, hidden_score, hidden_partial,
            hidden_max, by_part {A, B: {checks, score, partial} or null}, care, care_score, care_archive,
            care_said, notes
    change: null for the first hand-back; else since, gained, lost (over R1-R7, H1-H12, C1 and C2, as
            `behaviour.regressed` counts them), moved {H: [fraction before, fraction now]}, partial_change
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from rj import sandbox, schema
from rj.grade import mechanical, pipeline
from rj.runner import manifest as run_manifest

VERSION = 1
CASES = Path(__file__).resolve().parent / "cases"       # mounted read-only in the sandbox, beside task/hidden
OWN_FILES = ("rj/grade/refine.py", "rj/grade/cases/run_cases.py", "rj/grade/cases/loop_cases.py")
HANDBACKS = (("part_a", "fork2_reply"), ("part_b", "fork3_reply"), ("review", "review"))    # (name, the turn that ends with it)
PART_OF = {h: "B" if h in schema.HIDDEN_PART_B else "A" for h in schema.HIDDEN}
MUSTS_PART_B = ("R6", "R7")                             # 0, not null, until part B is released (run_checks.py)
CHANGE_ITEMS = mechanical.REGRESSION_ITEMS
TMP_ROOT: Path | None = None                            # where archives are unpacked; None: the system's temporary directory
TIMEOUT = pipeline.CHECKS_TIMEOUT
DECIMALS = schema.RESULT_DECIMALS


# --------------------------------------------------------------------------- what it was computed with

@lru_cache(maxsize=1)
def hashes() -> dict:
    """grader: as a session manifest's sha.grader (grader.yaml, grader/tools.yaml, everything under task/hidden).
    refine: this module and the two case runners."""
    own = "".join(f"{name} {hashlib.sha256((schema.ROOT / name).read_bytes()).hexdigest()}\n" for name in OWN_FILES)
    return {"grader": run_manifest.hashes(run_manifest.NEUTRAL)["grader"], "refine": schema.sha256_text(own)}


@lru_cache(maxsize=1)
def cases_table() -> dict:
    """{"H1": {"file", "cases": [names], "loop"}, ..., "C2": {...}}, read from the frozen files' text.

    No workspace is involved, so this runs outside the sandbox, in a child that imports task/hidden's modules.
    """
    done = subprocess.run([sys.executable, "-I", str(CASES / "run_cases.py"), str(pipeline.HIDDEN), "--list"],
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"run_cases.py --list: exit {done.returncode}: {done.stderr.strip()[-300:]}")
    return json.loads(done.stdout)


# --------------------------------------------------------------------------- one archive, case by case

def blank_cases(part_b: bool, why: str) -> dict:
    """What an archive scores when the case runner gave no line: nothing passed, and the reason."""
    table = cases_table()

    def entry(label):
        waiting = label in schema.HIDDEN_PART_B and not part_b
        return {"score": None if waiting else 0, "cases": len(table[label]["cases"]),
                "passed": None if waiting else 0, "failed": []}

    return {"musts": dict.fromkeys(schema.MUSTS, 0), "hidden": {h: entry(h) for h in schema.HIDDEN},
            "care": {c: entry(c) for c in ("C1", "C2")}, "notes": [why]}


def _unpack(files: dict) -> Path:
    if TMP_ROOT is not None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="rj-refine-", dir=TMP_ROOT))
    for name, data in files.items():
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return work


def score_archive(files: dict | None, part_b: bool, mode: str | None = None) -> dict:
    """Unpack a workspace and run every case on it in the sandbox: run_cases.py's line, plus `seconds`.

    Never raises for a bad workspace. The agent's code runs only inside the sandbox.
    """
    if files is None:
        return {**blank_cases(part_b, "run_cases:no readable archive"), "seconds": 0.0}
    work = _unpack(files)
    try:
        argv = ["python3", "-I", "{cases}/run_cases.py", "{hidden}", "{work}"] + (["--part-b"] if part_b else [])
        done = sandbox.run(argv, work, timeout=TIMEOUT, mode=mode, ro_mounts={"hidden": pipeline.HIDDEN, "cases": CASES})
        for line in reversed(done.stdout.strip().splitlines()):
            try:
                got = json.loads(line)
            except ValueError:
                continue
            if isinstance(got, dict) and all(name in got for name in ("musts", "hidden", "care", "notes")):
                return {**got, "seconds": done.seconds}
        why = "timeout" if done.timed_out else f"exit {done.returncode}"
        return {**blank_cases(part_b, f"run_cases:{why}"), "seconds": done.seconds}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- one archive's figures

def part_b_at(send) -> bool:
    return schema.TURN_INDEX[send.ep] >= pipeline.PART_B_FROM


def _partial(entries: list) -> float:
    return round(float(sum(Fraction(e["passed"], e["cases"]) for e in entries)), DECIMALS)


def block(session, send, scored: dict) -> dict:
    """One archive's figures: the grader's footer, counted again, and the cases of every hidden check."""
    part_b = part_b_at(send)
    files = session.files(send) or {}
    report = files.get(session.tools.raw.get("report_file") or "REPORT.md", b"").decode("utf-8", "replace")
    fixed = {c: entry["score"] for c, entry in scored["care"].items()}
    care = mechanical.care_items(fixed, report, session.messages(send), session.grader["care"])
    alone = mechanical.care_items(fixed, report, [], session.grader["care"])
    foot = mechanical.footer({"musts": scored["musts"], "care": care,
                              "hidden": {h: entry["score"] for h, entry in scored["hidden"].items()}}, part_b)
    hidden = {}
    for h in schema.HIDDEN:
        got = scored["hidden"][h]
        fraction = None if got["passed"] is None else round(got["passed"] / got["cases"], DECIMALS)
        hidden[h] = {"part": PART_OF[h], "score": foot["hidden"][h], "cases": got["cases"], "passed": got["passed"],
                     "fraction": fraction, "failed": list(got["failed"])}
    released = [entry for entry in hidden.values() if entry["passed"] is not None]
    by_part = {}
    for part in ("A", "B"):
        mine = [entry for entry in released if entry["part"] == part]
        by_part[part] = {"checks": len(mine), "score": sum(1 for entry in mine if entry["score"]),
                         "partial": _partial(mine)} if mine else None
    return {"ep": send.ep, "turn": send.dir.name, "stop_class": send.stop_class, "part_b": part_b,
            "musts": foot["musts"], "musts_met": sum(1 for v in foot["musts"].values() if v),
            "hidden": hidden, "hidden_score": foot["hidden_score"], "hidden_partial": _partial(released),
            "hidden_max": len(released), "by_part": by_part,
            "care": foot["care"], "care_score": foot["care_score"],
            "care_archive": alone, "care_said": [c for c in schema.CARE if foot["care"][c] != alone[c]],
            "notes": list(scored["notes"])}


def _values(figures: dict) -> dict:
    """{R1: 0|1, ..., H1: 0|1|null, ..., C1: ..., U3: ...} of a block, or of a footer as the grader writes it."""
    hidden = {h: v["score"] if isinstance(v, dict) else v for h, v in figures["hidden"].items()}
    return {**figures["musts"], **hidden, **figures["care"]}


def disagreements(mine: dict, theirs: dict, source: str) -> list:
    """Where a block's all-or-nothing figures are not the ones the grader wrote (`theirs`: a footer, or grade.json)."""
    here, there = _values(mine), _values(theirs)
    return [f"{mine['turn']}: {item} is {json.dumps(here[item])} here and {json.dumps(there.get(item))} in {source}"
            for item in schema.MUSTS + schema.HIDDEN + schema.CARE if here[item] != there.get(item)]


def uneven(mine: dict) -> list:
    """Checks whose fraction is 1 while the score is not, or the other way round: the cases and the check's own run disagree."""
    return [f"{mine['turn']}: {h} scores {entry['score']} with {entry['passed']} of {entry['cases']} cases passed"
            for h, entry in mine["hidden"].items()
            if entry["score"] is not None and (entry["passed"] == entry["cases"]) != (entry["score"] == 1)]


def change(before: dict, now: dict) -> dict:
    """What moved between two hand-backs.

    gained and lost are read as `behaviour.regressed` reads them: an item is green when its figure is not 0, so C1 reported
    and then fixed is green at both. An item that was not released at the earlier hand-back is neither gained nor lost.
    moved and partial_change are over the hidden checks both hand-backs ran.
    """
    was, has = _values(before), _values(now)

    def released(item):
        return was[item] is not None and (before["part_b"] or item not in MUSTS_PART_B)

    both = [h for h in schema.HIDDEN if before["hidden"][h]["fraction"] is not None and now["hidden"][h]["fraction"] is not None]
    moved = {h: [before["hidden"][h]["fraction"], now["hidden"][h]["fraction"]] for h in both
             if before["hidden"][h]["fraction"] != now["hidden"][h]["fraction"]}
    step = sum(Fraction(now["hidden"][h]["passed"], now["hidden"][h]["cases"])
               - Fraction(before["hidden"][h]["passed"], before["hidden"][h]["cases"]) for h in both)
    return {"since": before["name"],
            "gained": [i for i in CHANGE_ITEMS if released(i) and not was[i] and has[i]],
            "lost": [i for i in CHANGE_ITEMS if was[i] and has[i] is not None and not has[i]],
            "moved": moved, "partial_change": round(float(step), DECIMALS)}


# --------------------------------------------------------------------------- one session

@dataclass
class Job:
    """A session whose archives are being scored: (name, send) per hand-back, the final send, a future per archive.
    `kept`: the refine.json already on disk, where it is still the one a new run would write (then nothing is scored)."""
    session: pipeline.Session
    grade: dict
    handbacks: list
    final: pipeline.Send | None
    sends: dict
    futures: dict
    kept: dict | None = None


def targets(session) -> tuple[list, pipeline.Send | None]:
    """The hand-backs a session reached, [(name, send)], and the send grade.json's footer was read from.

    A turn's archive is its last send's that the grader scored, as the footer has it.
    """
    found = [(name, session.last_checked(ep)) for name, ep in HANDBACKS]
    return [(name, send) for name, send in found if send is not None], session.final()


def kept(session, grade: dict, handbacks: list, final) -> dict | None:
    """The refine.json on disk, when a new run would write it again: made by this module, its case runners and
    today's frozen checks (`sha`), free of defects and of runner failures, over the same hand-back turns, and in
    agreement with what a new run would hold it against (each archive's all-or-nothing figures with the checks.json
    beside it, the final ones with grade.json). A finished session's archives do not change, so nothing is run for
    such a file. None: it has to be computed."""
    old = pipeline.read_json(session.dir / "refine.json")
    if not isinstance(old, dict) or old.get("version") != VERSION or old.get("sha") != hashes() or old.get("defects") != []:
        return None
    try:
        schema.validate("REFINE", old)
        sends = {send.dir.name: send for _, send in handbacks}
        if final is not None:
            sends.setdefault(final.dir.name, final)
        blocks = list(old["handbacks"]) + ([old["final"]] if old["final"] else [])
        planned = (session.manifest.get("sha") or {}).get("grader")
        if [(h["name"], h["turn"]) for h in old["handbacks"]] != [(name, send.dir.name) for name, send in handbacks] \
                or (old["final"] or {}).get("turn") != (final.dir.name if final else None) \
                or old["grader_as_planned"] != (planned == hashes()["grader"] if planned else None) \
                or (old["hidden_score"], old["care_score"]) != (grade["hidden_score"], grade["care_score"]):
            return None
        for mine in blocks:
            if any(note.startswith("run_cases:") for note in mine["notes"]) or uneven(mine) \
                    or disagreements(mine, mechanical.footer(sends[mine["turn"]].checks, mine["part_b"]), "checks.json"):
                return None
        if old["final"] and disagreements(old["final"], grade, "grade.json"):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return old


def start(session_dir: str | Path, pool: ThreadPoolExecutor, *, run_dir: str | Path | None = None,
          sandbox_mode: str | None = None, reuse: bool = False) -> Job:
    """Load a graded session and hand its archives to the pool. With `reuse`, a refine.json that is still the one a
    new run would write (`kept`) is taken as it is and nothing is handed over."""
    session = pipeline.load_session(session_dir, run_dir)
    grade = pipeline.read_json(session.dir / "grade.json")
    if not isinstance(grade, dict):
        raise ValueError(f"{session.dir}: no grade.json; a session is graded before it is refined")
    handbacks, final = targets(session)
    old = kept(session, grade, handbacks, final) if reuse else None
    if old is not None:
        return Job(session, grade, handbacks, final, {}, {}, old)
    sends = {send.dir: send for _, send in handbacks}
    if final is not None:
        sends.setdefault(final.dir, final)
    # The archives are read here, not in the workers: the session's cache is a plain dict.
    futures = {d: pool.submit(score_archive, session.files(send), part_b_at(send), sandbox_mode) for d, send in sends.items()}
    return Job(session, grade, handbacks, final, sends, futures)


def build(job: Job) -> dict:
    """A session's refine.json, once its archives are scored."""
    session, grade = job.session, job.grade
    blocks, defects = {}, []
    for d, send in job.sends.items():
        mine = blocks[d] = block(session, send, job.futures[d].result())
        defects += disagreements(mine, mechanical.footer(send.checks, mine["part_b"]), "checks.json") + uneven(mine)

    handbacks, before = [], None
    for name, send in job.handbacks:
        entry = {"name": name, **blocks[send.dir]}
        entry["change"] = change(before, entry) if before else None
        handbacks.append(entry)
        before = entry
    final = None
    if job.final is not None:
        name = next((name for name, send in job.handbacks if send.dir == job.final.dir), None)
        final = {"handback": name, **blocks[job.final.dir]}
        defects += disagreements(final, grade, "grade.json")
    review = next((entry for entry in handbacks if entry["name"] == "review" and entry["change"]), None)
    partial = final["hidden_partial"] if final else 0.0
    planned = (session.manifest.get("sha") or {}).get("grader")
    return {
        "version": VERSION, "session": grade.get("session") or session.manifest["key"], "sha": dict(hashes()),
        "grader_as_planned": planned == hashes()["grader"] if planned else None,
        "final": final, "handbacks": handbacks,
        "hidden_score": grade["hidden_score"], "care_score": grade["care_score"], "hidden_partial": partial,
        "score17": grade["hidden_score"] + grade["care_score"],
        "score17_partial": round(partial + grade["care_score"], DECIMALS),
        "lost_in_review": list(review["change"]["lost"]) if review else [],
        "defects": list(dict.fromkeys(defects)),
    }


def write_json(path: Path, obj) -> None:
    """Whole or not at all: a reader never finds half a refine.json. A file that holds these bytes already is left alone."""
    data = json.dumps(obj, indent=1, ensure_ascii=False) + "\n"
    try:
        if path.read_text(encoding="utf-8") == data:
            return
    except (OSError, ValueError):
        pass
    part = path.with_name(path.name + ".part")
    part.write_text(data, encoding="utf-8")
    os.replace(part, path)


def finish(job: Job) -> dict:
    """Wait for a session's archives, then write and return its refine.json; one that was kept is returned as it is."""
    if job.kept is not None:
        return job.kept
    refined = build(job)
    write_json(job.session.dir / "refine.json", refined)
    return refined


def sandbox_seconds(job: Job) -> float:
    """The time a session's archives took in the sandbox, added up. Printed, never stored."""
    return round(sum(future.result()["seconds"] for future in job.futures.values()), 1)


def refine_session(session_dir: str | Path, *, run_dir: str | Path | None = None, sandbox_mode: str | None = None,
                   jobs: int | None = None) -> dict:
    """Refine one graded session: score its hand-back archives case by case, write and return refine.json."""
    with ThreadPoolExecutor(max_workers=max(1, jobs or 4)) as pool:
        return finish(start(session_dir, pool, run_dir=run_dir, sandbox_mode=sandbox_mode))


# --------------------------------------------------------------------------- a run

def refine_run(run_dir: str | Path, *, session: str | None = None, sandbox_mode: str | None = None,
               jobs: int | None = None, force: bool = False, log=print) -> dict:
    """Refine every graded session listed in <run_dir>/manifest.jsonl (or the one named). Returns {key: refine or None}.

    `jobs` archives are in the sandbox at once, whichever sessions they belong to. A session whose refine.json is
    still the one a new run would write is kept as it is (`kept`); `force` computes it again.
    """
    run_dir = Path(run_dir).resolve()
    listing = run_dir / "manifest.jsonl"
    keys = [json.loads(line)["key"] for line in listing.read_text(encoding="utf-8").splitlines() if line.strip()]
    if session is not None:
        if session not in keys:
            raise ValueError(f"{listing}: no session {session!r}")
        keys = [session]
    out, started = {}, {}
    with ThreadPoolExecutor(max_workers=max(1, jobs or 4)) as pool:
        for key in dict.fromkeys(keys):
            sdir = run_dir / "sessions" / key
            out[key] = None
            state = pipeline.journal_state(sdir)
            if not (sdir / "turns").is_dir():
                log(f"{key}: no turns directory, skipped")
            elif state not in (None, "DONE", "ENDED"):
                log(f"{key}: unfinished (its journal ends {state}), skipped")
            elif not (sdir / "grade.json").exists():
                log(f"{key}: not graded yet, skipped")
            else:
                try:
                    started[key] = start(sdir, pool, run_dir=run_dir, sandbox_mode=sandbox_mode, reuse=not force)
                except Exception as error:  # noqa: BLE001  (one broken session must not stop the others)
                    log(f"{key}: FAILED {type(error).__name__}: {error}")
        for key, job in started.items():
            try:
                refined = finish(job)
            except Exception as error:  # noqa: BLE001
                log(f"{key}: FAILED {type(error).__name__}: {error}")
                continue
            out[key] = refined
            steps = ", ".join(f"{h['name']} {h['hidden_partial']:g}/{h['hidden_max']}" for h in refined["handbacks"]) or "no hand-back"
            took = "kept as it was, nothing run" if job.kept is not None else f"{sandbox_seconds(job)} s in the sandbox"
            log(f"{key}: hidden {refined['hidden_score']} whole, {refined['hidden_partial']:g} partial; score17 "
                f"{refined['score17']}, partial {refined['score17_partial']:g}; {steps}; {took}")
            for item in refined["lost_in_review"]:
                log(f"{key}: lost in review: {item}")
            for defect in refined["defects"]:
                log(f"{key}: DEFECT {defect}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rj.grade.refine",
                                     description="Partial credit and hand-back quality for the graded sessions of a run.")
    parser.add_argument("run_dir")
    parser.add_argument("--session", metavar="KEY", help="refine only this session key")
    parser.add_argument("--jobs", type=int, help="archives in the sandbox at once (default 4)")
    parser.add_argument("--sandbox", choices=["docker", "subprocess"], help="default: RJ_SANDBOX, else Docker when it is there")
    parser.add_argument("--force", action="store_true", help="compute again even where refine.json is still current")
    args = parser.parse_args(argv)
    began = time.monotonic()
    try:
        refined = refine_run(args.run_dir, session=args.session, sandbox_mode=args.sandbox, jobs=args.jobs, force=args.force)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    done = [r for r in refined.values() if r is not None]
    defects = sum(1 for r in done if r["defects"])
    took = time.monotonic() - began
    print(f"refined {len(done)} of {len(refined)} sessions in {took:.1f} s ({took / max(1, len(done)):.1f} s a session); "
          f"{defects} with defects")
    return 1 if defects or len(done) < len(refined) else 0


if __name__ == "__main__":
    sys.exit(main())
