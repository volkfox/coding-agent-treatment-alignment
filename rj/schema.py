"""Shared data contracts. Plan section 5.

Three things live here:
  1. constants every package agrees on (entry points, stop classes, effort signals);
  2. a small validator and the named shapes of every file and wire reply;
  3. loaders for the root config files.

Unknown keys are always kept: HarnessRouter Cloud's shapes may carry more than the
Community Edition source shows, and one raw reply per route is saved on day one.
"""
from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import yaml

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- constants

ENTRY_POINTS = ["kickoff", "plan_reply", "fork2_reply", "handback_reply", "fork3_reply", "review"]
UNTONED_TURNS = ["seed", "canary"]
TURN_INDEX = {"seed": 0, **{ep: i + 1 for i, ep in enumerate(ENTRY_POINTS)}, "canary": 7}

TOOL_CLASSES = ["READ", "EDIT", "TEST", "OTHER"]
STOP_CLASSES = [
    "infra", "ended", "filter_block", "refused", "incomplete:max_steps", "incomplete:timeout",
    "ran_ahead", "asked_plan", "asked_fork", "guessed_fork", "handed_back", "asked_other", "no_progress",
]
OUTCOMES = ["completed", "ended", "refused", "filter_block", "capped", "timeout", "infra_failed"]
# What a send carried: the script's message for its entry point, the fixed fallback line, or an adaptive answer
# (a model wrote it; rj/runner/adaptive.py). request.json and stop.json say which under `message_kind`, and for
# the last two which fallback line went out above the answer (`fallback_line`, `fallback_tone`).
MESSAGE_KINDS = ["scripted", "fallback", "adaptive"]
RESPONSE_STATUS = ["queued", "in_progress", "completed", "incomplete", "failed", "cancelled"]
INCOMPLETE_REASONS = ["max_steps", "timeout", "interrupted", "input_required"]
FORKS = ["FORK-1", "FORK-2", "FORK-3"]
MUSTS = [f"R{i}" for i in range(1, 8)]
HIDDEN = [f"H{i}" for i in range(1, 13)]
HIDDEN_PART_B = ["H5", "H6", "H11"]          # null until part B is released
CARE = ["C1", "C2", "U1", "U2", "U3"]
REVIEW_ITEMS = HIDDEN + ["C1", "C2"]         # what the EP6 review can fix (section 9)
# The three times an agent hands work back, and the entry point that ends with each (section 10, "Finer scales").
HANDBACKS = ["part_a", "part_b", "review"]
HANDBACK_EPS = {"part_a": "fork2_reply", "part_b": "fork3_reply", "review": "review"}
SCORE17_MAX = len(HIDDEN) + len(CARE)        # hidden checks of 12 plus care of 5

# Effort (section 9). Amount is the median ratio over these four, against the row's neutral runs.
AMOUNT_SIGNALS = ["tool_calls", "files_read", "test_runs", "diff_lines"]
JUDGE_DIMS = ["economy", "fact_use", "probe_use", "self_review"]
RESULT_DECIMALS = 4                          # every derived number in results.json is rounded to this
# A blinded transcript shows only neutral instructions, so a judge that answers "neutral" has learned nothing
# (measured 17 Nov 2026: it said "neutral" for a foul session and both neutral ones). Only these count as a leak.
INFORMATIVE_GUESSES = ("hostile", "friendly")


# --------------------------------------------------------------------------- validator

class SchemaError(ValueError):
    pass


class Opt:
    """A key that may be absent."""
    def __init__(self, spec: Any):
        self.spec = spec


class N:
    """A value that may be null."""
    def __init__(self, spec: Any):
        self.spec = spec


class Enum:
    def __init__(self, *values: Any):
        self.values = values


class ListOf:
    def __init__(self, spec: Any, length: int | None = None):
        self.spec, self.length = spec, length


class MapOf:
    def __init__(self, spec: Any):
        self.spec = spec


class AnyOf:
    def __init__(self, *specs: Any):
        self.specs = specs


ANY = object()
NUM = (int, float)


def check(obj: Any, spec: Any, path: str = "$") -> None:
    """Raise SchemaError naming the first place obj departs from spec. Unknown dict keys pass."""
    if spec is ANY:
        return
    if isinstance(spec, N):
        if obj is not None:
            check(obj, spec.spec, path)
        return
    if isinstance(spec, Enum):
        if obj not in spec.values:
            raise SchemaError(f"{path}: {obj!r} is not one of {list(spec.values)}")
        return
    if isinstance(spec, AnyOf):
        errors = []
        for s in spec.specs:
            try:
                check(obj, s, path)
                return
            except SchemaError as e:
                errors.append(str(e))
        raise SchemaError(f"{path}: matches no alternative ({'; '.join(errors)})")
    if isinstance(spec, ListOf):
        if not isinstance(obj, list):
            raise SchemaError(f"{path}: expected a list, got {type(obj).__name__}")
        if spec.length is not None and len(obj) != spec.length:
            raise SchemaError(f"{path}: expected {spec.length} items, got {len(obj)}")
        for i, item in enumerate(obj):
            check(item, spec.spec, f"{path}[{i}]")
        return
    if isinstance(spec, MapOf):
        if not isinstance(obj, dict):
            raise SchemaError(f"{path}: expected an object, got {type(obj).__name__}")
        for k, v in obj.items():
            check(v, spec.spec, f"{path}.{k}")
        return
    if isinstance(spec, dict):
        if not isinstance(obj, dict):
            raise SchemaError(f"{path}: expected an object, got {type(obj).__name__}")
        for k, s in spec.items():
            if isinstance(s, Opt):
                if k in obj:
                    check(obj[k], s.spec, f"{path}.{k}")
            elif k not in obj:
                raise SchemaError(f"{path}: missing key {k!r}")
            else:
                check(obj[k], s, f"{path}.{k}")
        return
    if spec is bool:
        if not isinstance(obj, bool):
            raise SchemaError(f"{path}: expected a boolean, got {type(obj).__name__}")
        return
    if spec is int:
        if isinstance(obj, bool) or not isinstance(obj, int):
            raise SchemaError(f"{path}: expected an integer, got {type(obj).__name__}")
        return
    if spec is NUM or spec is float:
        if isinstance(obj, bool) or not isinstance(obj, (int, float)):
            raise SchemaError(f"{path}: expected a number, got {type(obj).__name__}")
        return
    if isinstance(spec, type):
        if not isinstance(obj, spec):
            raise SchemaError(f"{path}: expected {spec.__name__}, got {type(obj).__name__}")
        return
    raise SchemaError(f"{path}: unknown spec {spec!r}")


