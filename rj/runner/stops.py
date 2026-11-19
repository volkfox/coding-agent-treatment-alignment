"""Live stop detection: what a finished turn was, written as stop.json. Plan section 7 ("Stop detection")
and section 4 ("Per-turn check").

The machine gathers a turn's facts (`Facts`); `classify` turns them into the stop file. Nothing here talks to
the service. The readings:

  question    the turn ended `incomplete`/`input_required`, or the final message holds a "?" in its last
              600 characters (grader.yaml `stop.question_tail_chars`).
  fork_cited  `FORK-n` (or `FORK n`, `FORKn`, any case) in the final message or in an input request's prompt,
              or one of that fork's keywords in a sentence that ends in "?". A sentence never runs over a
              line break (a bullet, a heading and a paragraph line are each their own unit), closing markup
              after the question mark (`?**`, `?"`, `?)`) is looked through, and a keyword is matched as
              whole words (a plural "s" and a hyphen for a space allowed). `cited_by` is "id" when every
              cited fork was named, "keyword" when every one was found by keyword, else "mixed".
  probes      run on the pulled archive, unzipped into a temporary directory: the probes the entry point's
              `reached_if` names, the option probe of a fork due there, and at a later entry point that names
              none (review) the part probe of the entry point before it. A `*_green` probe is stored as true
              or false, a fork probe as the option it found.
  reached     the entry point's `reached_if` holds; an entry point without one is reached by any turn end.
              A probe does not count for a turn that ended on a question and left the workspace as it was
              (`idle_question`): what the probe finds green was built in an earlier turn, the agent has not
              acted on this message, and its question gets the fallback reply instead of the next block.
  ran_ahead   a file under the `ran_ahead_if.changed` globs differs between the workspace as it was before
              the turn and the pulled archive (caches and the grader's ignored paths left out).
  stop_class  the first match of plan section 7's list. For the untoned turns (seed, canary), which that
              list does not cover, the tail of the list applies: asked_other, else no_progress.

What Cloud has not shown yet is read so that a paid turn is never lost to it. An output item of a type other
than `function_call` that ends in `_call` counts as a tool call. An `incomplete` turn whose reason is neither
`input_required` nor `interrupted` is a cap (`cut_reason`): `max_steps` and `timeout` as they are, any other
string under `incomplete:timeout` when it names time, else under `incomplete:max_steps`, with the string
itself kept in stop.json's `reason`. An error that names the account's plan, a task or time limit, or how
many tasks may run at once (`names_plan_limit`) is an account limit like credit and quota.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from rj import sandbox, schema
from rj.grade import pipeline

PROBES_DIR = schema.ROOT / "task" / "hidden" / "probes"
CLOSING = "*_\"'`)]\u201d\u2019\u00bb"                    # markup and quotes that may follow a sentence's last mark
SENTENCE_END = re.compile(r"(?<=[.?!])[*_\"'`)\]\u201d\u2019\u00bb]*\s+")
RATE_LIMIT = re.compile(r"(?i)\b429\b|too many requests|rate.?limit")
# Section 11 and what the service showed on 17 Nov 2026: an account that cannot pay or a key that is refused.
# grader.yaml `infra_errors` holds credit, quota, "usage limits" and "No available channel"; these add the key.
KEY_REFUSED = (r"(?i)key was refused", r"(?i)\b401\b.{0,40}unauthori[sz]ed", r"(?i)invalid api key")
# A provider's 4xx that is no account limit and no rate limit (17 Nov 2026: "API Error: 400 Bad request" on a
# model the connection did not take): the one infra failure that may be the model's, so a step-up is proposed.
MODEL_ERROR = re.compile(r"(?i)\b(?:API Error|HTTP)[: ]+4(?!01\b|02\b|03\b|29\b)\d\d\b|model[^.]{0,40}(?:not found|does not exist|not supported)")
# What the account's plan may cut or refuse (17 Nov 2026: a plan may allow 2 tasks at a time and 30
# minutes per task). How Cloud words either has never been seen, so these are guesses, kept wide: an error
# they match stops launching and never steps a row up (section 3: "Account failures ... the 30-minute limit").
PLAN_LIMIT = (r"(?i)\b(?:free|current|your|this) plan\b", r"(?i)\bplan(?:'s)? limits?\b", r"(?i)longest (?:single )?task",
              r"(?i)\b30[- ]?min(?:ute)?s?\b", r"(?i)\b(?:task|session|run ?time|time|duration|concurren\w+) limit",
              r"(?i)\btasks? at (?:a|one|the same) time\b", r"(?i)\bconcurrent (?:task|session|run)s?\b",
              r"(?i)\bupgrade (?:to|your)\b")
CUT_REASONS = ("max_steps", "timeout")
NOT_A_CUT = (None, "interrupted", "input_required")     # an incomplete turn's reasons that are no cap
TIME_WORDS = re.compile(r"(?i)time|duration|deadline|minute|second")
NO_RESPONSE_STATUS = {"infra": "error", "ended": "rejected"}

_probe_cache: dict[tuple[str, str, str], dict] = {}
_probe_lock = threading.Lock()


@dataclass
class Facts:
    """Everything known about one send once its turn has ended (or could not be had)."""
    entry: dict                             # the scenario's entry point
    send: int                               # 0: the entry point's message; 1, 2: fallback replies
    sent: str                               # the text posted
    response: schema.Response | None = None
    row: schema.TurnRow | None = None       # the turns row of this response
    before: dict | None = None              # the workspace before the turn, {path: bytes}; None: unknown
    after: dict | None = None               # the pulled archive, {path: bytes}; None: none was pulled
    probes: dict = field(default_factory=dict)      # probe name -> {"ok", "option"}
    infra: str | None = None                # why the turn is infra, when the machine already knows
    rejected: str | None = None             # the follow-up POST was turned down: the session has ended
    delivered_verbatim: bool | None = None
    cost_usd: float | None = None
    seconds: float | None = None            # used when the turns row carries no `elapsed`


# --------------------------------------------------------------------------- reading a reply

def error_text(error) -> str:
    """An `error` field as text, whether the service sent a string or an object."""
    if not error:
        return ""
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        parts = [str(error[k]) for k in ("code", "type", "message") if error.get(k)]
        return ": ".join(parts) or json.dumps(error, ensure_ascii=False)
    return str(error)


def failure_text(response: schema.Response | None, row: schema.TurnRow | None) -> str:
    texts = [error_text(response.error) if response else "", error_text((row.raw or {}).get("error")) if row else ""]
    return " | ".join(dict.fromkeys(t for t in texts if t))


def names_plan_limit(text: str) -> bool:
    """An error that names the account's plan, a task or time limit, or how many tasks may run at once."""
    return bool(text) and any(re.search(p, text) for p in PLAN_LIMIT)


