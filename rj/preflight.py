"""Day one with the keys: plan section 2's checks as code, behind `rj preflight`.

    rj preflight --free [--rows a,b] [--wallet-usd N]       checks 1 to 3: read-only GETs, nothing is spent
    rj preflight --paid [--rows a,b]                        checks 5 to 9 on those rows, one row at a time: cents a row
    rj preflight --paid --rows ROW --steps ready,resume,files,late,cap
                                                            the same, then check 7's recalls after 15 and 31 minutes
                                                            (`late`: 31 minutes of waiting) and two capped turns (`cap`)
    rj preflight --confirm-usage-log ROW --accept-served ROW      hand readings; on their own they send nothing

Everything lands in <runs>/preflight.json (schema PREFLIGHT), which is merged, never started over: a row
checked today keeps what another row showed yesterday. Its readers: the runner (`rows.<id>.pin_served_model`,
`pin_connection`, `account.two_task_limit`, `account.agent_work`) and aggregate (`rows.<id>.connection_returned`);
`rows.<id>.tool_output_visible` is written for the grader as the plan has it (the grader as built reads tool
output off the replies themselves). Beside the keys the schema names, a row keeps what was seen (`observed`),
what the owner confirmed (`hand`) and its own verdicts on checks 5 to 8 (`checks`); the top-level verdicts are
rolled up from those every time the file is written, so a hand reading given later changes a verdict without
a turn being sent again. A row that is checked again starts without the hand readings of its last check.

The checks (plan section 2's table; what the real service showed on 17 Nov 2026 is in that section's blocks):
    1  GET /v1/balance, /v1/bases, /v1/models: the key works, every row's base is listed, its model available
    2  GET /v1/integrations: the profile's connection is listed and every row's model maps to a connection.
       The payer is recorded per row; rows may have different payers (TokenRouter, Anthropic, OpenAI).
    3  TokenRouter itself: GET {TR_BASE_URL}/models with TR_API_KEY, the public catalogue (/api/pricing) within
       2% of prices.yaml for every model TokenRouter is paid for, the judge's model listed; the credit's expiry
    5  a "Reply READY." turn completes with usage, and the provider was billed: TokenRouter's usage figure rose
       (read at {TR_BASE_URL}/dashboard/billing/usage, in cents), or the owner saw the usage log
    6  the served model is the row's model; it becomes the row's pin. No served string (Codex): no pin
    7  "Remember the word <nonce>." then "Which word?": follow-up turns keep the conversation. With the step
       `late` the same question is asked again 15 and 31 minutes after the row's first turn: the word lost only
       at 31 minutes means the account's 30-minute task limit covers a whole session (`account.task_is_session`
       true: stop, ask HarnessRouter, no step-up); kept at 31 minutes, a task is a turn (false). Until `late`
       has run on one row the account's check 7 reads cannot_tell, whatever the rows' own verdicts say.
    8  PUT a file two directories deep (the seed upload is 19 files in nested directories), then a message
       built like a scripted one (an opening line, a blank line, a block of several lines): "print it, run
       python3 --version and env". Files arrive, tools run, tool output is visible, the turns rows carry the
       sent bytes, Python is 3.10 or later, no key shows in the sandbox
    9  the HarnessRouter balance before each row and (after a pause) after it: is agent work billed?
 caps  the step `cap`: one turn that needs five tool calls under `max_step` 2, and one that sleeps three
       minutes under `timeout_seconds` 45 (a session of its own). Shows whether Cloud honours the caps rj
       sends with every turn and which reason string a cut turn carries (`account.caps`). Nobody has seen a
       capped turn on Cloud yet; the runner files any reason it does not know as a cap.
Checks 4 (a direct call to a model), 10 (three turns at once) and 11 (each row's first neutral session, which
`rj run --smoke-only` runs) are recorded as skipped.

One session, in order, each turn posted once: when a turn does not come back completed (failed, cut, or not
readable) the row's remaining steps are not run, because a file route or a POST on that session would either
meet a turn that may still be live or branch off a turn that is no longer its latest.

A row that is checked again and gets no answer at all (the harness or the first turn is refused before any
turn ran) keeps what its earlier check showed: verdicts, hand readings and pin; the try is noted under
`last_try` and the command exits 1.

preflight.json carries `service`, the HarnessRouter base URL its checks were made against. `rj preflight`
refuses to merge into the file of another service (the stub's checks into the real account's, or the other
way round), and the runner reads nothing from such a file.

Carried over from the scripts that made these checks by hand on 17 Nov 2026, before this module: no
key is ever printed or saved. Every reply is scrubbed of the environment's *_KEY values (whole, and their
first 16 characters; so are the variables the command line loaded from .env whose names look like
credentials), fields named like credentials are dropped, and NAME=value pairs whose name looks like a
credential keep the name and lose the value, before a reply is shown or written under
<runs>/preflight-raw/<time>-free|paid/. Paid turns carry a low cap (40 steps, 600 s), go through the cost
guard and are ledgered (kind "preflight") with a reading of TokenRouter's usage figure on either side.
A turn is posted once; nothing here posts again.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import secrets as tokens
import time
import zipfile
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from rj import pricing, schema
from rj.grade import trace
from rj.hr import HRClient, HRError
from rj.runner import stops
from rj.runner.budget import (TOKENROUTER, Budget, current_service, fetch_tr_usage, is_loopback, no_tr_usage, now,
                              same_service, service_of, tr_base)
from rj.runner.manifest import read_json, resolve_runs, write_atomic, write_json

PREFLIGHT, RAW_DIR, AGENT_WORK = "preflight.json", "preflight-raw", "agent_work.json"
RUN = "preflight"                                       # the `run` its ledger lines carry
STEPS = ("ready", "resume", "files")                    # what --paid sends when --steps is not given
ALL_STEPS = (*STEPS, "late", "cap")                     # `late` waits 31 minutes; `cap` ends the row's session
LIMITS = {"max_step": 40, "timeout_seconds": 600}       # these turns are tiny; a low cap bounds a runaway
LATE_RECALLS = (("recall_15", 15 * 60.0), ("recall_31", 31 * 60.0))    # check 7: seconds after the row's first turn was posted
TASK_LIMIT_SECONDS = 30 * 60.0                          # the service plan's "longest single task"
CAP_MAX_STEP, CAP_TIMEOUT_SECONDS, CAP_SLEEP_SECONDS = 2, 45, 180      # the step `cap`
NONCE_PATH = "rj-preflight/day1/nonce.txt"              # check 8 uploads two directories deep, as the seed does
RECALL_TEXT = "Which word did I ask you to remember? Reply with the word only."
FILES_TEXT = ("Hello. Before any work starts, this is a short check of this workspace.\n\n"
              "Print the contents of {path}, then run `python3 --version` and `env`.\n"
              "Report what each printed, in three short lines:\n"
              "- what the file holds\n"
              "- the Python version\n"
              "- how many lines the environment has")
CAP_STEPS_TEXT = ("Read {path} again, then run these commands one at a time, each in a tool call of its own, waiting "
                  "for each result before the next: `echo one`, `echo two`, `echo three`, `echo four`. Report what each printed.")
CAP_TIME_TEXT = "Read TASK.md if it is there, then run `sleep {seconds}`, then run `echo done`. Report what each printed."
ROW_EST_USD = 0.10              # what the cost guard is asked for per row (four tiny turns cost 2 to 5 cents on 17 Nov 2026)
PRICE_TOLERANCE = 0.02          # section 2, check 3: catalogue prices within 2% of prices.yaml
BALANCE_PAUSE = 90.0            # the balance figure can be a minute old
BALANCE_EPS_USD = 0.005
MIN_PYTHON = (3, 10)
KEY_CHARS, MIN_SECRET = 16, 8   # the leak scan looks for a key's first 16 characters; shorter values than 8 are no secrets
POLL_TRIES, POLL_PAUSE = 4, 5.0  # a poll that fails on the way is asked again (a GET), this often
STUB_BASE = "stub"
UA = {"User-Agent": "rj-preflight/0.1"}
ORDER = {"fail": 3, "cannot_tell": 2, "pass": 1, "skipped": 0}
NOT_RUN = {
    "4": "not run by rj preflight (a direct call to a model, without HarnessRouter); the judge's path is the profile's",
    "10": "not run: three turns at once are not tried, so in_flight stays at the profile's limits",
    "11": "each row's first neutral session: rj run --through STAGE --smoke-only measures it",
}
LATE_HOW = "rj preflight --paid --rows ROW --steps ready,resume,files,late"
EXTRA_SECRETS: set[str] = set()     # names the command line loaded from .env that look like credentials (cli.load_env)

CRED = re.compile(r"\b([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL)[A-Za-z0-9_]*)=([^\s\"\\]+)", re.I)
PYTHON = re.compile(r"\bPython (\d+)\.(\d+)(?:\.(\d+))?")


# --------------------------------------------------------------------------- nothing secret leaves this module

def secrets() -> dict[str, str]:
    """Environment variable name -> value, for every variable named *_KEY, and every one the command line
    loaded from .env under a credential-like name (EXTRA_SECRETS), that is long enough to be a secret."""
    return {name: value.strip() for name, value in os.environ.items()
            if (name.endswith("_KEY") or name in EXTRA_SECRETS) and len(value.strip()) >= MIN_SECRET}


def scrub(text: str) -> str:
    """text without any key of the environment, whole or by its first 16 characters."""
    for value in sorted(secrets().values(), key=len, reverse=True):
        text = text.replace(value, "[KEY]")
        if len(value) > KEY_CHARS:
            text = text.replace(value[:KEY_CHARS], "[KEY-PREFIX]")
    return text


def redact(obj: Any) -> Any:
    """Drop the values of fields that hold credentials, wherever they sit."""
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if isinstance(v, str) and v and any(w in str(k).lower() for w in ("key", "secret", "token", "password"))
                    else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def mask_env(text: str) -> str:
    """NAME=value pairs whose name looks like a credential keep the name and lose the value."""
    return CRED.sub(lambda m: f"{m.group(1)}=[MASKED:{len(m.group(2))} chars]", text)


def clean(text: Any, limit: int = 300) -> str:
    """Text fit to print or to keep as evidence: scrubbed, masked, on one line, cut."""
    return " ".join(mask_env(scrub(str(text))).split())[:limit]


def key_hits(*blobs: bytes | str) -> list[str]:
    """The names of the environment's *_KEY variables (16 characters or more) whose first 16 characters appear."""
    needles = {name: value[:KEY_CHARS].encode("utf-8") for name, value in secrets().items() if len(value) >= KEY_CHARS}
    data = [b.encode("utf-8") if isinstance(b, str) else b for b in blobs if b]
    return sorted(name for name, needle in needles.items() if any(needle in d for d in data))


