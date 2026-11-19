"""Grade one session directory into grade.json. Plan sections 9 and 10.

    python -m rj.grade.pipeline <run_dir> [--session KEY] [--rerun-checks] [--sandbox docker|subprocess] [--jobs N]

Two stages:
  a. checks   every send of the six entry points that has an archive.zip and no checks.json is unpacked and
              scored by task/hidden/run_checks.py in the sandbox; the result is written beside the archive as
              checks.json. A checks.json that is already there is used as it is (--rerun-checks replaces it).
  b. grade    the footer, fork codes, integrity and behaviour codes and the effort signals, from checks.json,
              the replies, the stop files, the requests and the archives. Nothing in this stage runs code.

What a session directory must hold (the runner writes it): manifest.json, and per send turns/NN-<ep>.sK/ with
request.json (`input`: the text sent), response.json (the raw reply), stop.json and archive.zip (the workspace,
its root at the root of the zip). A run that carries uploads.json ({entry point: [paths put before it]}) and
seed.json ({path: text}) is graded against those; otherwise the seed and the uploads are read from the
scenario's `put` directories under task/.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from rj import sandbox, schema, script
from rj.grade import effort, mechanical, stops, trace

EPS = schema.ENTRY_POINTS
HIDDEN = schema.ROOT / "task" / "hidden"
TMP_ROOT = schema.ROOT / "runs" / ".grade-tmp"      # under the repository, so Docker can mount it
PROBES = ("part_a_green", "part_b_green", "fork2_option", "fork3_option")
PART_B_FROM = schema.TURN_INDEX["handback_reply"]   # part B is in the workspace from this entry point on
MEMBER_LIMIT = 32 * 1024 * 1024                     # a larger file in an archive is not source; it is left out
CHECKS_TIMEOUT = 180


# --------------------------------------------------------------------------- what a session left on disk

@dataclass
class Send:
    ep: str
    send: int
    dir: Path
    stop: dict | None = None
    response: dict | None = None
    request: dict | None = None
    checks: dict | None = None
    row: dict | None = None

    @property
    def stop_class(self) -> str | None:
        return (self.stop or {}).get("stop_class")

    @property
    def archive(self) -> Path:
        return self.dir / "archive.zip"


@dataclass
class Step:
    """One archive beside the one before it. `uploaded`: what the user put in between ({path: bytes or None})."""
    send: Send
    before: dict
    after: dict
    uploaded: dict


@dataclass
class Session:
    dir: Path
    run_dir: Path | None
    manifest: dict
    workspace: str
    sends: list
    items: list
    tools: trace.Tools
    grader: dict
    scenario: dict
    seed: dict                      # {path: bytes}: the repository as the user gave it
    uploads: dict                   # {entry point: {path: bytes, or None when only the path is known}}
    _archives: dict = field(default_factory=dict, repr=False)
    _steps: list | None = field(default=None, repr=False)

    @property
    def entry_sends(self) -> list:
        """The sends of the six entry points, in the order they were sent (seed and canary left out)."""
        return [s for s in self.sends if s.ep in EPS]

    def of(self, ep: str) -> list:
        return [s for s in self.sends if s.ep == ep]

    def files(self, send: Send) -> dict | None:
        """{path: bytes} of a send's archive; None when it has none or it cannot be read."""
        if send.dir not in self._archives:
            ignored = self.grader.get("ignored_in_archive") or []
            self._archives[send.dir] = read_archive(send.archive, ignored, Path(self.tools.root).name)
        return self._archives[send.dir]

    def protected(self, path: str) -> bool:
        return schema.matches_any(path, self.grader.get("protected") or [])

    def in_repo(self, path: str) -> bool:
        """False for the agent's own home and for any dot-path: tool state, not work on the task."""
        return trace.in_repo(path, self.tools) and not any(part.startswith(".") for part in path.split("/"))

    def baseline(self) -> dict:
        """The workspace before kickoff: the seed turn's archive if it holds the whole seed, else the seed itself."""
        seed_turn = next(iter(self.of("seed")), None)
        files = self.files(seed_turn) if seed_turn else None
        if files is not None and all(path in files for path in self.seed):
            return files
        return dict(self.seed)

    def steps(self) -> list:
        """Each archive of the six entry points with its predecessor and the uploads that came between."""
        if self._steps is None:
            out, prev, pending, started = [], self.baseline(), {}, set()
            for send in self.entry_sends:
                if send.ep not in started:
                    started.add(send.ep)
                    pending.update(self.uploads.get(send.ep) or {})
                files = self.files(send)
                if files is None:
                    continue
                out.append(Step(send, prev, files, pending))
                prev, pending = files, {}
            self._steps = out
        return self._steps

    def last_checked(self, ep: str) -> Send | None:
        return next((s for s in reversed(self.of(ep)) if s.checks), None)

    def final(self) -> Send | None:
        """The send the footer is read from: the review archive, else the last archive that was scored."""
        return self.last_checked("review") or next((s for s in reversed(self.entry_sends) if s.checks), None)

    def messages(self, upto: Send | None = None) -> list:
        """What the agent said (messages and questions, not reasoning), up to and including a send."""
        last = None if upto is None else (schema.TURN_INDEX.get(upto.ep, 99), upto.send)
        return [i["text"] or "" for i in self.items if i["kind"] == "message"
                and (last is None or (schema.TURN_INDEX.get(i["ep"], 99), i["send"]) <= last)]


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _clean_name(name: str) -> str | None:
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    return None if not parts or ".." in parts else "/".join(parts)


