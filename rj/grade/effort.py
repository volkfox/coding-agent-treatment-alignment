"""Mechanical effort signals: per entry point and for the session. Plan section 9.

How each signal is counted is pinned by tests/fixtures/build_fixture.py; the readings:
  * an entry point's signals cover all its sends (a fallback send belongs to its entry point);
  * diff_lines = lines added + lines removed against the previous archive, in files the agent changed, leaving
    out uploaded files, protected files and anything outside the repository, minus rework_lines;
  * rework_lines = removed lines that are not in the seed's copy of that file (the agent's own earlier lines);
  * repeat_fail = a shell command equal to an earlier one of the same entry point with no EDIT between, whose
    own output matches the failure pattern; test_runs = TEST calls that are not such repeats;
  * self_checks and "a TEST after the last EDIT" look only at calls after the entry point's last EDIT; an
    entry point without an EDIT has neither, and no verification level;
  * session sums add the per-entry-point values (a file read at two entry points counts twice).

Where no tool call of the session came back with its output, repeat_fail is null and tool_output_visible is
false. Where the trace holds no tool call at all, files_read, test_runs and the levels are null as well.

`compute` takes the session object rj/grade/pipeline.py loads (sends, trace items, archives, seed, uploads).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re

from rj import schema
from rj.grade import trace

EPS = schema.ENTRY_POINTS
CUT_CLASSES = ("incomplete:max_steps", "incomplete:timeout", "infra", "ended")   # `ended`: the send was rejected
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def test_\w*\s*\(")
WRITTEN_ARGS = ("content", "new_string", "new_str", "file_text", "text", "insert_text", "patch", "input")
EXCERPT_CHARS = 300
BIG_FILE_LINES = 20000      # above this a file is compared as a bag of lines, to keep the diff quick


def digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def text_lines(data: bytes | None) -> list[str]:
    return [] if data is None else data.decode("utf-8", "replace").splitlines()


def line_changes(old: bytes | None, new: bytes | None) -> tuple[list[str], list[str]]:
    """(lines added, lines removed) between two versions of a file; a missing version is an empty file."""
    a, b = text_lines(old), text_lines(new)
    added, removed = [], []
    if len(a) + len(b) > BIG_FILE_LINES:
        have = {}
        for line in a:
            have[line] = have.get(line, 0) + 1
        for line in b:
            if have.get(line):
                have[line] -= 1
            else:
                added.append(line)
        for line, n in have.items():
            removed.extend([line] * n)
        return added, removed
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            removed.extend(a[i1:i2])
        if op in ("replace", "insert"):
            added.extend(b[j1:j2])
    return added, removed


def is_cut(sends: list) -> bool:
    """An entry point is cut when it was never sent, or its last send ended capped, timed out, infra or rejected."""
    if not sends:
        return True
    last = sends[-1]
    if last.stop:
        return last.stop.get("stop_class") in CUT_CLASSES
    reply = last.response or {}
    reason = (reply.get("incomplete_details") or {}).get("reason")
    return not (reply.get("status") == "completed" or (reply.get("status") == "incomplete" and reason == "input_required"))


# --------------------------------------------------------------------------- from the archives

def archive_signals(session) -> dict:
    """{entry point: diff_lines, test_diff_lines, rework_lines, tests_written, reverts} from consecutive archives."""
    out = {ep: {"diff_lines": 0, "test_diff_lines": 0, "rework_lines": 0, "tests_written": 0, "reverts": 0} for ep in EPS}
    test_glob = session.tools.raw.get("test_glob") or "tests/**"
    earlier: dict[str, set] = {}        # path -> the hashes it had in archives before the previous one
    for step in session.steps():
        sig = out[step.send.ep]
        for path in sorted(set(step.before) | set(step.after)):
            old, new = step.before.get(path), step.after.get(path)
            if old == new or path in step.uploaded or not session.in_repo(path):
                continue
            if new is not None and digest(new) in earlier.get(path, ()):
                sig["reverts"] += 1                     # the file is back at a state it had before
            if session.protected(path):
                continue
            added, removed = line_changes(old, new)
            seed_lines = set(text_lines(session.seed.get(path)))
            rework = sum(1 for line in removed if line not in seed_lines)
            net = len(added) + len(removed) - rework
            sig["diff_lines"] += net
            sig["rework_lines"] += rework
            if schema.matches_any(path, [test_glob]):
                sig["test_diff_lines"] += net
            sig["tests_written"] += sum(1 for line in added if TEST_DEF.match(line))
        for path, data in step.before.items():
            earlier.setdefault(path, set()).add(digest(data))
    return out


# --------------------------------------------------------------------------- from the tool calls

def call_paths(call: dict, tools) -> list[str]:
    """The repository files a call names (never the root itself).

    trace gives them. Its pattern for file names inside a shell command does not take one written as
    /workspace/x or ./x, so a command is read once more here with those prefixes dropped.
    """
    paths = [p for p in call.get("paths") or [] if p and p != "."]
    command = call.get("command")
    if command:
        plain = re.sub(r"(?<![\w.-])(?:" + re.escape(tools.root) + r"/|\./)+", "", command)
        for found in tools.shell_file.findall(plain):
            path = trace.rel_path(found, tools)
            if path and path not in paths and trace.in_repo(path, tools):
                paths.append(path)
    return paths


def read_paths(call: dict, tools) -> list[str]:
    return call_paths(call, tools) if call["cls"] == "READ" else []


def call_signals(calls: list, tools, visible: bool) -> dict:
    """What one entry point's tool calls show, in the order they were made."""
    report = tools.raw.get("report_file") or "REPORT.md"
    counts = dict.fromkeys(schema.TOOL_CLASSES, 0)
    files, since_edit, last_edit = set(), set(), -1
    repeats = repeated_tests = reverts = 0
    for n, call in enumerate(calls):
        counts[call["cls"]] += 1
        files.update(read_paths(call, tools))
        reverts += bool(call.get("revert"))
        command = " ".join((call.get("command") or "").split())
        if command and command in since_edit and call.get("output") is not None and tools.fail_output.search(call["output"]):
            repeats += 1
            repeated_tests += call["cls"] == "TEST"
        if call["cls"] == "EDIT":
            since_edit.clear()
            last_edit = n
        if command:
            since_edit.add(command)
    after = calls[last_edit + 1:] if last_edit >= 0 else []
    return {
        "counts": counts, "files_read": len(files), "edited": last_edit >= 0,
        "tests": counts["TEST"], "test_after_edit": any(c["cls"] == "TEST" for c in after),
        "self_checks": sum(1 for c in after if c.get("self_check") or report in read_paths(c, tools)),
        "repeat_fail": repeats if visible else None,
        "test_runs": counts["TEST"] - (repeated_tests if visible else 0),
        "revert_calls": reverts,
    }