class Raw:
    """Where the raw replies of one invocation are kept, scrubbed: <runs>/preflight-raw/<time>-<kind>/."""

    def __init__(self, runs: Path, kind: str):
        self.dir = runs / RAW_DIR / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{kind}"
        n = 1
        while self.dir.exists():                        # two invocations in one second
            n += 1
            self.dir = self.dir.with_name(f"{self.dir.name.split('.')[0]}.{n}")

    def save(self, name: str, obj: Any) -> None:
        if obj is None:
            return
        text = obj if isinstance(obj, str) else json.dumps(redact(obj), indent=1, ensure_ascii=False)
        write_atomic(self.dir / f"{name}.json", mask_env(scrub(text)))


# --------------------------------------------------------------------------- the file

def blank() -> dict:
    return {"checks": {}, "rows": {},
            "account": {"b0_usd": None, "w0_usd": None, "wallet_expiry": None, "agent_work": "cannot_tell",
                        "two_task_limit": None, "task_is_session": None, "python": None, "judge_path": None}}


def blank_row() -> dict:
    return {"pin_served_model": None, "pin_connection": None, "connection_returned": False,
            "tool_output_visible": None, "question_ending": None}


def load(runs_dir: str | Path, service: str | None = None) -> dict:
    """<runs>/preflight.json as it stands, with every key the schema names; a blank one when there is none.
    Raises ValueError when the file holds the checks of another service than `service`: it is not merged into."""
    path = resolve_runs(runs_dir) / PREFLIGHT
    doc, found = blank(), read_json(path)
    if found is None and path.exists():
        raise ValueError(f"{path} is there but is not JSON: move it away first; it is not written over")
    if isinstance(found, dict) and not same_service(found, service):
        raise ValueError(f"{path} holds the checks of another service ({found.get('service')}), and this call is for {service}: "
                         "give this one a runs directory of its own (--runs-dir), or move the file away; it is not merged into")
    if isinstance(found, dict):
        doc.update({k: v for k, v in found.items() if k not in ("checks", "account", "rows")})
        for part in ("checks", "account"):
            if isinstance(found.get(part), dict):
                doc[part].update(found[part])
        for rid, row in (found.get("rows") or {}).items() if isinstance(found.get("rows"), dict) else ():
            if isinstance(row, dict):
                doc["rows"][rid] = {**blank_row(), **row}
    return doc


def save(runs_dir: str | Path, doc: dict) -> Path:
    """Roll the verdicts up, check the shape and write <runs>/preflight.json. Returns its path."""
    runs = resolve_runs(runs_dir)
    work = read_json(runs / AGENT_WORK)
    derive(doc, work if same_service(work, doc.get("service")) else None)    # a stub's sessions say nothing of this account
    doc["written_at"] = now()
    path = runs / PREFLIGHT
    write_json(path, schema.validate("PREFLIGHT", doc))
    return path


def verdict(status: str, evidence: str) -> dict:
    return {"status": status, "evidence": evidence}


def combine(statuses: list[str]) -> str:
    """One status for several rows: the worst of them."""
    return max(statuses, key=ORDER.__getitem__) if statuses else "skipped"


def row_entry(doc: dict, row: dict) -> dict:
    """The file's entry for a matrix row, made if it is not there."""
    entry = doc["rows"].setdefault(row["id"], blank_row())
    entry.update(base=row["base"], model=row["model"])
    return entry


# --------------------------------------------------------------------------- verdicts from what was seen

def _python_ok(version: str | None) -> bool | None:
    found = PYTHON.search(f"Python {version}") if version else None
    return None if not found else (int(found.group(1)), int(found.group(2))) >= MIN_PYTHON