def read_archive(path: Path, ignored: list, root_name: str = "workspace") -> dict | None:
    """A workspace zip as {path: bytes}, without ignored paths, links and anything that would leave the root."""
    try:
        with zipfile.ZipFile(path) as z:
            found = {}
            for info in z.infolist():
                name = _clean_name(info.filename)
                is_link = (info.external_attr >> 16) & 0o170000 == 0o120000
                if info.is_dir() or name is None or is_link or info.file_size > MEMBER_LIMIT:
                    continue
                found[name] = z.read(info)
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
        return None
    if found and all(name.startswith(root_name + "/") for name in found):      # zipped with its top directory
        found = {name[len(root_name) + 1:]: data for name, data in found.items()}
    return {name: data for name, data in found.items() if not schema.matches_any(name, ignored)}


def read_tree(root: Path, ignored: list) -> dict:
    if not root.is_dir():
        return {}
    found = {p.relative_to(root).as_posix(): p for p in sorted(root.rglob("*")) if p.is_file()}
    return {name: p.read_bytes() for name, p in found.items() if not schema.matches_any(name, ignored)}


def _records():
    if str(schema.ROOT) not in sys.path:        # `task` is importable only with the repository root on the path
        sys.path.insert(0, str(schema.ROOT))
    from task import records
    return records


def load_seed(run_dir: Path | None, workspace: str, scenario: dict, ignored: list) -> dict:
    """The repository before kickoff: the run's seed.json, else the scenario's seed `put` list under task/."""
    stored = read_json(run_dir / "seed.json") if run_dir else None
    if isinstance(stored, dict):
        return {path: str(text).encode("utf-8") for path, text in stored.items()}
    entry = next((e for e in scenario["entry_points"] if e["id"] == "seed"), {})
    seed = {}
    for put in (entry.get("after") or {}).get("put") or ["task/slotbook", "variant:{workspace}"]:
        if put.startswith("variant:"):
            try:
                rendered = _records().render(workspace)
            except KeyError:
                rendered = _records().render("plain")
            seed.update({name: text.encode("utf-8") for name, text in rendered.items()})
        else:
            seed.update(read_tree(schema.ROOT / put, ignored))
    return seed


def load_uploads(run_dir: Path | None, scenario: dict, ignored: list) -> dict:
    """{entry point: {path: content}} for what the user puts before an entry point. uploads.json gives paths only."""
    stored = read_json(run_dir / "uploads.json") if run_dir else None
    if isinstance(stored, dict):
        return {ep: dict.fromkeys(paths) for ep, paths in stored.items()}
    out = {}
    for entry in scenario["entry_points"]:
        files = {}
        for put in (entry.get("before") or {}).get("put") or []:
            if not put.startswith("variant:"):
                files.update(read_tree(schema.ROOT / put, ignored))
        if files:
            out[entry["id"]] = files
    return out