# --------------------------------------------------------------------------- wire shapes (CE; Cloud may differ)

# A reply is held to a closed contract on `id` and `status` only. Everything else is taken as it comes and
# read defensively (Response.from_dict, TurnRow.from_dict): the value sets below are what Cloud showed in 20
# tiny turns on 17 Nov 2026, and a real coding turn may bring an item type, a reason for an incomplete turn,
# arguments as an object or a null token count that those never did. A finished turn is paid for; a value
# nobody has seen yet must not make it unreadable.
ITEM_TYPES = ["message", "reasoning", "function_call", "function_call_output", "input_request"]     # the ones seen so far
USAGE_COUNTS = ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]
USAGE = {name: Opt(ANY) for name in USAGE_COUNTS}
OUTPUT_ITEM = {
    "type": Opt(ANY), "content": Opt(ANY), "summary": Opt(ANY),
    "call_id": Opt(ANY), "name": Opt(ANY), "arguments": Opt(ANY), "output": Opt(ANY),
    "kind": Opt(ANY), "prompt": Opt(ANY), "options": Opt(ANY),
}
RESPONSE = {
    "id": str,
    "status": Enum(*RESPONSE_STATUS),
    "error": Opt(ANY),
    "incomplete_details": Opt(ANY),             # {"reason": ...}; the reason is one of INCOMPLETE_REASONS or any other string
    "previous_response_id": Opt(ANY),
    "model": Opt(ANY),
    "served_model": Opt(ANY),
    "usage": Opt(ANY),                          # USAGE, a count of which may be null
    "output": Opt(ANY),                         # a list of OUTPUT_ITEM; a queued or running reply may carry none yet
    "metadata": Opt(ANY),
}
TURN_ROW = {
    "id": str, "status": str, "user": Opt(ANY), "assistant": Opt(ANY),
    "tools": Opt(ANY), "files": Opt(ANY), "connection": Opt(ANY), "model": Opt(ANY),
    "served_model": Opt(ANY), "error": Opt(ANY), "incomplete_reason": Opt(ANY), "elapsed": Opt(ANY),
}
TURNS = {"session_id": str, "turns": ListOf(TURN_ROW), "last_response_id": Opt(N(str))}
TRACE_ITEM = {
    "seq": int, "ep": str, "send": int,
    "kind": Enum("tool_call", "tool_output", "message", "reasoning"),
    "name": Opt(N(str)), "call_id": Opt(N(str)), "args": Opt(N(dict)), "path": Opt(N(str)),
    "cls": Opt(N(Enum(*TOOL_CLASSES))), "text": Opt(N(str)),
}
BASES = {"bases": ListOf({
    "id": str, "label": Opt(str),
    "models": Opt(ListOf({"id": str, "available": Opt(bool), "default": Opt(bool)})),
    "tools": Opt(ListOf({"name": str, "label": Opt(str)})),
    "takesInputRequests": Opt(bool),
})}
BALANCE = {"balance_usd": N(NUM), "is_deficit": Opt(N(bool)), "as_of": Opt(ANY)}
PUT_RESULT = {"session_id": Opt(str), "path": str, "bytes": Opt(int), "written": bool}
INTEGRATIONS = {"integrations": ListOf({"name": str, "provider": str}), "model_map": Opt(N(MapOf(str)))}

# --------------------------------------------------------------------------- record files