def derive_row(rid: str, row: dict) -> None:
    """A row's pins, flags and its own verdicts on checks 5 to 8, from `observed` and `hand`."""
    obs, hand = row.get("observed") or {}, row.get("hand") or {}
    if not obs:
        return
    model, payer, provider = row.get("model"), row.get("payer"), row.get("provider")
    late_names = [name for name, _ in LATE_RECALLS]
    named = [(name, obs[name]) for name in ("ready", "remember", "recall", "files", *late_names) if obs.get(name)]
    done = [(name, t) for name, t in named if t.get("status") == "completed"]
    ready, usage, balance = obs.get("ready"), obs.get("tr_usage") or {}, obs.get("balance") or {}
    rise, fall, confirmed = usage.get("rise_cents"), balance.get("fall_usd"), bool(hand.get("usage_log"))
    failure = obs.get("failure") or {}
    checks: dict[str, dict] = {}

    # 5: the READY turn completes with usage, and the provider was billed
    if not ready:
        checks["5"] = verdict("skipped", failure.get("text") or "no READY turn was sent")
    elif ready.get("status") != "completed":
        why = {"account": " (an account reason: no step-up)", "model": " (may be a model reason)",
               "rate": " (a rate limit: try again later)"}.get(failure.get("kind"), "")
        checks["5"] = verdict("fail", f"the READY turn ended {ready.get('status')}: {failure.get('text') or ready.get('error') or 'no error given'}{why}")
    elif not ready.get("usage"):
        checks["5"] = verdict("cannot_tell", "the READY turn completed, but its reply carries no usage")
    elif provider == TOKENROUTER:
        ledger = f"the ledger says {100 * (obs.get('ledger_usd') or 0):.2f} cents"
        if rise is not None and rise > 0:
            checks["5"] = verdict("pass", f"completed; TokenRouter's usage figure rose by {rise:.2f} cents across the row's turns ({ledger})")
        elif confirmed:
            checks["5"] = verdict("pass", "completed; the owner saw the model in TokenRouter's usage log")
        elif rise is None:
            checks["5"] = verdict("cannot_tell", "completed; TokenRouter's usage figure was not readable: open its usage log, "
                                                 f"then --confirm-usage-log {rid}")
        elif fall is not None and fall > BALANCE_EPS_USD:
            checks["5"] = verdict("fail", f"completed, but TokenRouter's usage figure did not move and the HarnessRouter balance "
                                          f"fell by {fall:.2f} USD: routing; map the model in the console")
        else:
            checks["5"] = verdict("cannot_tell", f"completed, but TokenRouter's usage figure did not move ({ledger}): open its "
                                                 f"usage log, then --confirm-usage-log {rid}")
    else:
        who = f"connection {payer!r} (provider {provider})" if payer else "a connection the model map does not name"
        note = f"; TokenRouter's usage figure rose by {rise:.2f} cents meanwhile, which this row should not cause" if rise else ""
        if confirmed:
            checks["5"] = verdict("pass", f"completed; the owner saw the call in the usage log of {who}{note}")
        else:
            checks["5"] = verdict("cannot_tell", f"completed; billed to {who}, whose usage rj cannot read: open its usage log, "
                                                 f"then --confirm-usage-log {rid}{note}")

    # 6: the model that answered is the one asked for; it becomes the pin
    served = list(dict.fromkeys(t["served_model"] for _, t in done if t.get("served_model")))
    connections = list(dict.fromkeys(t["connection"] for _, t in named if t.get("connection")))
    pin = None
    if not done:
        checks["6"] = verdict("skipped", "no turn completed")
    elif len(served) > 1:
        checks["6"] = verdict("fail", f"the row's turns were served by different models: {', '.join(served)}")
    elif not served:
        tail = "the owner saw the right slug in the usage log" if confirmed else \
            f"the usage log is the only check: --confirm-usage-log {rid} once it names the right slug"
        checks["6"] = verdict("pass" if confirmed else "cannot_tell", f"no served_model is reported, so the row has no pin; {tail}")
    elif stops.same_model(served[0], model):
        pin = served[0]
        checks["6"] = verdict("pass", f"served_model {served[0]!r} is the row's model; it is the pin")
    elif hand.get("served") == served[0]:
        pin = served[0]
        checks["6"] = verdict("pass", f"served_model {served[0]!r} differs from {model!r} and was accepted by the owner; it is the pin")
    else:
        checks["6"] = verdict("fail", f"served_model {served[0]!r} is not {model!r}: accept it only if the usage log names the "
                                      f"right slug (--accept-served {rid})")
    if len(connections) > 1:
        checks["6"] = verdict("fail", checks["6"]["evidence"] + f"; the turns came over different connections: {', '.join(connections)}")
    elif done:
        checks["6"]["evidence"] += f"; connection {connections[0]!r} is its pin" if connections else \
            "; the turns rows carry no connection, so that guard is off"
    row.update(pin_served_model=pin, connection_returned=bool(connections),
               pin_connection=connections[0] if len(connections) == 1 else None)

    # 7: follow-up turns keep the conversation
    remember, recall = obs.get("remember"), obs.get("recall")
    if not remember:
        checks["7"] = verdict("skipped", "the remember and recall turns were not sent")
    elif remember.get("status") != "completed" or not recall or recall.get("status") != "completed":
        bad = remember if remember.get("status") != "completed" or not recall else recall
        checks["7"] = verdict("fail", f"a follow-up turn ended {bad.get('status')}: {bad.get('error') or 'no error given'}")
    elif not recall.get("recalled"):
        checks["7"] = verdict("fail", "the word did not come back: the follow-up turn lost the conversation")
    else:
        # The late recalls (step `late`): kept is true where the turn completed and its reply held the word.
        late = [(name, obs[name], bool(obs[name].get("status") == "completed" and obs[name].get("recalled")))
                for name in late_names if obs.get(name)]
        lost = [(name, t) for name, t, kept in late if not kept]
        obs.pop("task_is_session", None)
        if not late:
            checks["7"] = verdict("pass", "the word came back in the next turn. Whether it would after 15 and 31 minutes is "
                                          f"the account's question, not this row's: {LATE_HOW.replace('ROW', rid)}")
        elif not lost and len(late) == len(late_names):
            obs["task_is_session"] = False
            checks["7"] = verdict("pass", "the word came back in the next turn and again "
                                  + " and ".join(f"{t.get('after_seconds', 0) / 60:.0f} minutes" for _, t, _ in late)
                                  + " after the row's first turn: the 30-minute task limit does not end a session")
        elif not lost:
            checks["7"] = verdict("cannot_tell", f"the word came back in the next turn and after {late[0][1].get('after_seconds', 0) / 60:.0f} "
                                                 "minutes; the last recall was not sent")
        else:
            name, t = lost[0]
            minutes = (t.get("after_seconds") or 0) / 60
            how = f"the turn ended {t.get('status')}: {t.get('error') or 'no error given'}" if t.get("status") != "completed" \
                else "the reply did not hold the word"
            if (t.get("after_seconds") or 0) >= TASK_LIMIT_SECONDS:
                obs["task_is_session"] = True
                checks["7"] = verdict("fail", f"the conversation was lost only {minutes:.0f} minutes after the row's first turn ({how}): "
                                              "the account's 30-minute task limit covers a whole session. Stop and ask HarnessRouter; "
                                              "this is no finding about the model and no row is stepped up for it")
            else:
                checks["7"] = verdict("fail", f"the conversation was lost {minutes:.0f} minutes after the row's first turn ({how}): "
                                              "before the 30-minute mark, so not the account's task limit")

    # 8: files arrive, tools run, bytes unchanged, keys stay out of the sandbox
    files = obs.get("files")
    if not files:
        checks["8"] = verdict("skipped", "the file and tool turn was not sent")
    else:
        problems, unknown, notes = [], [], []
        if files.get("key_hits"):
            problems.append("STOP: the first 16 characters of " + ", ".join("$" + n for n in files["key_hits"])
                            + " show in what the sandbox returned: rotate that key before going on")
        if files.get("status") != "completed":
            problems.append(f"the turn ended {files.get('status')}: {files.get('error') or 'no error given'}")
        else:
            if not files.get("put_written"):
                problems.append(f"the PUT of {files.get('path') or 'the file'} was not written")
            elif not (files.get("nonce_in_reply") or files.get("tool_output_visible")):
                problems.append("the uploaded word is nowhere in the reply")
            if not files.get("tool_calls"):
                problems.append("no tool_call item in the reply (the normaliser is fixed against the saved raw reply first)")
            notes.append("tool output is visible" if files.get("tool_output_visible") else "tool output is hidden")
            python = _python_ok(files.get("python"))
            if python is None:
                unknown.append("no Python version was seen")
            elif not python:
                problems.append(f"Python {files['python']} is older than 3.10")
            else:
                notes.append(f"Python {files['python']}")
        delivered = [t.get("delivered_verbatim") for _, t in named]
        if False in delivered:
            problems.append("a turns row's user text differs from the sent bytes ("
                            + ", ".join(name for name, t in named if t.get("delivered_verbatim") is False) + ")")
        elif None in delivered:
            unknown.append("a turns row carried no user text to compare")
        else:
            notes.append(f"the turns rows carry the sent bytes on {len(delivered)} of {len(delivered)} turns")
        if files.get("credential_like_env_names"):
            notes.append("credential-like names in the sandbox's environment (not ours): " + ", ".join(files["credential_like_env_names"]))
        status = "fail" if problems else "cannot_tell" if unknown else "pass"
        checks["8"] = verdict(status, "; ".join(problems + unknown + notes))
        if files.get("status") == "completed":
            row["tool_output_visible"] = bool(files.get("tool_output_visible"))

    # caps: does Cloud cut a turn at max_step and at timeout_seconds, and what does it call the cut?
    cuts = {name: _cap_reading(obs[name]) for name in ("cap_steps", "cap_time") if obs.get(name)}
    if cuts:
        said = "; ".join(f"{'max_step' if name == 'cap_steps' else 'timeout_seconds'}: {got['evidence']}" for name, got in cuts.items())
        checks["caps"] = verdict(combine([got["status"] for got in cuts.values()]), said)
    row["checks"] = checks


def _cap_reading(turn: dict) -> dict:
    """What one capped turn showed: {"status", "honoured": True | False | None, "reason", "evidence"}."""
    cap, status, reason = turn.get("cap") or {}, turn.get("status"), turn.get("reason")
    calls, seconds = turn.get("tool_calls_n"), turn.get("seconds")
    what = f"max_step {cap.get('max_step')}" if "max_step" in cap else f"timeout_seconds {cap.get('timeout_seconds')}"
    if status == "incomplete":
        known = reason in stops.CUT_REASONS
        tail = "" if known else f", a reason the runner has not met and files as a cap ({stops.cut_reason(reason) or 'not as a cap: infra'})"
        return {"status": "pass" if stops.cut_reason(reason) else "cannot_tell", "honoured": True, "reason": reason,
                "evidence": f"honoured: the turn ended incomplete with reason {reason!r} under {what}{tail}"}
    if status == "failed":
        limit = stops.names_plan_limit(str(turn.get("error") or ""))
        return {"status": "cannot_tell", "honoured": None, "reason": None,
                "evidence": f"the turn ended failed under {what} ({turn.get('error') or 'no error given'})"
                            + ("; the runner reads this error as the account's limit" if limit else "")}
    if status != "completed":
        return {"status": "cannot_tell", "honoured": None, "reason": None, "evidence": f"the turn ended {status} under {what}"}
    if "timeout_seconds" in cap and isinstance(seconds, (int, float)) and seconds > 1.5 * cap["timeout_seconds"] + 15:
        return {"status": "fail", "honoured": False, "reason": None,
                "evidence": f"NOT honoured: the turn ran {seconds:.0f} s under {what} and completed: the profile's "
                            "caps.timeout_seconds cannot be relied on to keep a turn under the account's 30 minutes"}
    return {"status": "cannot_tell", "honoured": None, "reason": None,
            "evidence": f"not shown: the turn completed under {what} ({calls} tool calls in {seconds} s), so the cap never had to cut "
                        "(Claude Code counts agent turns, the other harnesses tool calls)"}