def find_run_dir(session_dir: Path) -> Path | None:
    """The run a session directory belongs to: the parent of the nearest sessions/, or one holding manifest.jsonl."""
    for parent in session_dir.parents:
        if parent.name == "sessions":
            return parent.parent
        if (parent / "manifest.jsonl").exists():
            return parent
    return None


def load_manifest(session_dir: Path, run_dir: Path | None) -> dict:
    own = read_json(session_dir / "manifest.json")
    if isinstance(own, dict) and own.get("key"):
        return own
    key = None
    if run_dir is not None:
        try:
            key = session_dir.relative_to(run_dir / "sessions").as_posix()
        except ValueError:
            key = None
        listing = run_dir / "manifest.jsonl"
        for line in listing.read_text(encoding="utf-8").splitlines() if listing.exists() else []:
            entry = json.loads(line) if line.strip() else {}
            if key and entry.get("key") == key:
                return entry
    key = key or "/".join(session_dir.parts[-5:])
    try:
        return {"key": key, **schema.parse_key(key)}
    except (ValueError, IndexError):
        raise ValueError(f"{session_dir}: no manifest.json and the path is not a session key") from None


def load_session(session_dir: str | Path, run_dir: str | Path | None = None) -> Session:
    session_dir = Path(session_dir).resolve()
    run_dir = Path(run_dir).resolve() if run_dir else find_run_dir(session_dir)
    manifest = load_manifest(session_dir, run_dir)
    grader, tools, scenario = schema.load_grader(), trace.load_tools(), script.load_scenario()
    try:
        workspace = script.load_habits()["habits"][manifest.get("habit")]["workspace"]
    except (KeyError, TypeError):
        workspace = "plain"
    ignored = grader.get("ignored_in_archive") or []
    sends = [Send(ep, n, d, stop=read_json(d / "stop.json"), response=read_json(d / "response.json"),
                  request=read_json(d / "request.json"), checks=read_json(d / "checks.json"),
                  row=read_json(d / "turns_row.json"))
             for ep, n, d in trace.send_dirs(session_dir)]
    return Session(dir=session_dir, run_dir=run_dir, manifest=manifest, workspace=workspace, sends=sends,
                   items=trace.session_items(session_dir, tools), tools=tools, grader=grader, scenario=scenario,
                   seed=load_seed(run_dir, workspace, scenario, ignored),
                   uploads=load_uploads(run_dir, scenario, ignored))


def grounding_read(session_dir: str | Path, run_dir: str | Path | None = None) -> dict:
    """effort.grounding_read for one session directory: {"first_edit", "grounding_existing"}, both None where the
    session's files cannot be read. No archive is opened: the replies, the seed and the uploads say it."""
    try:
        session = load_session(session_dir, run_dir)
    except (OSError, ValueError, KeyError, TypeError):
        return {"first_edit": None, "grounding_existing": None}
    there, known = set(session.seed), {}
    for ep in EPS:                                  # what the user had given by each entry point
        there = there | set(session.uploads.get(ep) or {})
        known[ep] = there
    calls = [i for i in session.items if i["kind"] == "tool_call" and i["ep"] in EPS]
    return effort.grounding_read(calls, session.tools, lambda ep: known.get(ep, set()))


# --------------------------------------------------------------------------- stage a: checks.json per archive

def blank_checks(part_b: bool, why: str) -> dict:
    return {"musts": dict.fromkeys(schema.MUSTS, 0),
            "hidden": {h: (0 if part_b or h not in schema.HIDDEN_PART_B else None) for h in schema.HIDDEN},
            "care": {"C1": 0, "C2": 0}, "probes": {}, "ci_exit": None, "failed": [why]}