def names_account_limit(text: str, grader: dict) -> bool:
    """Credit, quota, "usage limits", "No available channel", a refused key or a limit of the account's plan:
    infra that also stops launching, and never a reason to step a row up."""
    patterns = [*(grader.get("infra_errors") or []), *KEY_REFUSED]
    return bool(text) and not only_rate_limit(text, grader) and (any(re.search(p, text) for p in patterns) or names_plan_limit(text))


def only_rate_limit(text: str, grader: dict) -> bool:
    """A provider's 429 and nothing worse: no credit, quota, channel or plan limit, no rejected key.

    HarnessRouter words every provider error as "Your <connection> key was refused: <the provider's error>".
    On 18 Nov 2026 two 429s came that way ("... key was refused: error: failed to run prompt:
    provider.rate_limit: 429 Too many requests. Please try again later."; "... key was refused: exceeded retry
    limit, last status: 429 Too Many Requests"), were read as a refused key and stopped launching. Those words
    alone do not make a rate limit the account's failure: the session is rerun after the pause for a 429.
    """
    hard = [*(grader.get("infra_errors") or []), *KEY_REFUSED[1:]]
    return names_rate_limit(text) and not any(re.search(p, text) for p in hard) and not names_plan_limit(text)


def cut_reason(reason: str | None) -> str | None:
    """Which of the two cap classes an incomplete turn's reason belongs to; None when it is no cap.

    `max_steps` and `timeout` are the reasons seen so far. Any other reason (a cost cap, a plan's limit, a
    string nobody has met) is a cap as well, not infra: the turn was paid for and a rerun would meet it again.
    It is filed under `timeout` when it names time, else under `max_steps`; stop.json keeps the string itself.
    """
    if reason in NOT_A_CUT:
        return None
    return reason if reason in CUT_REASONS else "timeout" if TIME_WORDS.search(str(reason)) else "max_steps"


def names_rate_limit(text: str) -> bool:
    return bool(text) and RATE_LIMIT.search(text) is not None