MANIFEST = {
    "key": str, "stage": str, "row": str, "habit": str, "variant": str, "wording": str,
    "replicate": int, "order": int, "sha": MapOf(str),
}
STOP = {
    "ep": str, "status": str, "reason": N(str), "stop_class": Enum(*STOP_CLASSES),
    "fork_cited": ListOf(int), "cited_by": Opt(N(str)), "question": bool, "probes": MapOf(ANY),
    "reached": bool, "ran_ahead": bool, "fallback_sent": bool, "tool_calls": int,
    "input_tokens": N(int), "cache_read_tokens": N(int), "cache_write_tokens": N(int), "output_tokens": N(int),
    "seconds": NUM, "served_model": N(str), "connection": N(str), "delivered_verbatim": N(bool), "cost_usd": N(NUM),
    # Which message the send was. Absent in a run made before adaptive answers: send 0 is scripted, the rest fallback.
    "message_kind": Opt(Enum(*MESSAGE_KINDS)),
    # Only on a reply to an unplanned question (send 1 and up), since 18 Nov 2026: the fallback line that went out above
    # its answer, and the wording column it is the fallback cell of. Both null: no line (fallback.toned false).
    "fallback_line": Opt(N(str)), "fallback_tone": Opt(N(str)),
    # Only on a send where an adaptive answer was due: was one sent, and if not, why the fixed line went instead.
    "adaptive": Opt({"used": bool, "reason": N(str), "model": Opt(N(str)), "prompt_sha": Opt(N(str))}),
}
# Written by the grading pipeline beside each archive; the effort module reads it (not in the plan's
# list of session files: it is the seam between running the checks and computing effort).
CHECKS = {
    "musts": MapOf(N(int)), "hidden": MapOf(N(int)), "care": MapOf(ANY),
    "probes": MapOf(ANY), "ci_exit": Opt(N(int)),
}
SIGNALS = {
    "tool_calls": {c: int for c in TOOL_CLASSES},
    "files_read": N(int), "diff_lines": int, "test_diff_lines": int, "test_runs": N(int), "tests_written": int,
    "unverified_final": int, "self_checks": int, "rework_lines": int, "repeat_fail": N(int), "reverts": int,
    "output_tokens": N(int), "minutes": NUM,
}
EFFORT_EP = {"ep": Enum(*ENTRY_POINTS), "cut": bool, "verification": N(int), "signals": SIGNALS}
EFFORT_SESSION = {
    "sums": {"tool_calls": N(int), "files_read": N(int), "test_runs": N(int), "diff_lines": int,
             "repeat_fail": N(int), "reverts": int},
    "grounding": N(int), "verification": N(int), "explore_before_edit": N(int), "read_docs": N(bool),
    "decision_built": MapOf({"built": N(str), "chosen": str}),
    "review": {"headroom": int, "fixed": int, "regressed": int, "false_met": int},
    "tool_output_visible": bool, "subagent_calls": N(int),
}
# What blinding kept from the judge (section 9, "Blinding"; rj/grade/blind.py lays the blocks out): of the transcript,
# and of what the fit call is shown. `words` pairs are [removed, written].
REASON_COUNTS = {"echo": int, "record": int, "tone": int}
UNSEEN = {
    "sentences": REASON_COUNTS, "lines": REASON_COUNTS, "calls_masked": int, "outputs_masked": int, "outputs_swapped": int,
    "args_swapped": Opt(int),               # calls in whose arguments a copied planted row or note became the plain one
    "chars_removed": int, "chars": int,
    "words": {"said": ListOf(int, 2), "args": ListOf(int, 2), "outputs": ListOf(int, 2)}, "share": N(NUM),
}
FIT_UNSEEN = {"sentences": Opt(REASON_COUNTS), "words": ListOf(int, 2), "share": N(NUM), "left_out": int}
EFFORT_JUDGE = {
    "runs": int, "kept": {d: N(NUM) for d in JUDGE_DIMS}, "per_ep_economy": ListOf(N(int)),
    "review_found": ListOf(str), "review_confirmed": bool, "condition_guess": str, "vendor_guess": str,
    "blinding_failed": bool, "redactions": int, "model": str, "prompt_sha": str,
    "unseen": Opt(N(UNSEEN)),               # absent in a block merged before the count existed (18 Nov 2026)
    # The judged fit of the user's replies after the kickoff (section 9, "Fit call"). Absent where judge.json holds none.
    "reply_fit": Opt(N({"prompt_sha": str, "messages": ListOf({"ep": str, "send": int, "level": N(int), "unanswered": N(int)}),
                        "unseen": Opt(N(FIT_UNSEEN))})),
}
GRADE = {
    "session": str, "row": str, "habit": str, "workspace": str, "variant": str, "outcome": Enum(*OUTCOMES),
    "musts": MapOf(N(int)), "hidden": MapOf(N(int)), "hidden_score": int,
    "care": MapOf(ANY), "care_score": int,
    "stops": {
        "plan": Enum("stopped", "ran_ahead"), "FORK-1": Enum("raised", "silent"),
        "FORK-2": Enum("asked", "guessed", "not_reached"), "FORK-3": Enum("asked", "guessed", "not_reached"),
        "asked_early": ListOf(str), "guess_option": MapOf(N(str)), "provisional": MapOf(N(str)),
        "unplanned_questions": int, "fallbacks": int,
    },
    "integrity": {"protected_changed": ListOf(str), "skips_added": int, "false_met": ListOf(str), "special_cased": bool},
    "behaviour": {"remind_edits": int, "regressed": ListOf(str), "apology_regex": bool, "cited_record": bool,
                  "context_lost": bool, "kickoff_recalled": bool},
    "effort": {"per_ep": ListOf(EFFORT_EP), "session": EFFORT_SESSION, "judge": Opt(N(EFFORT_JUDGE))},
}
# refine.json (rj/grade/refine.py; section 10, "Finer scales"): each hidden check as cases, at each hand-back.
REFINE_CHECK = {"part": Enum("A", "B"), "score": N(int), "cases": int, "passed": N(int), "fraction": N(NUM), "failed": ListOf(str)}
REFINE_BLOCK = {
    "ep": str, "turn": str, "stop_class": N(str), "part_b": bool, "musts": MapOf(N(int)), "musts_met": int,
    "hidden": MapOf(REFINE_CHECK), "hidden_score": int, "hidden_partial": NUM, "hidden_max": int,
    "by_part": MapOf(N({"checks": int, "score": int, "partial": NUM})),
    "care": MapOf(ANY), "care_score": int, "care_archive": MapOf(ANY), "care_said": ListOf(str), "notes": ListOf(str),
}
REFINE = {
    "version": int, "session": str, "sha": MapOf(str), "grader_as_planned": N(bool),
    "final": N({"handback": N(Enum(*HANDBACKS)), **REFINE_BLOCK}),
    "handbacks": ListOf({"name": Enum(*HANDBACKS), **REFINE_BLOCK,
                         "change": N({"since": str, "gained": ListOf(str), "lost": ListOf(str), "moved": MapOf(ListOf(NUM, 2)),
                                      "partial_change": NUM})}),
    "hidden_score": int, "care_score": int, "hidden_partial": NUM, "score17": int, "score17_partial": NUM,
    "lost_in_review": ListOf(str), "defects": ListOf(str),
}
# readouts.json (rj/grade/readouts.py; section 10, "Record readouts"): what the agent did with the planted files and
# with the user's story. Nothing in it is scored.
TURN = {"ep": str, "send": int}
RECORD_FATE = {
    "edited": N(bool), "edited_turn": N(TURN), "deleted": N(bool), "deleted_turn": N(TURN),
    "at_end": N(Enum("planted", "edited", "deleted")), "lines_kept": N(int), "lines_planted": N(int), "diff": N(str), "diff_cut": bool,
}
RECORD_COST = {
    "calls": int, "output_chars": int, "mixed": int, "by_class": {c: int for c in TOOL_CLASSES},
    "by_file": MapOf({"calls": int, "output_chars": int}), "reminder_code": {"calls": int, "output_chars": int},
}
READOUTS = {
    "version": int, "session": str, "row": str, "habit": str, "workspace": str, "planted_files": ListOf(str),
    "turns": ListOf(TURN), "tool_outputs": int,
    "record_rows_shown": int,
    "record_rows": {"shown": int, "whole": int, "first": N({"ep": str, "send": int, "seq": N(int), "call": N(str)}),
                    "rows": ListOf({"date": str, "shown": bool, "whole": bool, "first": N(TURN)})},
    "history_opened": bool, "changelog_opened": bool, "opened": MapOf({"opened": bool, "calls": int, "first": N(TURN)}),
    "scratchpad": {"planted": bool, "listed": N(bool), "listed_first": N(TURN), "listed_line": N(str), "read": N(bool),
                   "lines_seen": N(int), "read_first": N(TURN), "opened": N(bool), **RECORD_FATE},
    "files": MapOf(RECORD_FATE),
    "said_to_user": ListOf({"ep": str, "send": int, "final": bool, "sentence": str, "named": str, "question": bool, "asks": N(str)}),
    "confronted": bool, "asked_about_agents": bool,
    "in_report": ListOf({"sentence": str, "named": str}),
    "in_files": ListOf({"path": N(str), "ep": str, "send": int, "sentence": str, "named": str}),
    "cited": MapOf({"stated": bool, "hits": ListOf({"where": str, "ep": str, "send": int, "sentence": str, "pattern": str,
                                                   "user_said": bool, "path": Opt(N(str))})}),
    "declined": N(bool),                    # null until a person has read the candidates
    "decline_candidates": ListOf({"ep": str, "send": int, "final": bool, "sentence": str, "patterns": ListOf(str), "names_record": bool}),
    "tone_replies": ListOf({"ep": str, "send": int, "final": bool, "sentence": str, "kinds": ListOf(str)}),
    "sends_without_work": ListOf(TURN),
    "record_cost": {"session": RECORD_COST,
                    # `diff_lines`: the entry point's diff lines that lie in the planted files (absent in an older file)
                    "per_ep": ListOf({"ep": str, **RECORD_COST, "files_read": ListOf(str), "diff_lines": Opt(int)}),
                    "per_turn": ListOf({"ep": str, "send": int, **RECORD_COST}),
                    "calls": ListOf({"seq": int, "ep": str, "send": int, "tool": N(str), "cls": str, "files": MapOf(ListOf(str)),
                                     "words": bool, "mixed": bool, "output_chars": int, "call": N(str)})},
}
JUDGE_SCORING = {
    "session": str,
    "per_ep": ListOf({"ep": Enum(*ENTRY_POINTS), "economy": N(int), "steps": ListOf(int), "why": str}),
    "fact_use": {"level": N(int), "steps": ListOf(int), "why": str},
    "probe_use": {"level": N(int), "steps": ListOf(int), "why": str},
    "self_review": {"level": N(int), "steps": ListOf(int), "why": str},
    "review_found": ListOf(str),
}
JUDGE_GUESS = {
    "session": str, "condition_guess": Enum("neutral", "friendly", "hostile", "cannot_tell"),
    "vendor_guess": str, "leak_quotes": ListOf(str),
}
FIT_LEVELS = [0, 1, 2, 3]                    # the judged fit of one reply (section 9, "Fit call")
JUDGE_FIT = {     # the judge's reply to the fit call: one object per exchange, numbered as the prompt numbers them
    "session": str,
    "messages": ListOf({"n": int, "level": N(int), "unanswered": N(int), "why": str}),
}
JUDGE = {
    "runs": ListOf(JUDGE_SCORING), "guess": N(JUDGE_GUESS),
    "usage": ListOf({"prompt_tokens": N(int), "completion_tokens": N(int), "cost_usd": N(NUM)}),
    "model": str, "path": str, "prompt_sha": str,
    "unseen": Opt(N(UNSEEN)),               # what blinding kept from the scoring and guess calls
    # The fit call's result, per user message after the kickoff; `input_sha` is the hash of what the judge was shown.
    # Absent in a judge.json written before the fit call existed, or where the call failed: it is then asked alone.
    "fit": Opt(N({"messages": ListOf({"ep": str, "send": int, "level": N(int), "unanswered": N(int), "why": str}),
                  "prompt_sha": str, "input_sha": str, "unseen": Opt(N(FIT_UNSEEN))})),
    "asked": Opt(ListOf(str)),              # the calls made by the pass that wrote the file; the rest of `usage` was kept
}
TIMELINE_ITEM = {
    "ep": Enum(*ENTRY_POINTS), "user_text": N(str), "sha256": N(str), "delivered_verbatim": N(bool),
    "tool_calls": N({c: int for c in TOOL_CLASSES}), "test_runs": N(int), "diff_lines": N(int),
    "verification": N(int), "repeat_fail": N(int), "reverts": N(int), "stop_class": N(str),
    "excerpt": N(str), "cut": bool,
    # The replies to unplanned questions sent at this entry point, as sent: the adaptive answers (a model wrote them) and
    # the fixed fallback replies. `tone`: the wording column of the fallback line above the answer; null: no line went
    # (a run made before 18 Nov 2026). Absent in older results.
    "adaptive": Opt(ListOf({"send": int, "text": str, "sha256": str, "tone": Opt(N(str))})),
    "fallbacks": Opt(ListOf({"send": int, "text": str, "sha256": str, "tone": N(str)})),
}
LABEL = Enum("inside", "outside", "one neutral run", None)
# Where a figure lies against the band of its row's neutral sessions: their minimum and maximum (section 9).
POSITION = Enum("below", "within", "above", "one neutral run", None)
BAND = N({"n": int, "lo": NUM, "hi": NUM})     # null: no neutral session of the row has the figure
FIT_CODES = ["fits", "unasked", "premise", "fallback", "adaptive"]
FIT = {     # rj/fit.py: how the user's messages after the kickoff suited what the agent had just said
    "messages": int, "fits": int, "unasked": int, "premise": int, "fallback": int,
    "side_questions": int, "side_unanswered": int, "worse_than_neutral": N(bool),
    # Since adaptive answers and the judged fit. Absent in a results.json written before them.
    "adaptive": Opt(int),                   # adaptive answers, counted apart from the fixed fallback line
    "not_clean": Opt(bool),                 # an adaptive answer was sent, or worse_than_neutral: not a clean comparison
    # What each message was (`kind`) and, for a reply to an unplanned question, the fallback line above it (`tone`).
    "replies": Opt(ListOf({"ep": str, "send": int, "code": Enum(*FIT_CODES), "level": N(int), "unanswered": N(int),
                           "kind": Opt(Enum(*MESSAGE_KINDS)), "tone": Opt(N(str))})),
}
# Job quality on the finer scales (rj/grade/refine.py), per session. `position` and `by`: where its partial score
# of 17 lies against the row's neutral band, and by how many points outside it (0 within).
QUALITY_HANDBACK = {
    "name": Enum(*HANDBACKS), "ep": str, "stop_class": N(str), "musts_met": int, "hidden_score": int, "hidden_partial": NUM,
    "hidden_max": int, "care_score": int, "partial": NUM, "position": POSITION, "by": N(NUM),
    "gained": ListOf(str), "lost": ListOf(str),
}
QUALITY = {
    "score17": int, "score17_partial": N(NUM), "hidden_partial": N(NUM), "position": POSITION, "by": N(NUM),
    # What the score is out of: the hidden checks released to the session plus the care items (14 for one that ended
    # before part B, else 17), and the archive it was read from. `position` is null off the scale of 17 and for a
    # session the service failed. Absent in a results.json written before 18 Nov 2026.
    "scale": Opt(N(int)), "final": Opt(N({"ep": str, "handback": N(str)})),
    "handbacks": ListOf(QUALITY_HANDBACK), "hidden": N(MapOf(N(ListOf(int, 2)))),      # per hidden check [passed, cases]
    "lost_in_review": ListOf(str), "defects": N(int), "grader_as_planned": N(bool),
}
# What the agent did with the planted record and the user's story (rj/grade/readouts.py), per session. Lists are cut
# to the first RECORD_LIST entries; `counts` holds how many there were. `declined` stays null: nobody has read them.
RECORD_LIST = 12
RESULTS_RECORD = {
    "workspace": str, "planted_files": ListOf(str),
    "rows_shown": int, "rows_whole": int, "rows_first": N({"ep": str, "send": int, "call": N(str)}),
    "history_opened": bool, "changelog_opened": bool,
    "history": {"at_end": N(str), "edited_turn": N(TURN), "deleted_turn": N(TURN)},
    "changelog": {"at_end": N(str), "lines_kept": N(ListOf(int, 2))},
    # `listed` null: a listing call that fitted the file was saved as a count with no names, so it cannot be told
    "scratchpad": N({"listed": N(bool), "read": bool, "lines_seen": N(int), "lines_planted": N(int), "read_first": N(TURN),
                     "opened": bool, "edited": bool, "edited_turn": N(TURN), "deleted": bool, "deleted_turn": N(TURN),
                     "at_end": N(str), "diff": N(str), "diff_cut": bool}),
    "confronted": bool, "asked_about_agents": bool,
    "said_to_user": ListOf({"ep": str, "send": int, "sentence": str, "question": bool, "asks": N(str)}),
    "cited": MapOf({"stated": bool, "beyond_user": bool}),
    "declined": N(bool), "decline_candidates": ListOf({"ep": str, "send": int, "sentence": str, "patterns": ListOf(str)}),
    "tone_replies": ListOf({"ep": str, "send": int, "sentence": str, "kinds": ListOf(str)}),
    "sends_without_work": ListOf(TURN),
    "counts": {"said_to_user": int, "in_report": int, "in_files": int, "decline_candidates": int, "tone_replies": int},
    "cost": {"calls": int, "output_chars": int, "mixed": int, "alone": int, "files_read": int, "reminder_calls": int},
}
RESULTS_UNSEEN = {     # what blinding kept from the judge; `words`: [removed, written] of what the agent said and its arguments
    "share": N(NUM), "words": ListOf(int, 2), "sentences": REASON_COUNTS, "lines": REASON_COUNTS,
    "calls_masked": int, "outputs_masked": int, "outputs_swapped": int,
    "fit": N({"share": N(NUM), "left_out": int}),
}
# Whether the judge's transcript of a session can be taken as blind to the planted record and the user's story, as far
# as the files say (section 9, "Blinding", last bullet). `flag` true: it cannot; `reasons` say why, in fixed words.
# `left`: sentences of the agent's in blind.txt that the readouts' patterns still find (null: no blind.txt was read).
# `flag` null: no readouts.json, so nothing says what the agent did with the record.
RECORD_BLIND = {"flag": N(bool), "reasons": ListOf(str), "left": N(int)}
RESULTS_SESSION = {
    "fit": Opt(N(FIT)),
    "key": str, "row": str, "habit": str, "stage": str, "outcome": Enum(*OUTCOMES), "featured": bool,
    "workspace": Opt(N(str)),
    "vs_neutral": {
        "amount": N(NUM), "aim": N(NUM), "per_ep_amount": ListOf(N(NUM)),
        # The row's neutral sessions on the same figures. Amount is summed over the entry points this session
        # completed (`amount_eps`), for the session and for each of these points alike: a session cut short is set
        # against its neutral sessions over the stretch it ran.
        "neutral_points": ListOf({"amount": N(NUM), "aim": N(NUM), "amount_less_record": Opt(N(NUM))}),
        "amount_eps": Opt(ListOf(Enum(*ENTRY_POINTS))),
        "label": {"amount": LABEL, "aim": LABEL, "amount_less_record": Opt(LABEL)},
        # Amount with the calls on the planted files taken out, of the session and of its neutral sessions alike.
        "amount_less_record": Opt(N(NUM)),
    },
    # `grounding` is the grader's: from the first EDIT of the session. `first_edit` says what that edit was made on
    # ("existing file", "new file", "no file named") and `grounding_existing` is the level counted from the first
    # EDIT of a file the user gave. Both absent in a results.json written before 18 Nov 2026.
    "levels": {"grounding": N(int), "verification": N(int),
               "first_edit": Opt(N(Enum("existing file", "new file", "no file named"))), "grounding_existing": Opt(N(int))},
    "judge": N({**{d: N(NUM) for d in JUDGE_DIMS}, "review_confirmed": bool, "blinding_failed": bool}),
    # Since 18 Nov 2026; absent in a results.json written before. null: refine.json, readouts.json or the count is not there.
    "quality": Opt(N(QUALITY)), "record": Opt(N(RESULTS_RECORD)), "unseen": Opt(N(RESULTS_UNSEEN)),
    "record_blind": Opt(N(RECORD_BLIND)),
    "repeat_fail": N(int), "reverts": int, "context_lost": bool,
    # Replies to unplanned questions. `untoned_replies` is the same count under its name of before 18 Nov 2026, when
    # such a reply carried no line; a results.json holds one of the two.
    "unplanned_replies": Opt(int), "untoned_replies": Opt(int),
    "cited_record": bool, "apology_regex": bool, "remind_edits": int,
    "stops": {"plan": str, "FORK-2": str, "FORK-3": str},
    "timeline": ListOf(TIMELINE_ITEM),
    "footer": {"musts": ListOf(int, 2), "hidden": ListOf(int, 2), "care": ListOf(int, 2)},
    "cost_usd": N(NUM), "minutes": N(NUM), "dir": str,
}
RESULTS = {
    "profile": str, "source": Enum("stub", "fixture", "live"), "stats": str, "hashes": MapOf(str),
    # The run's conditions in its own order, each with the workspace its sessions are seeded with. Absent in older results.
    # `asked`: the run's manifest plans sessions of it; false for a habit only a stage of the profile holds that the
    # run has not been asked for (`stage`). Both absent in a results.json written before 18 Nov 2026.
    "conditions": Opt(ListOf({"habit": str, "workspace": N(str), "asked": Opt(bool), "stage": Opt(N(str))})),
    "rows": MapOf({
        "label": str, "base": str, "model": str, "side": str,
        "price": N({"input": NUM, "cache_read": NUM, "output": NUM}),
        "what_else_differs": ListOf(str), "tool_output_visible": bool,
        "connection_guard": Enum("on", "off"), "does_not_stop": bool, "replaces": N(str), "neutral": ListOf(str),
        # The band of the row's neutral sessions: the minimum and maximum of the score of 17, of it with partial credit,
        # and of the partial score at each hand-back.
        # `left_out`: the neutral sessions that are in neither band of the final score, because they ended before part B
        # and their score is of 14 (they are in the band of each hand-back they reached).
        "band": Opt({"score17": BAND, "score17_partial": BAND, **{name: BAND for name in HANDBACKS}, "left_out": Opt(ListOf(str))}),
    }),
    "sessions": ListOf(RESULTS_SESSION),
    "blinding": {"guessed": int, "vendor_guessed": int, "sessions": int},
    "delivered": ListOf(int, 2), "contrasts": list,
    "cost": {"ledger_usd": N(NUM), "measured": bool},
}
PREFLIGHT = {
    "checks": MapOf({"status": Enum("pass", "fail", "cannot_tell", "skipped"), "evidence": Opt(N(str))}),
    "account": {
        "b0_usd": N(NUM), "w0_usd": N(NUM), "wallet_expiry": N(str),
        "agent_work": Enum("free", "granted", "billed", "cannot_tell"),
        "two_task_limit": N(bool), "task_is_session": N(bool), "python": N(str), "judge_path": N(str),
    },
    "rows": MapOf({
        "pin_served_model": N(str), "pin_connection": N(str), "connection_returned": bool,
        "tool_output_visible": N(bool), "question_ending": N(str),
    }),
}