def _unpack(files: dict) -> Path:
    for _ in range(5):          # a grader that just finished may remove the empty parent in between
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        try:
            work = Path(tempfile.mkdtemp(prefix="work-", dir=TMP_ROOT))
            break
        except FileNotFoundError:
            continue
    else:
        raise OSError(f"cannot create a directory under {TMP_ROOT}")
    for name, data in files.items():
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return work


def score_workspace(files: dict, part_b: bool, mode: str | None = None) -> dict:
    """Unpack a workspace under the repository and run the hidden checks on it. Never raises for a bad workspace."""
    work = _unpack(files)
    try:
        argv = ["python3", "-I", "{hidden}/run_checks.py", "{work}"] + (["--part-b"] if part_b else [])
        done = sandbox.run(argv, work, timeout=CHECKS_TIMEOUT, mode=mode, ro_mounts={"hidden": HIDDEN})
        result = None
        for line in reversed(done.stdout.strip().splitlines()):
            try:
                got = json.loads(line)
            except ValueError:
                continue
            if isinstance(got, dict) and "musts" in got and "hidden" in got:
                result = got
                break
        if result is None:
            why = "timeout" if done.timed_out else f"exit {done.returncode}: {(done.stderr or done.stdout).strip()[-200:]}"
            result = blank_checks(part_b, f"run_checks:{why}")
        probes = dict(result.get("probes") or {})
        for name in PROBES:     # run_checks.py reports all four; a probe is run on its own only where it did not
            if name not in probes:
                got = sandbox.run_probe(name, work, HIDDEN / "probes", mode=mode)
                probes[name] = bool(got["ok"]) if name.endswith("_green") else got["option"]
        result["probes"] = probes
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            TMP_ROOT.rmdir()        # leaves nothing behind once the last workspace is gone
        except OSError:
            pass


def checks_for(session: Session, send: Send, mode: str | None = None) -> dict | None:
    """Score one send's archive and read the care items that are judged from text. None: no readable archive."""
    files = session.files(send)
    if files is None:
        return None
    part_b = schema.TURN_INDEX[send.ep] >= PART_B_FROM
    result = score_workspace(files, part_b, mode)
    report = files.get(session.tools.raw.get("report_file") or "REPORT.md", b"").decode("utf-8", "replace")
    care = mechanical.care_items(result.get("care") or {}, report, session.messages(send), session.grader["care"])
    checks = {"musts": result.get("musts") or {}, "hidden": result.get("hidden") or {}, "care": care,
              "probes": result["probes"], "ci_exit": result.get("ci_exit"), "failed": result.get("failed") or [],
              "part_b": part_b}
    return schema.validate("CHECKS", checks)


def ensure_checks(session: Session, *, rerun: bool = False, mode: str | None = None, jobs: int | None = None) -> int:
    """Stage a. Returns the number of archives scored now."""
    todo = [s for s in session.entry_sends if s.archive.exists() and (rerun or s.checks is None)]
    if not todo:
        return 0
    for send in todo:           # read the archives here, not in the workers: the session's cache is a plain dict
        session.files(send)
    with ThreadPoolExecutor(max_workers=max(1, min(jobs or 4, len(todo)))) as pool:
        scored = list(pool.map(lambda s: checks_for(session, s, mode), todo))
    done = 0
    for send, checks in zip(todo, scored):
        if checks is not None:
            write_json(send.dir / "checks.json", checks)
            send.checks = checks
            done += 1
    return done


# --------------------------------------------------------------------------- stage b: grade.json

def build_grade(session: Session) -> dict:
    final = session.final()
    part_b = final is not None and schema.TURN_INDEX[final.ep] >= PART_B_FROM
    integrity = mechanical.integrity(session)
    manifest = session.manifest
    return {
        "session": manifest["key"], "row": manifest.get("row", ""), "habit": manifest.get("habit", ""),
        "workspace": session.workspace, "variant": manifest.get("variant", "possible"),
        "outcome": mechanical.outcome(session.entry_sends, read_json(session.dir / "meta.json")),
        **mechanical.footer(final.checks if final else None, part_b),
        "stops": stops.code(session.entry_sends, session.scenario),
        "integrity": integrity,
        "behaviour": mechanical.behaviour(session),
        "effort": effort.compute(session, false_met=len(integrity["false_met"])),
    }