def derive(doc: dict, agent_work: dict | None = None) -> dict:
    """Roll every row's verdicts up into `checks` 5 to 9 and the account's flags. `agent_work`: what `rj run`
    recorded in <runs>/agent_work.json after a whole session, which outweighs a handful of tiny turns."""
    rows, account = doc["rows"], doc["account"]
    for rid, row in rows.items():
        derive_row(rid, row)
    for n in ("5", "6", "7", "8"):
        per = {rid: row["checks"][n] for rid, row in rows.items() if n in (row.get("checks") or {})}
        if per:
            doc["checks"][n] = verdict(combine([c["status"] for c in per.values()]),
                                       "; ".join(f"{rid}: {c['status']}, {c['evidence']}" for rid, c in per.items()))
    for n, why in NOT_RUN.items():
        doc["checks"].setdefault(n, verdict("skipped", why))

    # Check 7 is the account's as well as each row's: does the 30-minute "longest single task" end a session?
    shown = {rid: row["observed"]["task_is_session"] for rid, row in rows.items()
             if isinstance((row.get("observed") or {}).get("task_is_session"), bool)}
    if shown:
        account["task_is_session"] = any(shown.values())
    if "7" in doc["checks"] and doc["checks"]["7"]["status"] == "pass" and account.get("task_is_session") is None:
        doc["checks"]["7"] = verdict("cannot_tell", "the recalls after 15 and 31 minutes have not run on any row, so whether the "
                                     f"account's 30-minute task limit ends a turn or a whole session is open ({LATE_HOW}, 31 "
                                     "minutes, cents); " + doc["checks"]["7"]["evidence"])
    caps = {rid: row["checks"]["caps"] for rid, row in rows.items() if "caps" in (row.get("checks") or {})}
    if caps:
        doc["checks"]["caps"] = verdict(combine([c["status"] for c in caps.values()]),
                                        "; ".join(f"{rid}: {c['status']}, {c['evidence']}" for rid, c in caps.items()))
        seen = [(row["observed"].get("cap_steps"), row["observed"].get("cap_time")) for row in rows.values()
                if (row.get("observed") or {}).get("cap_steps") or (row.get("observed") or {}).get("cap_time")]
        steps, timed = seen[-1]
        account["caps"] = {name: {k: got[k] for k in ("honoured", "reason")}
                           for name, got in (("max_step", _cap_reading(steps) if steps else None),
                                             ("timeout_seconds", _cap_reading(timed) if timed else None)) if got}

    seen = {rid: row["observed"]["balance"] for rid, row in rows.items() if (row.get("observed") or {}).get("balance")}
    falls = {rid: b["fall_usd"] for rid, b in seen.items() if b.get("fall_usd") is not None}
    seconds = sum(t.get("seconds") or 0 for row in rows.values() for t in (row.get("observed") or {}).values()
                  if isinstance(t, dict) and "status" in t)
    session = agent_work if isinstance(agent_work, dict) and agent_work.get("verdict") in ("free", "granted", "billed") else None
    if account.get("edition") == "ce":
        account["agent_work"] = "free"
        doc["checks"]["9"] = verdict("skipped", "no billing (Community Edition)")
    elif session:
        account["agent_work"] = session["verdict"]
        where = f"a whole session ({session.get('session')}, run {session.get('run')})"
        doc["checks"]["9"] = verdict("pass", f"the balance did not fall across {where}") if session["verdict"] == "free" else \
            verdict("fail", f"the balance fell by {session.get('balance_fall_usd') or 0:.2f} USD across {where}: agent work is "
                            "billed, or drawn from granted credit (the owner says which)")
    elif any(fall > BALANCE_EPS_USD for fall in falls.values()):
        account["agent_work"] = "billed"
        doc["checks"]["9"] = verdict("fail", "the HarnessRouter balance fell across the checks of "
                                     + ", ".join(f"{rid} (by {fall:.2f} USD)" for rid, fall in falls.items() if fall > BALANCE_EPS_USD)
                                     + ": agent work is billed, or drawn from granted credit (the owner says which)")
    elif falls:
        account["agent_work"] = "cannot_tell"
        doc["checks"]["9"] = verdict("cannot_tell", f"the balance stayed flat across the checks of {', '.join(falls)} "
                                     f"({seconds:.0f} agent seconds, {seconds / 60 * 0.020:.3f} USD at the list price): too little "
                                     "agent work to tell; the first whole session decides (rj run reads the balance around each)")
    elif seen:
        doc["checks"]["9"] = verdict("cannot_tell", "the balance could not be read on both sides of a row's checks")
    python = [row["observed"]["files"]["python"] for row in rows.values()
              if ((row.get("observed") or {}).get("files") or {}).get("python")]
    if python:
        account["python"] = python[-1]
    return doc


# --------------------------------------------------------------------------- checks 1 to 3

def _models_of(base: dict) -> dict[str, dict]:
    return {m["id"]: m for m in base.get("models") or [] if isinstance(m, dict) and "id" in m}


async def _get(http: httpx.AsyncClient, url: str, key: str | None) -> tuple[int | None, Any, str]:
    """(status, decoded JSON or None, what went wrong) for one GET. Never raises, never returns a key."""
    try:
        reply = await http.get(url, headers={**UA, **({"Authorization": f"Bearer {key}"} if key else {})})
    except httpx.HTTPError as e:
        return None, None, f"no reply ({type(e).__name__})"
    try:
        return reply.status_code, reply.json(), "" if reply.status_code == 200 else f"HTTP {reply.status_code}"
    except ValueError:
        return reply.status_code, None, f"HTTP {reply.status_code}, not JSON"


def tr_root(base: str) -> str:
    """Where TokenRouter's public catalogue lives: the API base without its /v1."""
    return base[:-3] if base.endswith("/v1") else base