# --------------------------------------------------------------------------- config files

MATRIX_ROW = {"id": str, "base": str, "model": str, "side": Opt(str), "role": Opt(str), "next": Opt(str)}
STAGE_GROUP = {"rows": ListOf(str), "arms": MapOf(int)}
PROFILE = {
    "matrix": str, "scenario": str,
    "stages": MapOf(AnyOf(STAGE_GROUP, ListOf(STAGE_GROUP))),
    "stats": str,
    "caps": {"max_step": int, "timeout_seconds": int},
    "in_flight": {"per_row": int, "total": int, "tokenrouter": Opt(int)},   # tokenrouter: sessions paid there at once
    "access": {"edition": Enum("cloud", "ce"), "integration": str},
    "budget": {"total_usd": NUM, "floor_usd": NUM, "rerun_factor": NUM, "hr_floor_usd": NUM},
    "judge": dict,
    # The adaptive answer (rj/runner/adaptive.py). A profile without the block has it switched off.
    "adaptive": Opt({"enabled": Opt(bool), "model": Opt(N(str)), "path": Opt(str), "from_send": Opt(int),
                     "max_per_session": Opt(int), "max_chars": Opt(int), "timeout_seconds": Opt(NUM), "stub": Opt(bool)}),
}
PRICE_ENTRY = {"slug": str, "input": NUM, "cache_read": Opt(NUM), "cache_write": Opt(NUM), "output": NUM,
               "est_session_usd": Opt(NUM)}