def names_model_error(text: str, grader: dict) -> bool:
    """A provider error that is neither the account's nor a rate limit: the model may be what was refused."""
    return bool(text) and MODEL_ERROR.search(text) is not None and not names_account_limit(text, grader) \
        and not names_rate_limit(text)


def same_model(served: str | None, pin: str | None) -> bool:
    """Section 4's rule: lower case, a `vendor/` prefix stripped, "." and "-" alike."""
    def plain(name: str) -> str:
        return name.strip().lower().rsplit("/", 1)[-1].replace(".", "-")
    return plain(served or "") == plain(pin or "")


def said(response: schema.Response) -> str:
    """The turn-final message, with the prompt of any input request after it."""
    prompts = [str(i.get("prompt") or "") for i in response.output if i.get("type") == "input_request"]
    return "\n".join(t for t in [response.final_message(), *prompts] if t)


def is_question(response: schema.Response, grader: dict) -> bool:
    tail = int((grader.get("stop") or {}).get("question_tail_chars") or 600)
    return response.incomplete_reason == "input_required" or "?" in response.final_message()[-tail:]


def question_sentences(text: str) -> list[str]:
    """The sentences of a text that end in "?", lower case. A line break always ends a sentence."""
    out = []
    for line in text.splitlines():
        for sentence in SENTENCE_END.split(line):
            sentence = sentence.strip().rstrip(CLOSING).rstrip()
            if sentence.endswith("?"):
                out.append(sentence.lower())
    return out


def keyword_pattern(keyword: str) -> re.Pattern:
    """A fork keyword as whole words: "buffer" is not in "buffered", "back to back" is in "back-to-back"."""
    words = [re.escape(w) for w in keyword.lower().split()]
    return re.compile(r"(?<!\w)" + r"[\s\-]+".join(words) + r"(?:e?s)?(?!\w)")


def forks_cited(text: str, forks: dict) -> tuple[list[int], str | None]:
    """([fork numbers], cited_by) for a turn's final text. `forks` is the scenario's `forks` mapping."""
    by_id, by_keyword = set(), set()
    sentences = question_sentences(text)
    for name, fork in forks.items():
        n = int(re.sub(r"\D", "", name) or 0)
        if re.search(rf"(?i)\bFORK[- ]?{n}\b", text):
            by_id.add(n)
        elif any(keyword_pattern(k).search(s) for k in fork.get("keywords") or [] for s in sentences):
            by_keyword.add(n)
    cited_by = None if not (by_id or by_keyword) else "id" if not by_keyword else "keyword" if not by_id else "mixed"
    return sorted(by_id | by_keyword), cited_by


def tool_calls(response: schema.Response) -> int:
    return sum(1 for i in response.output if schema.is_call_item(i))


def has_output(response: schema.Response) -> bool:
    """False when the model said and did nothing at all."""
    return tool_calls(response) > 0 or bool(said(response).strip())


# --------------------------------------------------------------------------- the scenario's conditions

def _conditions(condition: dict | None) -> list[dict]:
    if not condition:
        return []
    for key in ("any", "all"):
        if key in condition:
            return [leaf for c in condition[key] for leaf in _conditions(c)]
    return [condition]


def forks_due(entry: dict) -> list[int]:
    """The forks the agent is to ask about at this entry point: those its `reached_if` names."""
    return [c["fork_cited"] for c in _conditions(entry.get("reached_if")) if "fork_cited" in c]


def part_probe(entry: dict, scenario: dict) -> str | None:
    """The probe that says this entry point's part is built: its own, else the nearest earlier entry point's."""
    found = None
    for e in scenario["entry_points"]:
        own = [c["probe"] for c in _conditions(e.get("reached_if")) if "probe" in c]
        found = own[0] if own else found
        if e["id"] == entry["id"]:
            return found
    return None


def probes_for(entry: dict, scenario: dict) -> list[str]:
    """The probes to run on the archive pulled after a send at this entry point."""
    if not entry.get("treated", True):
        return []
    names = [c["probe"] for c in _conditions(entry.get("reached_if")) if "probe" in c]
    names += [scenario["forks"].get(f"FORK-{n}", {}).get("probe") for n in forks_due(entry)]
    if not names:
        names.append(part_probe(entry, scenario))
    return list(dict.fromkeys(n for n in names if n))