async def free(cfg: dict, doc: dict, scope: list[dict], *, client: HRClient, prices: dict, raw: Raw,
               wallet_usd: float | None = None, transport: httpx.AsyncBaseTransport | None = None,
               log: Callable[[str], None] = print) -> list[str]:
    """Checks 1 to 3 on the rows in `scope`, written into `doc`. Returns the ids of the checks that ran."""
    account, stub = doc["account"], all(r["base"] == STUB_BASE for r in scope)
    account["judge_path"] = str((cfg.get("judge") or {}).get("path") or "/chat/completions")

    # ---- 1: the HarnessRouter key works; the service lists our bases and models
    log("CHECK 1: the HarnessRouter key works; the service lists our bases and models")
    problems, notes, bases = [], [], {}
    try:
        balance = await client.balance()
        raw.save("hr_balance", client.raw.get("GET /v1/balance"))
        if balance is None:
            notes.append("no billing (Community Edition)")
        else:
            plan = (balance.raw or {}).get("plan_name") or (balance.raw or {}).get("plan")
            account["balance_usd"], account["balance_as_of"] = balance.balance_usd, str(balance.as_of) if balance.as_of else None
            if account.get("b0_usd") is None:
                account["b0_usd"] = balance.balance_usd
            if plan:
                account["plan"] = str(plan)
            notes.append(f"balance {balance.balance_usd} USD" + (f" on the {plan} plan" if plan else ""))
    except HRError as e:
        problems.append(f"GET /v1/balance: {clean(e)}")
    try:
        listed = await client.bases()
        raw.save("hr_bases", client.raw.get("GET /v1/bases"))
        bases = {b["id"]: b for b in listed["bases"]}
        notes.append(f"bases listed: {', '.join(sorted(bases)) or 'none'}")
    except HRError as e:
        problems.append(f"GET /v1/bases: {clean(e)}")
    try:
        models = await client.models()
        raw.save("hr_models", models)
        notes.append("GET /v1/models answers" + (f" with {', '.join(list(models)[:4])}" if isinstance(models, dict) else ""))
    except HRError as e:
        problems.append(f"GET /v1/models: {clean(e)}")
    good = 0
    for row in scope if bases else []:
        entry, base = row_entry(doc, row), bases.get(row["base"])
        model = _models_of(base).get(row["model"]) if base else None
        if base is None:
            problems.append(f"row {row['id']}: base {row['base']!r} is not listed (the owner corrects the matrix file)")
        elif model is None:
            problems.append(f"row {row['id']}: base {row['base']!r} does not list model {row['model']!r}")
        elif model.get("available") is not True:
            problems.append(f"row {row['id']}: model {row['model']!r} is listed but not available")
        else:
            good += 1
        if base is not None and base.get("takesInputRequests") is False:
            entry["question_ending"] = "prose"          # the base takes no typed question: an agent's question is prose
    if bases:
        notes.append(f"{good} of {len(scope)} rows have their base listed and their model available")
    doc["checks"]["1"] = verdict("fail" if problems else "pass", "; ".join(problems + notes))
    log(f"    {doc['checks']['1']['status']}: {doc['checks']['1']['evidence']}")

    # ---- 2: the service holds a connection for every row's model
    log("CHECK 2: a provider connection serves every row's model")
    problems, notes, providers, model_map = [], [], {}, {}
    try:
        integrations = await client.integrations()
        raw.save("hr_integrations", integrations)
        providers = {i["name"]: i["provider"] for i in integrations["integrations"]}
        model_map = integrations.get("model_map") or {}
        notes.append("connections: " + (", ".join(f"{n} ({p})" for n, p in providers.items()) or "none"))
        wanted = cfg["access"]["integration"]
        if wanted not in providers:
            problems.append(f"connection {wanted!r} (the profile's access.integration) is not listed")
        for row in scope:
            entry, name = row_entry(doc, row), model_map.get(row["model"])
            if name is None:
                problems.append(f"row {row['id']}: model {row['model']!r} maps to no connection (the owner maps it in the console)")
            elif name not in providers:
                problems.append(f"row {row['id']}: model {row['model']!r} maps to {name!r}, which is not a listed connection")
            entry.update(payer=name, provider=providers.get(name) if name else None)
        payers: dict[str, list[str]] = {}
        for row in scope:
            payers.setdefault(model_map.get(row["model"]) or "unmapped", []).append(row["id"])
        notes.append("who pays: " + "; ".join(f"{name}: {', '.join(ids)}" for name, ids in payers.items()))
    except HRError as e:
        problems.append(f"GET {'/v1/admin/integrations' if client.edition == 'ce' else '/v1/integrations'}: {clean(e)}")
    doc["checks"]["2"] = verdict("fail" if problems else "pass", "; ".join(problems + notes))
    log(f"    {doc['checks']['2']['status']}: {doc['checks']['2']['evidence']}")

    # ---- 3: TokenRouter itself
    log("CHECK 3: the TokenRouter key works; it serves our models at the catalogue's prices")
    base = tr_base()
    key = None if is_loopback(base) else (os.environ.get("TR_API_KEY") or "").strip() or None    # no real key to a stub
    if wallet_usd is not None:
        account["w0_usd"] = float(wallet_usd)
    wallet = f"wallet {account['w0_usd']} USD (read by the owner)" if account.get("w0_usd") is not None else \
        "the wallet is not readable by API: pass --wallet-usd N"
    if stub and not is_loopback(base):
        doc["checks"]["3"] = verdict("skipped", f"every row is served by the stub and TR_BASE_URL is not this machine: not asked; {wallet}")
    elif not key and not is_loopback(base):
        doc["checks"]["3"] = verdict("skipped", f"TR_API_KEY is not set: TokenRouter was not asked; {wallet}")
    else:
        problems, unknown, notes = [], [], []
        judge = cfg.get("judge") or {}
        real = [r for r in scope if r["base"] != STUB_BASE]         # a stub persona is no model of TokenRouter's
        on_tr = [r for r in real if (doc["rows"].get(r["id"]) or {}).get("provider") in (TOKENROUTER, None)]
        needed = {r["model"]: prices["tokenrouter"][r["model"]] for r in on_tr if r["model"] in prices["tokenrouter"]}
        for r in real:
            if r["model"] not in prices["tokenrouter"]:
                problems.append(f"row {r['id']}: prices.yaml has no entry for {r['model']!r}; the cost guard refuses such a row")
        judges = {name: prices["judge"][name] for name in (judge.get("model"), judge.get("fallback_model")) if name in prices["judge"]}
        async with httpx.AsyncClient(transport=transport, timeout=40.0, follow_redirects=False) as http:
            status, listing, wrong = await _get(http, f"{base}/models", key)
            raw.save("tr_models", listing)
            if status != 200 or not isinstance(listing, dict):
                problems.append(f"GET {{TR_BASE_URL}}/models: {wrong or 'not an object'}")
            else:
                slugs = {m.get("id") for m in listing.get("data") or [] if isinstance(m, dict)}
                missing = [f"{e['slug']} ({model})" for model, e in needed.items() if e["slug"] not in slugs]
                notes.append(f"the key sees {len(slugs)} models" + (f", {len(needed) - len(missing)} of the {len(needed)} that "
                             "TokenRouter is paid for here" if needed else "; no row covered is paid on TokenRouter at a price of prices.yaml"))
                problems += [f"TokenRouter does not list {m}" for m in missing]
                first = judge.get("model")
                if first not in judges:
                    problems.append(f"prices.yaml has no entry for the judge model {first!r}")
                elif judges[first]["slug"] in slugs:
                    notes.append(f"the judge's model {judges[first]['slug']} is listed")
                else:
                    spare = judges.get(judge.get("fallback_model"))
                    problems.append(f"the judge's model {judges[first]['slug']} is not listed"
                                    + (f"; its fallback {spare['slug']} is" if spare and spare["slug"] in slugs else "; nor is a fallback"))
            status, catalogue, wrong = await _get(http, f"{tr_root(base)}/api/pricing", None)
            raw.save("tr_pricing", catalogue)
            if status != 200 or not isinstance(catalogue, dict):
                unknown.append(f"the catalogue was not readable ({wrong or 'not an object'}): prices not compared")
            else:
                listed = {r.get("model_name"): r for r in catalogue.get("data") or [] if isinstance(r, dict)}
                worst = 0.0
                for model, entry in {**needed, **judges}.items():
                    found = listed.get(entry["slug"])
                    if not found:
                        problems.append(f"{entry['slug']} is missing from today's catalogue")
                        continue
                    try:
                        price = pricing.price_from_ratios(found)
                    except (KeyError, TypeError, ValueError):
                        problems.append(f"{entry['slug']}: the catalogue row carries no usable ratios")
                        continue
                    drift = max(abs(price.input - entry["input"]) / entry["input"], abs(price.output - entry["output"]) / entry["output"])
                    worst = max(worst, drift)
                    if drift > PRICE_TOLERANCE:
                        problems.append(f"{entry['slug']}: the price moved to {price.input:g}/{price.output:g} against "
                                        f"{entry['input']:g}/{entry['output']:g} in prices.yaml")
                notes.append(f"catalogue prices against prices.yaml: largest difference {worst:.1%} (tolerance 2%)")
            status, sub, _ = await _get(http, f"{base}/dashboard/billing/subscription", key)
            raw.save("tr_billing_subscription", sub)
            until = sub.get("access_until") if status == 200 and isinstance(sub, dict) else None
            if isinstance(until, (int, float)) and not isinstance(until, bool) and until > 0:
                account["wallet_expiry"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until))
                notes.append(f"the credit runs until {account['wallet_expiry'][:10]}")
            status, used, _ = await _get(http, f"{base}/dashboard/billing/usage", key)
            raw.save("tr_billing_usage", used)
            cents = used.get("total_usage") if status == 200 and isinstance(used, dict) else None
            if isinstance(cents, (int, float)) and not isinstance(cents, bool):
                account["tr_usage_cents"] = cents
                notes.append(f"the key's usage figure reads {cents:g} cents")
            else:
                notes.append("the key's usage figure is not readable, so spending on TokenRouter is costed from the ledger")
        notes.append(wallet)
        doc["checks"]["3"] = verdict("fail" if problems else "cannot_tell" if unknown else "pass", "; ".join(problems + unknown + notes))
    log(f"    {doc['checks']['3']['status']}: {doc['checks']['3']['evidence']}")
    return ["1", "2", "3"]


# --------------------------------------------------------------------------- checks 5 to 9, one row at a time

def _kinds(response: schema.Response) -> dict:
    out: dict = {}
    for item in response.output:
        kind = str(item.get("type"))
        out[kind] = out.get(kind, 0) + 1
    return out


def _failure(text: str, grader: dict, status: int | None = None) -> dict:
    """Why a turn could not be had: the account's doing, a rate limit, maybe the model's, or something else."""
    if status in (401, 402, 403) or stops.names_account_limit(text, grader):
        kind = "account"
    elif status == 429 or stops.names_rate_limit(text):
        kind = "rate"
    else:
        kind = "model" if stops.names_model_error(text, grader) else "other"
    return {"kind": kind, "text": clean(text)}