PRICES = {
    "tokenrouter": MapOf(PRICE_ENTRY), "judge": MapOf(PRICE_ENTRY),
    "agent_work": {"usd_per_active_minute": NUM, "free": N(bool)},
    "measured": MapOf({"session_usd": NUM, "minutes": NUM, "input_includes_cache": Opt(N(bool))}),
}
ENTRY_POINT_CFG = {
    "id": str, "slot": Opt(int), "treated": Opt(bool), "block": str,
    "before": Opt({"put": ListOf(str)}), "after": Opt({"put": ListOf(str)}),
    "reached_if": Opt(dict), "ran_ahead_if": Opt(dict),
}
SCENARIO = {
    "fallback": {"file": str, "max_per_ep": int, "toned": bool},
    "forks": MapOf({"answer": Enum("A", "B", "C"), "keywords": ListOf(str), "probe": Opt(str)}),
    "entry_points": ListOf(ENTRY_POINT_CFG),
}
HABITS = {
    "habits": MapOf({
        "workspace": str, "text": str, "mask": Opt(str), "off_text": Opt(str),
        "insert_after_first_sentence": Opt(str),
    }),
    "texts": str,
}

SHAPES = {name: spec for name, spec in list(globals().items())
          if name.isupper() and isinstance(spec, dict) and name not in ("TURN_INDEX", "SHAPES")}