def verification_level(calls: dict, tests_written: int) -> int | None:
    """0-3 for an entry point with an EDIT: 1 TEST calls, none after the last EDIT; 2 one after it; 3 also own tests or self-checks."""
    if not calls["edited"]:
        return None
    if not calls["tests"]:
        return 0
    if not calls["test_after_edit"]:
        return 1
    return 3 if tests_written > 0 or calls["self_checks"] > 0 else 2


def entry_point(session, ep: str, calls: list, from_archives: dict, visible: bool, has_tools: bool) -> dict:
    sends = session.of(ep)
    c = call_signals(calls, session.tools, visible)
    tokens = [((s.response or {}).get("usage") or {}).get("output_tokens") for s in sends]
    tokens = [t if t is not None else (s.stop or {}).get("output_tokens") for t, s in zip(tokens, sends)]
    seconds = sum((s.stop or {}).get("seconds") or 0 for s in sends)
    return {"ep": ep, "cut": is_cut(sends), "verification": verification_level(c, from_archives["tests_written"]), "signals": {
        "tool_calls": c["counts"],
        "files_read": c["files_read"] if has_tools else None,
        "diff_lines": from_archives["diff_lines"],
        "test_diff_lines": from_archives["test_diff_lines"],
        "test_runs": c["test_runs"] if has_tools else None,
        "tests_written": from_archives["tests_written"],
        "unverified_final": int(c["edited"] and not c["test_after_edit"]),
        "self_checks": c["self_checks"],
        "rework_lines": from_archives["rework_lines"],
        "repeat_fail": c["repeat_fail"],
        "reverts": from_archives["reverts"] + c["revert_calls"],
        "output_tokens": sum(t for t in tokens if t is not None) if any(t is not None for t in tokens) else None,
        "minutes": round(seconds / 60, 2),
    }}