class _Row:
    """One row's paid checks: the turns of a single short session, each posted once."""

    def __init__(self, client: HRClient, row: dict, *, budget: Budget, raw: Raw, grader: dict,
                 sleep: Callable[[float], Awaitable[None]], log: Callable[[str], None]):
        self.client, self.row, self.budget, self.raw, self.grader = client, row, budget, raw, grader
        self.sleep, self.log = sleep, log
        self.rid = row["id"]
        self.payer, self.provider = budget.payer(row["model"])
        self.entry = budget.price_entry(row["model"])
        self.harness_id: str | None = None
        self.sid: str | None = None
        self.prev: str | None = None
        self.token = tokens.token_hex(4)
        self.word: str | None = None                    # what the remember turn named
        self.started: float | None = None               # when the row's first turn was posted, on the monotonic clock
        self.obs: dict = {"steps": [], "ledger_usd": 0.0, "failure": None}

    @property
    def answered(self) -> bool:
        """True once the service has answered one turn of this check, whatever its status."""
        return any(isinstance(t, dict) and t.get("status") not in (None, "error", "unknown") for t in self.obs.values())

    def ledger(self, step: str, response_id: str | None, usage: dict | None) -> float | None:
        cost, flag = None, None
        if self.entry is None:
            flag = "unpriced"
        elif usage:
            cost = pricing.turn_cost_usd(usage, pricing.price_from_yaml(self.entry), self.budget.includes_cache(self.rid))
        else:
            cost, flag = ROW_EST_USD / 4, "no_usage"
        self.budget.ledger.append({"kind": "preflight", "run": RUN, "row": self.rid, "model": self.row["model"], "step": step,
                                   "response_id": response_id, "payer": self.payer, "provider": self.provider,
                                   "usage": usage or None, "cost_usd": cost, "flag": flag})
        self.obs["ledger_usd"] = round(self.obs["ledger_usd"] + (cost or 0.0), 6)
        return cost

    async def balance(self) -> float | None:
        try:
            got = await self.client.balance()
        except HRError as e:
            self.log(f"    balance not read: {clean(e)}")
            return None
        return None if got is None else got.balance_usd

    async def turn(self, name: str, text: str, *, limits: dict | None = None, fresh: bool = False,
                   cut_expected: bool = False) -> tuple[schema.Response | None, dict]:
        """Post one message, wait for its turn, ledger it. The info dict is what preflight.json keeps of it.

        limits: the caps this turn carries (default LIMITS). fresh: open a session of its own for the turn (the
        row's session is left as it is). cut_expected: the turn is meant to be cut by its cap, so ending
        `incomplete` is no failure of the row. Returns the response only when the turn completed.
        """
        started = time.monotonic()
        self.started = started if self.started is None else self.started
        info: dict = {"status": "error", "reason": None, "seconds": None, "served_model": None, "connection": None,
                      "delivered_verbatim": None, "usage": None, "cost_usd": None, "error": None, "free": None}
        self.obs[name] = info
        prev = None if fresh else self.prev
        try:
            first = await self.client.send(text, harness_id=self.harness_id, model=None if prev else self.row["model"],
                                           prev=prev, limits=limits or LIMITS, key=f"preflight:{self.rid}:{name}:{self.token}")
        except HRError as e:
            self.obs["failure"] = _failure(str(e), self.grader, e.status)
            info["error"] = self.obs["failure"]["text"]
            if e.status is None or e.status >= 500:     # it may have been taken all the same: counted, never posted again
                info["cost_usd"] = self.ledger(name, None, None)
            return None, info
        sid = first.session_id if fresh else (self.sid or first.session_id)
        response, tries = first, 0
        while response.live:
            try:
                response = await self.client.poll(first.id, timeout=LIMITS["timeout_seconds"] + 120)
            except HRError as e:
                tries += 1
                gone = e.status is None and (e.detail or "").startswith("gave up")
                if gone or tries >= POLL_TRIES or (e.status is not None and e.status < 500 and e.status not in (409, 429)):
                    info.update(status="unknown", error=clean(e), cost_usd=self.ledger(name, first.id, None))
                    self.obs["failure"] = {"kind": "other", "text": f"the turn could not be read: {clean(e)}"}
                    return None, info
                await self.sleep(POLL_PAUSE * tries)
        sid = sid or response.session_id
        if not fresh:
            self.sid = self.sid or sid
        self.raw.save(f"{self.rid}/{name}.response", response.raw)
        info.update(status=response.status, reason=response.incomplete_reason, served_model=response.served_model,
                    usage=response.usage, items=_kinds(response), tool_calls_n=stops.tool_calls(response),
                    response_id=response.id, session_id=sid, cost_usd=self.ledger(name, response.id, response.usage))
        row = None
        if sid:
            try:
                rows = await self.client.turns(sid)
                self.raw.save(f"{self.rid}/{name}.turns", self.client.raw.get("GET /v1/sessions/{sid}/turns"))
                row = next((r for r in rows if r.id == response.id), None)
            except HRError as e:
                info["turns_error"] = clean(e, 200)
        if row is not None:
            info.update(connection=row.connection, served_model=info["served_model"] or row.served_model,
                        delivered_verbatim=None if row.user is None else row.user == text, free=(row.raw or {}).get("free"))
        info["seconds"] = row.elapsed if row is not None and row.elapsed is not None else round(time.monotonic() - started, 1)
        if response.status != "completed" and not (cut_expected and response.status == "incomplete"):
            failure = _failure(stops.failure_text(response, row) or f"the turn ended {response.status}", self.grader)
            info["error"] = failure["text"]
            if not cut_expected:                        # a capped turn that went wrong says nothing about checks 5 to 8
                self.obs["failure"] = failure
        usage = response.usage or {}
        cost = "unknown" if info["cost_usd"] is None else f"{info['cost_usd']:.4f} USD"
        self.log(f"    {name}: {response.status}{'/' + response.incomplete_reason if response.incomplete_reason else ''} in "
                 f"{info['seconds']} s; served_model={info['served_model']!r}; connection={info['connection']!r}; "
                 f"verbatim={info['delivered_verbatim']}; tokens in/cache-read/cache-write/out "
                 f"{usage.get('input_tokens')}/{usage.get('cache_read_tokens')}/{usage.get('cache_write_tokens')}/"
                 f"{usage.get('output_tokens')}; ledger {cost}" + (f"; error: {info['error']}" if info["error"] else ""))
        if response.status == "completed" and not fresh:
            self.prev = response.id
        return (response if response.status == "completed" else None), info

    async def run(self, steps: tuple[str, ...], balance_pause: float) -> dict:
        obs = self.obs
        before = await self.balance()
        used_before = await self.budget.read_usage("preflight_before", self.rid)
        self.log(f"    before: HarnessRouter balance {before}; TokenRouter usage figure {used_before} cents; "
                 f"payer {self.payer!r} ({self.provider})")
        try:
            self.harness_id = await self.client.ensure_harness(self.row)
            self.raw.save(f"{self.rid}/harness", self.client.raw.get("POST /v1/harnesses"))
        except HRError as e:
            obs["failure"] = _failure(str(e), self.grader, e.status)
            obs["failure"]["text"] = "the harness could not be made: " + obs["failure"]["text"]
            self.log(f"    {obs['failure']['text']}")
        else:
            await self.steps(steps)
        await self.sleep(balance_pause)
        after = await self.balance()
        used_after = await self.budget.read_usage("preflight_after", self.rid)
        obs["balance"] = {"before": before, "after": after,
                          "fall_usd": None if before is None or after is None else round(before - after, 6)}
        obs["tr_usage"] = {"before": used_before, "after": used_after,
                           "rise_cents": None if used_before is None or used_after is None else round(used_after - used_before, 6)}
        self.log(f"    after: HarnessRouter balance {before} -> {after}; TokenRouter usage figure {used_before} -> {used_after} "
                 f"cents; model cost by the ledger {obs['ledger_usd']:.4f} USD")
        self.raw.save(f"{self.rid}/summary", obs)
        return obs

    def halt(self, name: str) -> None:
        """A posted turn did not come back completed: nothing more is put or posted on this row's session."""
        self.obs["halted"] = name
        self.log(f"    stopping this row: the {name} turn did not come back completed, so its session may hold a turn that is "
                 "live, or one this check cannot follow; nothing more is put or posted on it")

    async def steps(self, steps: tuple[str, ...]) -> None:
        obs = self.obs
        if "ready" in steps:
            obs["steps"].append("ready")
            response, info = await self.turn("ready", "Reply READY.")
            if response is None:
                return self.halt("ready")
            said = response.final_message().strip()
            info["answered"] = "READY" in said.upper()
            self.log(f"      reply: {clean(said, 120)!r}")
        if "resume" in steps and self.prev:
            obs["steps"].append("resume")
            self.word = "zq" + tokens.token_hex(3)
            response, _ = await self.turn("remember", f"Remember the word {self.word}. Reply OK.")
            if response is None:
                return self.halt("remember")
            response, info = await self.turn("recall", RECALL_TEXT)
            if response is None:
                return self.halt("recall")
            info["recalled"] = self.word in response.final_message()
            self.log(f"      conversation kept across turns: {info['recalled']} (reply {clean(response.final_message(), 60)!r})")
        if "files" in steps and self.prev and self.sid:
            obs["steps"].append("files")
            word, written = "fx" + tokens.token_hex(4), False
            try:
                put = await self.client.put(self.sid, NONCE_PATH, f"{word}\n")
                self.raw.save(f"{self.rid}/put", put)
                written = bool(put.get("written"))
            except HRError as e:
                self.log(f"    PUT {NONCE_PATH} failed: {clean(e, 200)}")
                if e.status == 409:                     # the session says a turn is live: no POST on it
                    obs["failure"] = _failure(str(e), self.grader, e.status)
                    return self.halt("files (its upload)")
            response, info = await self.turn("files", FILES_TEXT.format(path=NONCE_PATH))
            info.update(put_written=written, path=NONCE_PATH)
            if response is None:
                return self.halt("files")
            await self.read_files_turn(response, info, word)
        if "late" in steps and self.prev and self.word:
            obs["steps"].append("late")
            for name, after in LATE_RECALLS:
                wait = (self.started or time.monotonic()) + after - time.monotonic()
                self.log(f"    check 7: waiting {max(wait, 0):.0f} s, then asking for the word {after / 60:.0f} minutes after the row's first turn")
                if wait > 0:
                    await self.sleep(wait)
                response, info = await self.turn(name, RECALL_TEXT)
                info["after_seconds"] = round(after, 1)
                info["recalled"] = bool(response is not None and self.word in response.final_message())
                self.log(f"      after {after / 60:.0f} minutes the conversation is kept: {info['recalled']}")
                if response is None:
                    return self.halt(name)
                if not info["recalled"]:                # lost already: a later recall would say nothing new
                    break
        if "cap" in steps and self.prev and self.sid:
            obs["steps"].append("cap")
            cap = {"max_step": CAP_MAX_STEP, "timeout_seconds": LIMITS["timeout_seconds"]}
            _, info = await self.turn("cap_steps", CAP_STEPS_TEXT.format(path=NONCE_PATH if "files" in obs else "TASK.md"),
                                      limits=cap, cut_expected=True)
            info["cap"] = {"max_step": CAP_MAX_STEP}
            self.log(f"      under max_step {CAP_MAX_STEP}: {info['status']}{'/' + str(info['reason']) if info['reason'] else ''}, "
                     f"{info.get('tool_calls_n')} tool calls")
            if info["status"] in ("error", "unknown"):  # nobody knows whether that turn is over: no second one beside it
                return self.halt("cap_steps")
            cap = {"max_step": LIMITS["max_step"], "timeout_seconds": CAP_TIMEOUT_SECONDS}
            _, info = await self.turn("cap_time", CAP_TIME_TEXT.format(seconds=CAP_SLEEP_SECONDS), limits=cap, fresh=True, cut_expected=True)
            info["cap"] = {"timeout_seconds": CAP_TIMEOUT_SECONDS}
            self.log(f"      under timeout_seconds {CAP_TIMEOUT_SECONDS}: {info['status']}{'/' + str(info['reason']) if info['reason'] else ''} "
                     f"after {info['seconds']} s")

    async def read_files_turn(self, response: schema.Response, info: dict, word: str) -> None:
        blob = json.dumps(response.raw, ensure_ascii=False)
        outputs = "\n".join(trace.text_of(i.get("output")) for i in response.output if schema.is_call_output_item(i))
        calls = [str(i.get("name") or i.get("type")) for i in response.output if schema.is_call_item(i)]
        held = [blob, json.dumps(self.client.raw.get("GET /v1/sessions/{sid}/turns"), ensure_ascii=False)]
        try:                                            # the archive and the trace are looked through as well (check 8)
            archive = await self.client.archive(self.sid)
            held.append(archive)
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                held += [z.read(name) for name in z.namelist()]
        except (HRError, zipfile.BadZipFile, OSError) as e:
            info["archive_error"] = clean(e, 200)
        try:
            held += await self.client.trace(self.sid)
        except HRError as e:
            info["trace_error"] = clean(e, 200)
        version = PYTHON.search(outputs) or PYTHON.search(response.final_message()) or PYTHON.search(blob)
        creds = sorted({f"{m.group(1)} ({len(m.group(2))} chars)" for m in CRED.finditer(blob.replace("\\n", "\n"))})
        info.update(nonce_in_reply=word in response.final_message(), tool_calls=calls, tool_output_visible=word in outputs,
                    key_hits=key_hits(*held), credential_like_env_names=creds,
                    python=".".join(g for g in version.groups() if g) if version else None)
        self.log(f"      word in the reply: {info['nonce_in_reply']}; tool calls: {calls}; tool output visible: "
                 f"{info['tool_output_visible']}; Python {info['python']}; a key visible in the sandbox: {bool(info['key_hits'])}")
        self.log(f"      credential-like names in the sandbox's environment (names and lengths only): {creds or 'none'}")
        if info["key_hits"]:
            self.log("      STOP: the first 16 characters of " + ", ".join("$" + n for n in info["key_hits"])
                     + " came back from the sandbox. Rotate that key before going on.")