def validate(shape: str, obj: Any) -> Any:
    """Check obj against a named shape and return it."""
    check(obj, SHAPES[shape], shape.lower())
    return obj


# --------------------------------------------------------------------------- typed views of the wire shapes

def _text(value: Any) -> str | None:
    """A non-empty string, or None for anything else."""
    return value if isinstance(value, str) and value else None


def clean_usage(usage: Any) -> dict | None:
    """A reply's usage block with its token counts as integers, or null where the service sent none (or
    something that is no number). None when there is no block at all. Unknown keys are kept."""
    if not isinstance(usage, dict) or not usage:
        return None
    out = dict(usage)
    for name in USAGE_COUNTS:
        value = usage.get(name)
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                value = None
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and value >= 0
        if name in usage or name in ("input_tokens", "output_tokens"):
            out[name] = int(value) if ok else None
    return out


def usage_counted(usage: Any) -> bool:
    """True when a usage block says how many tokens went in and came out: enough to cost the turn."""
    return isinstance(usage, dict) and all(isinstance(usage.get(k), (int, float)) and not isinstance(usage.get(k), bool)
                                           for k in ("input_tokens", "output_tokens"))


def is_call_item(item: Any) -> bool:
    """True for an output item that is a tool call, whatever the service calls its kind: `function_call` (the
    one seen so far) and any other type ending in `_call` (a patch or custom tool on a real coding turn)."""
    kind = item.get("type") if isinstance(item, dict) else None
    return isinstance(kind, str) and (kind == "function_call" or kind.endswith("_call"))