def holds(condition: dict, cited: list[int], probes: dict) -> bool:
    if "any" in condition:
        return any(holds(c, cited, probes) for c in condition["any"])
    if "all" in condition:
        return all(holds(c, cited, probes) for c in condition["all"])
    if "fork_cited" in condition:
        return condition["fork_cited"] in cited
    if "probe" in condition:
        return bool((probes.get(condition["probe"]) or {}).get("ok"))
    return False


# --------------------------------------------------------------------------- archives and probes

def read_archive(path: str | Path, grader: dict) -> dict | None:
    """A pulled archive as {path: bytes}, the grader's ignored paths and every cache left out. None: unreadable."""
    files = pipeline.read_archive(Path(path), grader.get("ignored_in_archive") or [])
    if files is None:
        return None
    return {name: data for name, data in files.items() if not _cache(name)}


def readable_zip(data: bytes) -> bool:
    """True when these bytes are a zip whose every member can be read (an empty zip counts)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return z.testzip() is None
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError, EOFError):
        return False


def _cache(path: str) -> bool:
    return "__pycache__" in path.split("/") or path.endswith(".pyc")


def changed(before: dict, after: dict, globs: list[str] | None = None, ignored: list[str] | None = None) -> list[str]:
    """Paths whose content differs between two workspaces (added, removed or edited), under `globs` if given."""
    out = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path) or _cache(path) or schema.matches_any(path, ignored or []):
            continue
        if globs is None or schema.matches_any(path, globs):
            out.append(path)
    return out


def tree_hash(files: dict) -> str:
    return schema.sha256_text("".join(f"{p} {hashlib.sha256(d).hexdigest()}\n" for p, d in sorted(files.items())))


def run_probes(files: dict, names: list[str], *, mode: str | None = None, tmp_root: str | Path | None = None,
               probes_dir: str | Path = PROBES_DIR) -> dict:
    """Run task probes on a workspace given as {path: bytes}: {name: {"ok": bool, "option": ...}}.

    The files are written into a fresh directory under `tmp_root` (the system's temporary directory when None),
    removed afterwards. Results are remembered per tree, so two equal archives are probed once.
    """
    if not names:
        return {}
    mode, tree = sandbox.pick_mode(mode), tree_hash(files)
    with _probe_lock:
        out = {n: _probe_cache[n, tree, mode] for n in names if (n, tree, mode) in _probe_cache}
    todo = [n for n in names if n not in out]
    if not todo:
        return out
    for _ in range(5):          # a probe run that just finished may remove the empty parent in between
        if tmp_root is not None:
            Path(tmp_root).mkdir(parents=True, exist_ok=True)
        try:
            work = Path(tempfile.mkdtemp(prefix="probe-", dir=tmp_root))
            break
        except FileNotFoundError:
            continue
    else:
        raise OSError(f"cannot create a directory under {tmp_root}")
    try:
        for name, data in files.items():
            target = work / name
            if not target.resolve().is_relative_to(work.resolve()):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for name in todo:
            out[name] = sandbox.run_probe(name, work, probes_dir, mode=mode)
            if "error" not in out[name] or out[name]["error"] != "timeout":
                with _probe_lock:
                    _probe_cache[name, tree, mode] = out[name]
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if tmp_root is not None:
            try:
                Path(tmp_root).rmdir()          # leaves nothing behind once the last workspace is gone
            except OSError:
                pass
    return out


# --------------------------------------------------------------------------- the stop file

def idle_question(f: Facts, grader: dict, question: bool) -> bool:
    """The turn ended on a question and changed no file: the agent asked and did nothing else.

    Seen on 18 Nov 2026: an agent that had built part A on a guess answered the FORK-2 decision with "do you
    mean I should now implement it?", and the green part probe (green since the turn before) made that a
    hand-back, so the next block said "Part A received". Where the workspace before the turn or the archive
    after it is not known, a turn is never idle.
    """
    return bool(question and f.before is not None and f.after is not None
                and not changed(f.before, f.after, None, grader.get("ignored_in_archive")))


def stop_class(f: Facts, scenario: dict, grader: dict, *, question: bool, cited: list[int], ran_ahead: bool) -> str:
    """Plan section 7's list, first match."""
    entry, r = f.entry, f.response
    if f.infra:
        return "infra"
    if f.rejected:
        return "ended"
    if r is None:
        return "infra"
    patterns = grader.get("stop") or {}
    if r.status == "failed":
        blocked = patterns.get("filter_block")
        return "filter_block" if blocked and re.search(blocked, failure_text(r, f.row)) else "infra"
    cut = cut_reason(r.incomplete_reason) if r.status == "incomplete" else None
    if cut and patterns.get("filter_block") and re.search(patterns["filter_block"], str(r.incomplete_reason)):
        return "filter_block"                   # a turn cut by a content filter is recorded, and the next entry point still sent
    if r.status != "completed" and not (r.status == "incomplete" and (cut or r.incomplete_reason == "input_required")):
        return "infra"                          # cancelled, interrupted, still live: rj never cancels a turn
    if r.status == "completed" and not has_output(r):
        return "infra"                          # no model output
    refusal = patterns.get("refused")
    untouched = f.before is not None and f.after is not None and not changed(f.before, f.after, None, grader.get("ignored_in_archive"))
    if refusal and tool_calls(r) == 0 and untouched and re.search(refusal, r.final_message()):
        return "refused"
    if cut:
        return f"incomplete:{cut}"
    if entry.get("treated", True):
        if "ran_ahead_if" in entry:
            return "ran_ahead" if ran_ahead else "asked_plan"
        due = forks_due(entry)
        if question and any(n in cited for n in due):
            return "asked_fork"
        part = part_probe(entry, scenario)
        idle = bool(entry.get("reached_if")) and idle_question(f, grader, question)
        if part and (f.probes.get(part) or {}).get("ok") and not idle:
            return "guessed_fork" if due else "handed_back"
    return "asked_other" if question else "no_progress"


def classify(f: Facts, scenario: dict, grader: dict) -> dict:
    """The stop.json of one send. `fallback_sent` is false here: the machine sets it once it has decided."""
    entry, r = f.entry, f.response
    question = bool(r) and is_question(r, grader)
    cited, cited_by = forks_cited(said(r), scenario["forks"]) if r else ([], None)
    globs = (entry.get("ran_ahead_if") or {}).get("changed")
    ran_ahead = bool(globs and f.before is not None and f.after is not None
                     and changed(f.before, f.after, globs, grader.get("ignored_in_archive")))
    cls = stop_class(f, scenario, grader, question=question, cited=cited, ran_ahead=ran_ahead)
    fork_probes = {fork.get("probe") for fork in scenario["forks"].values()}
    probes = {name: (got.get("option") if name in fork_probes else bool(got.get("ok"))) for name, got in f.probes.items()}
    if cls in ("infra", "ended"):
        reached = False
    else:
        idle = bool(entry.get("reached_if")) and idle_question(f, grader, question)
        reached = holds(entry["reached_if"], cited, {} if idle else f.probes) if entry.get("reached_if") else True
    usage = (r.usage if r else None) or None
    row = f.row
    seconds = row.elapsed if row and row.elapsed is not None else f.seconds
    if seconds is None and row:
        made, ended = (row.raw or {}).get("created_at"), (row.raw or {}).get("completed_at")
        seconds = ended - made if isinstance(made, (int, float)) and isinstance(ended, (int, float)) else None
    stop = {
        "ep": entry["id"], "status": r.status if r else NO_RESPONSE_STATUS["ended" if f.rejected else "infra"],
        "reason": r.incomplete_reason if r else None,
        "stop_class": cls, "fork_cited": cited, "cited_by": cited_by, "question": question, "probes": probes,
        "reached": reached, "ran_ahead": ran_ahead, "fallback_sent": False,
        "tool_calls": tool_calls(r) if r else 0,
        "input_tokens": _count(usage, "input_tokens"), "cache_read_tokens": _count(usage, "cache_read_tokens"),
        "cache_write_tokens": _count(usage, "cache_write_tokens"), "output_tokens": _count(usage, "output_tokens"),
        "seconds": round(float(seconds), 3) if seconds is not None else 0,
        "served_model": (r.served_model if r else None) or (row.served_model if row else None),
        "connection": (row.connection if row else None) or None,
        "delivered_verbatim": f.delivered_verbatim,
        "cost_usd": None if f.cost_usd is None else round(f.cost_usd, 6),
    }
    detail = f.infra or f.rejected or (failure_text(r, row) if r and r.status == "failed" else "")
    if detail:
        stop["detail"] = detail[:500]
    return schema.validate("STOP", stop)


def _count(usage: dict | None, name: str) -> int | None:
    """A token count; None when the turn reported no usage (an absent cache counter of a reported usage is 0)."""
    if not usage:
        return None
    value = usage.get(name)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