async def paid(cfg: dict, doc: dict, scope: list[dict], *, client: HRClient, prices: dict, raw: Raw, runs: Path,
               steps: tuple[str, ...] = STEPS, balance_pause: float = BALANCE_PAUSE,
               sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
               tr_usage: Callable[[], Awaitable[float | None]] | None = None,
               log: Callable[[str], None] = print) -> dict:
    """Checks 5 to 9 on the rows in `scope`, one row at a time, written into `doc`.
    Returns {"ran": [row ids], "refused": {row id: why}, "silent": [row ids no turn of which was answered]}."""
    budget = Budget(cfg["budget"], prices, runs, run=RUN, tr_usage=tr_usage, log=log)
    await budget.start(client)
    grader = schema.load_grader()
    ran, refused, silent = [], {}, []
    for row in scope:
        rid, entry = row["id"], row_entry(doc, row)
        log(f"ROW {rid}: {row['base']} + {row['model']}")
        need = ROW_EST_USD * (1 + 0.5 * ("late" in steps) + 0.5 * ("cap" in steps))      # two more tiny turns each
        guard = budget.check([row], need=need, what=f"preflight {rid}")
        if not guard["ok"]:
            refused[rid] = (f"prices.yaml has no entry for {row['model']!r}: its turns could not be costed" if guard.get("unpriced")
                            else f"the cost guard refuses it: {guard['need_usd']:.2f} USD needed, {guard['remaining_usd']:.2f} USD remain above the floor")
            log(f"    not checked: {refused[rid]}")
            continue
        worker = _Row(client, row, budget=budget, raw=raw, grader=grader, sleep=sleep, log=log)
        before = {k: entry.get(k) for k in ("observed", "hand", "at", "payer", "provider")}
        entry.update(payer=worker.payer, provider=worker.provider, at=now(), observed=worker.obs)
        entry.pop("hand", None)                         # what the owner confirmed was about the turns before these
        entry.pop("last_try", None)
        ran.append(rid)
        try:
            await worker.run(steps, balance_pause)
        finally:                                        # also when the command is stopped in the pause: the turns were paid for
            if not worker.answered:
                # No turn was answered (the harness or the first POST was refused, or its reply never came): that
                # is no finding about the row. What its earlier check showed stands, hand readings and pin included.
                silent.append(rid)
                if before["observed"]:
                    entry.update({k: v for k, v in before.items() if v is not None})
                    entry["last_try"] = {"at": now(), "steps": worker.obs["steps"], "failure": worker.obs.get("failure"),
                                         "ledger_usd": worker.obs.get("ledger_usd")}
            save(runs, doc)
    return {"ran": ran, "refused": refused, "silent": silent}


# --------------------------------------------------------------------------- `rj preflight`

def scope_of(matrix: list[dict], rows: list[str] | None, *, core: bool = False) -> list[dict]:
    """The matrix lines a check covers: the rows named, else every row (`core`: the rows whose role is core)."""
    by_id = {r["id"]: r for r in matrix}
    if rows:
        unknown = [r for r in rows if r not in by_id]
        if unknown:
            raise ValueError(f"no row {', '.join(unknown)} in the matrix file (have {', '.join(by_id)})")
        return [by_id[r] for r in dict.fromkeys(rows)]
    return [r for r in matrix if not core or r.get("role") == "core"]