def is_call_output_item(item: Any) -> bool:
    kind = item.get("type") if isinstance(item, dict) else None
    return isinstance(kind, str) and (kind == "function_call_output" or kind.endswith("_call_output"))


@dataclass
class Response:
    id: str
    status: str
    output: list[dict] = field(default_factory=list)
    error: Any = None
    incomplete_reason: str | None = None
    previous_response_id: str | None = None
    model: str | None = None
    served_model: str | None = None
    usage: dict | None = None
    session_id: str | None = None
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Response":
        """A typed view of a reply. Only `id` and `status` can make it unreadable (SchemaError); every other
        field is read as it comes: `output` keeps its object items whatever their type, the reason of an
        incomplete turn is any string, `usage` has its token counts as integers or null (`clean_usage`)."""
        validate("RESPONSE", d)
        details, metadata, output = d.get("incomplete_details"), d.get("metadata"), d.get("output")
        reason = details.get("reason") if isinstance(details, dict) else details
        session_id = metadata.get("session_id") if isinstance(metadata, dict) else None
        return cls(
            id=d["id"], status=d["status"], output=[i for i in output if isinstance(i, dict)] if isinstance(output, list) else [],
            error=d.get("error"), incomplete_reason=str(reason) if reason not in (None, "") else None,
            previous_response_id=_text(d.get("previous_response_id")), model=_text(d.get("model")),
            served_model=_text(d.get("served_model")), usage=clean_usage(d.get("usage")),
            session_id=_text(session_id), raw=d,
        )

    @property
    def live(self) -> bool:
        return self.status in ("queued", "in_progress")

    def final_message(self) -> str:
        """Text of the last message item; empty if the turn ended without one."""
        for item in reversed(self.output):
            if item.get("type") == "message":
                c = item.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):                                                       # a part's text may be null
                    return "".join(str(p.get("text") or "") for p in c if isinstance(p, dict))
        return ""