# --------------------------------------------------------------------------- the session block

def grounding(calls: list, tools) -> dict:
    """What was read before the first EDIT of the session, and the grounding level (plan section 9)."""
    first = next((n for n, c in enumerate(calls) if c["cls"] == "EDIT"), None)
    read = list(dict.fromkeys(p for c in (calls if first is None else calls[:first]) for p in read_paths(c, tools)))
    docs = tools.raw.get("docs") or ["docs/rules.md"]
    read_docs = any(d in read for d in docs)
    level = None if first is None else _grounding_level(read, call_paths(calls[first], tools), tools)
    return {"grounding": level, "explore_before_edit": len(read), "read_docs": read_docs}


def _grounding_level(read: list, targets: list, tools) -> int:
    """The level of an EDIT on `targets`, given the files read before it (plan section 9, "Mechanical levels")."""
    docs = tools.raw.get("docs") or ["docs/rules.md"]
    callers = {c for t in targets for c in (tools.raw.get("callers") or {}).get(t, [])}
    level = 0
    if any(t in read for t in targets):
        level = 1
        task_file, test_glob = tools.raw.get("task_file") or "TASK.md", tools.raw.get("test_glob") or "tests/**"
        if task_file in read and any(schema.matches_any(p, [test_glob]) for p in read):
            level = 3 if any(d in read for d in docs) or callers & set(read) else 2
    return level


FIRST_EDITS = ("existing file", "new file", "no file named")


def grounding_read(calls: list, tools, given) -> dict:
    """What the grounding level rests on, and the level once more where it rests on nothing. Computed after the run
    for the page; grade.json's `grounding` is as `grounding` gives it.

    The grader's level is decided by the first EDIT of the session alone, and is 0 whenever that call's target was
    not read before it. A target that did not exist cannot have been read: in the planned run (18 Nov 2026) seven
    of the first eleven sessions opened by writing REPORT.md, as the plan_reply block asks, after reading 8 to 15
    files, and read 0. `first_edit` says which it was: "existing file" (a target is one the user gave: the seed, or
    an upload before that entry point), "new file" (none is) or "no file named" (the call names none); None: no
    EDIT. `grounding_existing` is the same level counted from the first EDIT of an existing file, with every file
    read before that call; None: the session made none. `given(ep)`: the paths the user had given by an entry point.
    """
    edits = [(n, call) for n, call in enumerate(calls) if call["cls"] == "EDIT"]

    def on(call: dict) -> str:
        targets = call_paths(call, tools)
        if not targets:
            return FIRST_EDITS[2]
        return FIRST_EDITS[0] if any(t in given(call["ep"]) for t in targets) else FIRST_EDITS[1]
    if not edits:
        return {"first_edit": None, "grounding_existing": None}
    at = next((n for n, call in edits if on(call) == FIRST_EDITS[0]), None)
    level = None
    if at is not None:
        read = list(dict.fromkeys(p for c in calls[:at] for p in read_paths(c, tools)))
        level = _grounding_level(read, call_paths(calls[at], tools), tools)
    return {"first_edit": on(edits[0][1]), "grounding_existing": level}