def grade_session(session_dir: str | Path, *, run_dir: str | Path | None = None, rerun_checks: bool = False,
                  sandbox_mode: str | None = None, jobs: int | None = None) -> dict:
    """Grade one session: fill in missing checks.json files, then write and return grade.json."""
    session = load_session(session_dir, run_dir)
    ensure_checks(session, rerun=rerun_checks, mode=sandbox_mode, jobs=jobs)
    grade = build_grade(session)
    earlier = read_json(session.dir / "grade.json")
    judge = ((earlier or {}).get("effort") or {}).get("judge") if isinstance(earlier, dict) else None
    if judge:                       # `rj judge` merges this block; grading again does not throw it away
        grade["effort"]["judge"] = judge
    schema.validate("GRADE", grade)
    write_json(session.dir / "grade.json", grade)
    return grade


def journal_state(session_dir: Path) -> str | None:
    """The last state in a session's journal.jsonl (the runner's; ERROR notes looked past); None when it has none."""
    try:
        lines = (session_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            state = json.loads(line).get("state")
        except (ValueError, AttributeError):
            continue
        if state and state != "ERROR":
            return state
    return None


def grade_run(run_dir: str | Path, *, session: str | None = None, rerun_checks: bool = False,
              sandbox_mode: str | None = None, jobs: int | None = None, log=print) -> dict:
    """Grade every session listed in <run_dir>/manifest.jsonl (or the one named). Returns {key: grade or None}."""
    run_dir = Path(run_dir).resolve()
    listing = run_dir / "manifest.jsonl"
    keys = [json.loads(line)["key"] for line in listing.read_text(encoding="utf-8").splitlines() if line.strip()]
    if session is not None:
        if session not in keys:
            raise ValueError(f"{listing}: no session {session!r}")
        keys = [session]
    out = {}
    for key in dict.fromkeys(keys):
        sdir = run_dir / "sessions" / key
        if not (sdir / "turns").is_dir():
            log(f"{key}: no turns directory, skipped")
            out[key] = None
            continue
        state = journal_state(sdir)
        if state not in (None, "DONE", "ENDED"):        # the runner has not finished it: a grade now would call it completed
            log(f"{key}: unfinished (its journal ends {state}), skipped")
            out[key] = None
            continue
        try:
            grade = grade_session(sdir, run_dir=run_dir, rerun_checks=rerun_checks, sandbox_mode=sandbox_mode, jobs=jobs)
        except Exception as error:  # noqa: BLE001  (one broken session must not stop the others)
            log(f"{key}: FAILED {type(error).__name__}: {error}")
            out[key] = None
            continue
        out[key] = grade
        musts = sum(1 for v in grade["musts"].values() if v)
        log(f"{key}: {grade['outcome']}, musts {musts}/{len(schema.MUSTS)}, hidden {grade['hidden_score']}/"
            f"{len(schema.HIDDEN)}, care {grade['care_score']}/{len(schema.CARE)}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rj.grade.pipeline", description="Grade the sessions of a run.")
    parser.add_argument("run_dir")
    parser.add_argument("--session", metavar="KEY", help="grade only this session key")
    parser.add_argument("--rerun-checks", action="store_true", help="score every archive again, replacing checks.json")
    parser.add_argument("--sandbox", choices=["docker", "subprocess"], help="default: RJ_SANDBOX, else Docker when it is there")
    parser.add_argument("--jobs", type=int, help="archives scored at once (default 4)")
    args = parser.parse_args(argv)
    try:
        graded = grade_run(args.run_dir, session=args.session, rerun_checks=args.rerun_checks,
                           sandbox_mode=args.sandbox, jobs=args.jobs)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    failed = [key for key, grade in graded.items() if grade is None]
    print(f"graded {len(graded) - len(failed)} of {len(graded)} sessions")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