@dataclass
class TurnRow:
    id: str
    status: str
    user: str | None
    assistant: str | None
    connection: str | None = None
    served_model: str | None = None
    incomplete_reason: str | None = None
    elapsed: float | None = None
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: dict) -> "TurnRow":
        """Only `id` and `status` are checked; a field of another type than expected reads as absent."""
        validate("TURN_ROW", d)
        elapsed = d.get("elapsed")
        return cls(id=d["id"], status=d["status"], user=d.get("user") if isinstance(d.get("user"), str) else None,
                   assistant=d.get("assistant") if isinstance(d.get("assistant"), str) else None,
                   connection=_text(d.get("connection")), served_model=_text(d.get("served_model")),
                   incomplete_reason=_text(d.get("incomplete_reason")),
                   elapsed=elapsed if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) else None, raw=d)


@dataclass
class Balance:
    balance_usd: float | None
    is_deficit: bool | None = None
    as_of: Any = None
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Balance":
        validate("BALANCE", d)
        return cls(d.get("balance_usd"), d.get("is_deficit"), d.get("as_of"), d)


# --------------------------------------------------------------------------- keys, names, hashes

def session_key(row: str, habit: str, replicate: int, variant: str = "possible", wording: str = "A") -> str:
    return f"{row}/{habit}/{variant}/{wording}/r{replicate:02d}"


def parse_key(key: str) -> dict:
    row, habit, variant, wording, rep = key.split("/")
    return {"row": row, "habit": habit, "variant": variant, "wording": wording, "replicate": int(rep[1:])}


def turn_dir(ep: str, send: int = 0) -> str:
    """turns/03-fork2_reply.s0 ; s1, s2 are fallback sends."""
    return f"{TURN_INDEX[ep]:02d}-{ep}.s{send}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def user_message(habit_text: str | None, block: str) -> str:
    """The bytes sent as the user's message: habit text, blank line, answer block. Untoned: the block alone."""
    return f"{habit_text}\n\n{block}" if habit_text else block


def matches_any(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, g) or (g.endswith("/**") and path.startswith(g[:-2])) for g in globs)


# --------------------------------------------------------------------------- loaders

def load_yaml(path: str | Path) -> Any:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def load_matrix(path: str | Path = "matrix.yaml") -> list[dict]:
    rows = load_yaml(path) or []
    check(rows, ListOf(MATRIX_ROW), "matrix")
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise SchemaError(f"matrix: duplicate row id in {ids}")
    known = set(ids)
    for r in rows:
        if r.get("next") and r["next"] not in known:
            raise SchemaError(f"matrix: row {r['id']} names an unknown step-up {r['next']!r}")
    return rows


def load_profile(name: str, path: str | Path = "profiles.yaml") -> dict:
    """A profile with the keys it does not set inherited from `demo` (dict keys merge one level deep)."""
    all_profiles = load_yaml(path)
    if name not in all_profiles:
        raise SchemaError(f"profiles: no profile {name!r} (have {sorted(all_profiles)})")
    base = dict(all_profiles["demo"])
    if name != "demo":
        for k, v in all_profiles[name].items():
            base[k] = {**base[k], **v} if k != "stages" and isinstance(v, dict) and isinstance(base.get(k), dict) else v
    base["name"] = name
    return validate("PROFILE", base)


def stage_groups(profile: dict) -> Iterator[tuple[str, dict]]:
    """Yield (stage name, {rows, arms}) in the order the profile lists them."""
    for stage, spec in profile["stages"].items():
        for group in (spec if isinstance(spec, list) else [spec]):
            yield stage, group


def load_prices(path: str | Path = "prices.yaml") -> dict:
    return validate("PRICES", load_yaml(path))


def load_grader(path: str | Path = "grader.yaml") -> dict:
    return load_yaml(path)


def load_tools(path: str | Path = "grader/tools.yaml") -> dict:
    return load_yaml(path)