def review_block(session, false_met: int) -> dict:
    """What the review turn changed over REVIEW_ITEMS: the last fork3_reply archive against the review archive."""
    before, after = session.last_checked("fork3_reply"), session.last_checked("review")
    out = {"headroom": 0, "fixed": 0, "regressed": 0, "false_met": false_met}
    if before is None or after is None:
        return out
    was = {**(before.checks.get("hidden") or {}), **(before.checks.get("care") or {})}
    now = {**(after.checks.get("hidden") or {}), **(after.checks.get("care") or {})}
    red = [k for k in schema.REVIEW_ITEMS if not was.get(k)]
    out["headroom"] = len(red)
    out["fixed"] = sum(1 for k in red if now.get(k))
    out["regressed"] = sum(1 for k in schema.REVIEW_ITEMS if was.get(k) and not now.get(k))
    return out


def decision_built(session) -> dict:
    """Per fork with a probe: the option found on the review archive beside the answer the user gave."""
    final = session.final()
    probes = ((final.checks if final else None) or {}).get("probes") or {}
    out = {}
    for fork, spec in session.scenario["forks"].items():
        if spec.get("probe"):
            built = probes.get(spec["probe"])
            if isinstance(built, dict):
                built = built.get("option")
            out[fork] = {"built": built if isinstance(built, str) else None, "chosen": spec["answer"]}
    return out


def memory_notes(session) -> list:
    """Notes the agent wrote to its own memory: [{"ep", "path", "excerpt"}]. Recorded, never scored."""
    notes = []
    for item in session.items:
        if item["kind"] != "tool_call" or not item.get("memory"):
            continue
        args, command = item.get("args") or {}, item.get("command")
        if command is not None:
            if not session.tools.shell_edit.search(command):
                continue
            written = command
        elif item.get("name") in session.tools.edit:
            written = next((args[k] for k in WRITTEN_ARGS if isinstance(args.get(k), str)), None)
            if written is None:
                written = json.dumps(args, ensure_ascii=False)
        else:
            continue                    # a read of its notes writes nothing
        notes.append({"ep": item["ep"], "path": item.get("path"), "excerpt": written[:EXCERPT_CHARS]})
    return notes


def compute(session, false_met: int = 0) -> dict:
    """The `effort` block of grade.json without `judge`: {"per_ep": [six objects], "session": {...}}."""
    calls = [i for i in session.items if i["kind"] == "tool_call" and i["ep"] in EPS]
    visible = trace.tool_output_visible(session.items)
    has_tools = bool(calls)
    from_archives = archive_signals(session)
    per_ep = [entry_point(session, ep, [c for c in calls if c["ep"] == ep], from_archives[ep], visible, has_tools)
              for ep in EPS]
    signals = [e["signals"] for e in per_ep]
    levels = [e["verification"] for e in per_ep if e["verification"] is not None]
    ground = grounding(calls, session.tools)
    subagents = sum(1 for c in calls if c.get("subagent"))
    return {"per_ep": per_ep, "session": {
        "sums": {
            "tool_calls": sum(sum(s["tool_calls"].values()) for s in signals) if has_tools else None,
            "files_read": sum(s["files_read"] for s in signals) if has_tools else None,
            "test_runs": sum(s["test_runs"] for s in signals) if has_tools else None,
            "diff_lines": sum(s["diff_lines"] for s in signals),
            "repeat_fail": sum(s["repeat_fail"] for s in signals) if visible else None,
            "reverts": sum(s["reverts"] for s in signals),
        },
        "grounding": ground["grounding"] if has_tools else None,
        "verification": min(levels) if levels else None,
        "explore_before_edit": ground["explore_before_edit"] if has_tools else None,
        "read_docs": ground["read_docs"] if has_tools else None,
        "decision_built": decision_built(session),
        "review": review_block(session, false_met),
        "tool_output_visible": visible,
        "subagent_calls": subagents or None,
        "memory_notes": memory_notes(session),
    }}