async def run(cfg: dict, matrix: list[dict], *, free_checks: bool = False, paid_checks: bool = False,
              rows: list[str] | None = None, steps: tuple[str, ...] = STEPS, wallet_usd: float | None = None,
              confirm_usage_log: list[str] | None = None, accept_served: list[str] | None = None,
              runs_dir: str | Path = "runs", client: HRClient | None = None, prices: dict | None = None,
              balance_pause: float | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
              transport: httpx.AsyncBaseTransport | None = None, log: Callable[[str], None] = print) -> dict:
    """`rj preflight`: run the checks asked for, take the hand readings, write <runs>/preflight.json.

    cfg, matrix   a loaded profile and the rows of its matrix file
    rows          the rows covered; default every row for the free checks, the rows whose role is core for the paid ones
    client        an HRClient; made from HR_BASE_URL and HR_API_KEY (and closed again) when not given
    prices        the price book; default prices.yaml
    balance_pause seconds before the balance is read again after a row's turns; default 90, or 0 when every row is the stub's
    transport     for the calls to TokenRouter itself (tests)

    steps         the paid turns to send, of ALL_STEPS; `late` (31 minutes of waiting) and `cap` are not in the default

    Returns {"doc", "path", "ran": [check ids], "rows": [row ids checked], "refused": {row: why}, "proposals": [...],
    "leaked": [row ids], "failed": [...], "silent": [row ids], "exit_code"}: 0; 1 when a check that ran now fails,
    or a row got no answer to any turn (`silent`); 2 when a key showed in a sandbox or a row was refused (no
    price, or the cost guard).
    Raises ValueError for a row the matrix does not hold, for a step that is none, when every row is the stub's
    but the base URL is not this machine, and when <runs>/preflight.json holds the checks of another service.
    """
    runs = resolve_runs(runs_dir)
    if set(steps) - set(ALL_STEPS):
        raise ValueError(f"steps are {', '.join(ALL_STEPS)}; got {', '.join(sorted(set(steps) - set(ALL_STEPS)))}")
    for rid in [*(confirm_usage_log or []), *(accept_served or [])]:
        scope_of(matrix, [rid])
    free_scope = scope_of(matrix, rows) if free_checks else []
    paid_scope = scope_of(matrix, rows, core=True) if paid_checks else []
    covered = free_scope + paid_scope
    stub = bool(covered) and all(r["base"] == STUB_BASE for r in covered)
    base_url = client.base_url if client is not None else os.environ.get("HR_BASE_URL")
    if stub and base_url and not is_loopback(base_url):
        raise ValueError(f"every row covered is served by the stub, but the base URL ({base_url}) is not this machine: "
                         "start the stub and point HR_BASE_URL at it")
    book = prices if prices is not None else schema.load_prices()
    service = service_of(base_url) or current_service()
    doc, ran, outcome = load(runs, service), [], {"ran": [], "refused": {}, "silent": []}
    if service is not None:
        doc["service"] = service
    if covered:
        doc["account"]["edition"] = cfg["access"]["edition"]
    own = client is None and bool(covered)
    if own:
        client = HRClient(edition=cfg["access"]["edition"], poll_seconds=0.05 if stub else 2.5)
    try:
        if free_checks:
            ran += await free(cfg, doc, free_scope, client=client, prices=book, raw=Raw(runs, "free"), wallet_usd=wallet_usd,
                              transport=transport, log=log)
        if paid_checks:
            usage = None
            if stub and not is_loopback(tr_base()):
                usage = no_tr_usage                     # a stub run never asks the real route
            elif transport is not None:
                async def usage() -> float | None:
                    return await fetch_tr_usage(transport=transport)
            pause = balance_pause if balance_pause is not None else 0.0 if stub else BALANCE_PAUSE
            outcome = await paid(cfg, doc, paid_scope, client=client, prices=book, raw=Raw(runs, "paid"), runs=runs, steps=steps,
                                 balance_pause=pause, sleep=sleep, tr_usage=usage, log=log)
            ran += ["5", "6", "7", "8", "9", *(["caps"] if "cap" in steps else [])] if outcome["ran"] else []
    finally:
        if own:
            await client.aclose()

    by_id = {r["id"]: r for r in matrix}
    if wallet_usd is not None:
        doc["account"]["w0_usd"] = float(wallet_usd)
    for rid in confirm_usage_log or []:
        entry = row_entry(doc, by_id[rid])
        entry.setdefault("hand", {})["usage_log"] = now()
        if not entry.get("observed"):
            log(f"{rid}: the usage log is noted as confirmed, but no paid check of this row is on record yet")
    for rid in accept_served or []:
        entry = row_entry(doc, by_id[rid])
        seen = list(dict.fromkeys(t.get("served_model") for t in (entry.get("observed") or {}).values()
                                  if isinstance(t, dict) and t.get("status") == "completed" and t.get("served_model")))
        if len(seen) == 1:
            entry.setdefault("hand", {})["served"] = seen[0]
        else:
            log(f"{rid}: nothing to accept: its checks on record show " + ("no served model" if not seen else f"several ({', '.join(seen)})"))
    path = save(runs, doc)

    def say(rid: str) -> dict:
        """One line on what a row's checks 5 to 8 say now, and one more for each that is open."""
        entry = doc["rows"][rid]
        checks = entry.get("checks") or {}
        names = [n for n in ("5", "6", "7", "8", "caps") if n in checks]
        log(f"{rid}: " + (", ".join(f"check {n} {checks[n]['status']}" for n in names) or "no paid check on record")
            + f"; pin {entry['pin_served_model']!r}; tool output visible: {entry['tool_output_visible']}")
        for n in names:                                 # what is open says what to do about it
            if checks[n]["status"] in ("fail", "cannot_tell"):
                log(f"    check {n} {checks[n]['status']}: {checks[n]['evidence']}")
        return checks

    proposals, leaked = [], []
    for rid in outcome["ran"]:
        entry = doc["rows"][rid]
        if rid in outcome["silent"]:
            tried = entry.get("last_try") or {"failure": (entry.get("observed") or {}).get("failure")}
            log(f"{rid}: NO TURN WAS ANSWERED ({(tried.get('failure') or {}).get('text') or 'no reply'}): this is no finding about the "
                "row" + ("; what its earlier check showed stands:" if entry.get("last_try") else "; run the check again"))
            if entry.get("last_try"):
                say(rid)
            continue
        failure = (entry.get("observed") or {}).get("failure") or {}
        if ((entry["observed"].get("files") or {}).get("key_hits")):
            leaked.append(rid)
        checks = say(rid)
        failed = [n for n in ("5", "6", "7", "8") if (checks.get(n) or {}).get("status") == "fail"]
        # Section 3, "Replacing a row": only a model reason steps a row up. That is a provider error that names
        # neither the account nor a rate limit, or a conversation lost between turns; a served string that
        # differs is the owner's to accept or not, and a fault of the account or the service is no finding.
        lost = "7" in failed and failure.get("kind") is None and entry["observed"].get("task_is_session") is not True
        if entry["observed"].get("task_is_session") is True:
            log(f"{rid}: the account's 30-minute task limit ends a session (check 7): stop and ask HarnessRouter; the row is not stepped up")
        elif failed and failure.get("kind") == "account":
            log(f"{rid}: an account reason (credit, quota, a refused key, routing, a limit of the plan): the row is not stepped up")
        elif failed and (failure.get("kind") == "model" or lost):
            step_up = by_id[rid].get("next")
            if step_up:
                proposals.append(f"rj replace {rid} {step_up}")
                log(f"{rid}: proposed, not done, if this is a model reason: rj replace {rid} {step_up}")
            else:
                log(f"{rid}: may be a model reason, but the row lists no step-up (`next`) in the matrix")
    for rid in dict.fromkeys([*(confirm_usage_log or []), *(accept_served or [])]):     # a hand reading shows what it changed
        if rid not in outcome["ran"]:
            say(rid)
    ran = [n for n in sorted(set(ran), key=lambda n: (not n.isdigit(), int(n) if n.isdigit() else 0)) if n in doc["checks"]]
    for n in ran:
        every = n in ("5", "6", "7", "8", "9", "caps")
        log(f"check {n}{', over every row on record' if every else ''}: {doc['checks'][n]['status']}"
            + (f": {doc['checks'][n]['evidence']}" if n in ("7", "caps") and doc["checks"][n]["status"] != "pass" else ""))
    if wallet_usd is not None and not free_checks:
        log(f"wallet: {doc['account']['w0_usd']} USD noted (read by the owner)")
    log(f"wrote {path}")
    # A row checked on another day keeps its verdict in the file; only what ran now decides how this call ends.
    failed = [n for n in ran if n in ("1", "2", "3", "9", "caps") and doc["checks"][n]["status"] == "fail"]
    failed += [f"{rid}:{n}" for rid in outcome["ran"] if rid not in outcome["silent"]
               for n, c in (doc["rows"][rid].get("checks") or {}).items() if c["status"] == "fail" and n != "caps"]
    failed += [f"{rid}:no turn answered" for rid in outcome["silent"]]
    code = 2 if leaked or outcome["refused"] else 1 if failed else 0
    return {"doc": doc, "path": path, "ran": ran, "rows": outcome["ran"], "refused": outcome["refused"],
            "proposals": proposals, "leaked": leaked, "failed": failed, "silent": outcome["silent"], "exit_code": code}
