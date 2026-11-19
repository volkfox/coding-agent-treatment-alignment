"""One session as a journaled state machine. Plan section 6 ("Session sequence", "Rules"), section 7
("Next send") and section 11.

    result = await run_session(ctx, manifest_entry, matrix_row)

States, each one line of sessions/<key>/journal.jsonl, fsynced before the next step is taken:

    NEW  HARNESS
    seed turn:            SENDING SENT POLLING PULLED CLASSIFIED    then SEEDED (the seed and the planted files are up)
    each entry point:     PUT (the `before` uploads)
                          SENT POLLING PULLED CLASSIFIED            the entry point's message
                          FALLBACK_SENT POLLING PULLED CLASSIFIED   each reply to an unplanned question, at most fallback.max_per_ep
                          ADAPTIVE                                  before the FALLBACK_SENT of a reply that may be adaptive
    canary turn:          PUT SENT POLLING PULLED CLASSIFIED        then CANARY (was the conversation kept?)
    DONE                  all six entry points and the canary were sent (`until`: a live session cut short)
    ENDED                 a rejected follow-up, a cap or a timeout ended it; or it was infra twice (infra_failed)
    INFRA_RETRY           the attempt was infra: its directory is kept as <key>.infraN and a new attempt starts
    ERROR                 a note, not a state: an exception nobody planned for (`Journal.state` looks past it)
    REDO                  written by `redo` (rj.runner) as an attempt is put away: the next attempt is a first one again

What each line promises on a restart:
    HARNESS               every turn of this attempt goes to this harness, whatever the row's pin says by then;
    SENDING               the seed POST is about to be made with this key (no session id exists to look it up by);
    SENT / FALLBACK_SENT  the POST was accepted and this is its response id: it is polled, never sent again; what it
                          carried is the `input` of the send's request.json, and the turn is held against those bytes;
    ADAPTIVE              what this reply says is settled: `text` holds the message as it is posted, the adaptive
                          answer (`answer`) under the habit's fallback line (`fallback_line`), with `used` true; or
                          `reason` says why the fixed line goes (`used` false). The writer is not asked again for
                          this send, and the same bytes are posted: the stored `text`, whatever the files say by then;
    PULLED                request.json, response.json, turns_row.json, trace.ndjson and archive.zip are on disk;
    CLASSIFIED            stop.json is written, the turn is in the ledger, and `next` says what follows
                          ("advance", "fallback", "end" or "infra", with the flags the failure carried).
What a send says is settled when its request.json is written, just before the POST: scripted message, fixed
reply or adaptive answer alike. A later process sends, takes over or checks those same bytes, whatever the
scenario, the blocks and the wording file say by then (`Session.words`); only a new attempt, in a new
directory, is built from the files again.

One harness per row. Every session of a row in a run directory starts on the same harness: the one the row's
first session was given, kept in <run>/pins.json (`harness_id`; `harness_id_from` is that session's key) and
taken from there by every later session, in this process or a later one, with no POST. On 18 and 19 Nov 2026
the service honoured the Idempotency-Key of POST /v1/harnesses for about a day: a runner started later was
given a second harness of the same name, base and model, and each row's sessions were split over two ids.
Once per process and row, before a pinned id is first used, the service is asked for it
(GET /v1/harnesses/{id}). A 404 that names `harness_not_found` is said in one log line, and a new harness is
asked for, under a key of its own, and pinned in its place; where the service hands the lost id back and
still does not know it, the attempt is infra and the pin stays. Any other failure, a 404 that does not name
the harness among them, is read as the POST's would be (waited out, then Resumable or infra) and leaves the
pin as it is. Sessions of one row that start together settle this one after the other, so they end on one id
after at most one POST; what one of them gives up with is the answer of those waiting behind it too, not
waited out again by each. A session whose journal has a HARNESS line keeps that id to its end, where the pin
differs too, and never writes the pin; a new attempt, in a new directory, starts on the row's pin like any session.

A kill between a POST and its SENT line leaves a turn the journal does not know. So the first POST after a
restart first looks at the session's turns: a last turn that is not the one the journal ends on and carries
the text about to be sent is that POST, and is taken over. The same look is taken before a follow-up POST
that got no reply or a 5xx is made again. The seed POST cannot be looked up: a SENDING line without its SENT
line means a session with one "Reply READY." turn may have been opened and left behind; it is logged and
ledgered as one seed turn (flag "orphan"). The Idempotency-Key `<run>:<key>:<attempt>:<ep>:<send>:<nonce>`
is the second guard (whether Cloud honours the header is not confirmed); the nonce is drawn once per attempt
and kept in its NEW line, so a directory removed by hand never brings an old key back. When a session is
pulled, a turn in it that the journal does not know makes the attempt infra: a POST was taken twice.

Rules kept here: one coroutine per session and every POST waits for the turn before it, so nothing is posted
while a turn is live; plain text only; `max_step` and `timeout_seconds` from the profile's caps go with every
turn; the model goes with the seed turn only.

The ladder (plan section 7, "Next send"). A treated turn that ends short of its entry point's goal with a
question gets a reply to it, at most fallback.max_per_ep of them; then the next entry point is sent, always
with its scripted block. Every such reply is the habit's fallback line, a blank line and an answer
(script.fallback_reply; the answer alone where the scenario's fallback.toned is false, as every such reply went
before 18 Nov 2026). The line is the script's to choose: the fallback cell of the habit's tone, never with an
insert, and the neutral one at every stop in a habit whose mask tones some stops only (F-once, E-once, F-late).
Send 1 carries the fixed fallback text (script.fallback_message(habit, ep)). From send `adaptive.from_send` on
(2: the agent asked again after the fixed line) the answer is an adaptive one, written by a model
(rj/runner/adaptive.py), when the profile has `adaptive.enabled`, the session has had fewer than
`adaptive.max_per_session` of them, the writer can be reached (a key, a priced model), the cost guard has
something left (it is asked before the call), no message the agent has written at this entry point names the
record, the story or the threat (STIMULI.md, section 1: then neither the guard nor the writer is asked; nor are
they when nothing of the agent's last message is left for the writer to read) and the text passes every check
(one that names any of them is dropped). In any other case the fixed fallback text goes again under the same
line, and the reason is recorded. An adaptive answer is a kind of fallback send: it has the same place in the journal
(FALLBACK_SENT) and it never stands in for a scripted message. What it will say is settled in an ADAPTIVE line
before the POST, line and answer together, so the no-turn-twice guards above hold for it as they do for the
fixed line: a restart posts the stored bytes or takes over the turn that carries them. If the process stops
while the writer is being asked, or between its ledger line and the ADAPTIVE line, the next call asks it once
more; every call that came back is one ledger line (kind "adaptive", provider tokenrouter), written first thing
when the call returns, with `written`: whether it gave a text that passed its checks. Whatever goes wrong
between that line and the ADAPTIVE line (a text that cannot be settled), the ADAPTIVE line says `used` false
with the reason and the fixed line goes. How many answers were sent is read from the FALLBACK_SENT lines, not
from the ledger. Which message a send carried is `message_kind` ("scripted",
"fallback", "adaptive") in request.json (beside the posted body), in stop.json and in the SENT or
FALLBACK_SENT line. For a reply the same three places say which line went out above the answer:
`fallback_line` (its text; null: none) and `fallback_tone` (the wording column it is the fallback cell of: n,
e, g, f or t; null with no line). stop.json's `adaptive` says whether an adaptive answer went and, if not, why;
where the writer was called, adaptive.json beside them keeps the prompt and what it wrote. A run made before
these fields reads as it did: send 0 scripted, the rest the fixed line; an ADAPTIVE line of such a run has no
`answer`, and its `text` is the answer, posted with no line above it.

A scripted message that does not fit the agent's last stop (rj/fit.py: it decides a fork the agent did not
ask about, or presumes a plan or a hand-back that did not come) is sent all the same, and one note line says so.

Patience. A turn that was posted is polled until it is over: no reply, a 5xx, a 409 or a 429 on the way are
waited out for as long as the turn may be live (the cap's timeout plus five minutes from its SENT line) and
at least `patience` seconds. Every other GET and PUT, and a POST answered 409 or 429 or never connected (the
service took nothing), is tried again with a growing pause for `patience` seconds (default: the same span).
A POST that was sent and got no reply or a 5xx is looked for and made again `post_tries` times in all. When
the patience runs out:
  * the service gave no reply at all, or the turn is still live: `Resumable`. The session stays as its
    journal stands, launching stops, and the next call takes it up. No attempt is spent on an outage.
  * the service answered 5xx or 429 to the end: infra (section 11).

Section 11's failure rule: infra reruns the whole session once. Before the first attempt is put away as
<key>.infraN every response it posted is waited for and ledgered (`settle`): none is left live or unpaid
behind a rerun. The rerun then waits (`rate_pause` after a provider 429, else `retry_pause`), asks the service
one plain GET, and asks the cost guard for a full session; refused, it stays INFRA_RETRY for a later call.
A reply that cannot be read (a 2xx that does not fit the contract) is kept as response.raw.json, ledgered,
and ends the session infra_failed without a rerun: a blind rerun would pay for the same reply twice.
A canary turn that goes wrong is recorded and the session still ends DONE: its six entry points are paid for.
`redo` (rj.runner) gives a session that ended another life under a new attempt number.

The cost guard between turns. Before every POST of a session (each entry point's message, each fallback, the
canary) the guard is asked whether anything is left of total_usd and of the wallet figure (`Budget.exhausted`;
for a row paid on TokenRouter its usage figure is read first, so the answer is the provider's, not the
ledger's estimate). When nothing is left the message is not posted: `Resumable`, launching stops, and the
session stays as its journal stands until the owner raises the total and runs the same command. The guard is
asked the same before the writer of an adaptive answer is called; refused, the writer is not called and the
fixed line is what waits to be posted.

The account's plan. A failed turn whose error names the plan, a task or time limit, or how many tasks may run
at once (stops.names_plan_limit) is an account reason: launching stops, and the session ends infra_failed
with no rerun, since a rerun would meet the same limit after paying for the same turns.

meta.json carries `state`: RUNNING from the first step, then DONE, ENDED or INFRA_RETRY, with `outcome`. The
grader skips a session whose journal has not reached DONE or ENDED.
"""
from __future__ import annotations

import asyncio
import calendar
import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from rj import fit, schema, script
from rj.grade import mechanical, trace
from rj.hr import HRClient, HRError, is_rejected_followup
from rj.runner import adaptive, manifest, stops
from rj.runner.budget import TOKENROUTER, Budget, current_service, now, same_service
from rj.runner.manifest import _records, append_lines, read_json, read_lines, session_dir, write_atomic, write_json

TERMINAL = ("DONE", "ENDED")
JOURNAL, META, PINS, PREFLIGHT, LOCK = "journal.jsonl", "meta.json", "pins.json", "preflight.json", ".lock"
PROBE_TMP = ".probe-tmp"                # in the run directory: where a pulled archive is unpacked for its probes
ABOVE = ("fallback_line", "fallback_tone")      # what request.json, stop.json and the journal say of the line over a reply
OUTCOME_OF = mechanical.OUTCOME_OF      # stop class -> outcome, as the grader names them
POLL_ROUTE = "GET /v1/responses/{id}"   # HRClient.raw's name for a polled reply: a new object there means an answer came
MAX_PAUSE = 60.0                        # the longest pause between two tries of one call
MISFIT_TRIES = 3                        # a 2xx that is not JSON or does not fit is asked for this many times
ARCHIVE_TRIES = 4                       # an archive that is no readable zip is asked for this many times
TRACE_PATIENCE = 30.0                   # the trace is kept for reference only
SAY_EVERY = 60.0                        # while a call is tried again, the log says so at once and then this often
NOT_TRANSPORT = ("refused before sending", "gave up", "no 404")    # HRError details with status None that are no outage
NEVER_SENT = ("ConnectError", "ConnectTimeout", "PoolTimeout")     # ... and those where no connection was ever made
HARNESS_GONE = "harness_not_found"      # the error code of a 404 for a harness id the service does not know
KEPT = re.compile(r"\.infra(\d+)$")


class Infra(Exception):
    """The attempt cannot go on for a reason that is not the agent's (plan section 11's list).

    kind: "account" (credit, quota, a refused key: launching stops), "rate" (a provider 429), "provider" (a
    failed turn naming a provider 4xx that is neither: the model may be what was refused), "transport" (5xx
    or 429 from the service to the end), "check" (the per-turn check), "reply" (a reply that cannot be read),
    "turn" (any other failed turn) or "other".
    final: the session is not run again (kind "reply").
    """

    def __init__(self, reason: str, detail: str = "", *, stop_launching: bool = False, rate_limited: bool = False,
                 unanswered: bool = False, no_reply: bool = False, kind: str = "other", final: bool = False):
        super().__init__(reason)
        self.reason, self.detail = reason, detail or reason
        self.stop_launching, self.rate_limited = stop_launching, rate_limited
        self.unanswered = unanswered        # a POST that got no reply or a 5xx: it may have been taken all the same
        self.no_reply = no_reply            # ... and there was no reply at all
        self.kind, self.final = kind, final


class Rejected(Exception):
    """A follow-up POST was turned down for good (a 4xx other than 409 and 429): the session has ended."""


class Resumable(Exception):
    """The session cannot go on now and nothing is known to be wrong with it: the service gave no reply for as
    long as the runner waits, or a posted turn is still live past its time. The session is left as its
    journal stands; launching stops; the next call takes it up. `live`: the turn was still live."""

    def __init__(self, reason: str, *, live: bool = False):
        super().__init__(reason)
        self.reason, self.live = reason, live


class RunLocked(RuntimeError):
    """Another runner holds <runs>/<run>/.lock: two processes must never follow the same journals."""


class RunLock:
    """An exclusive flock on <run_dir>/.lock, counted, so `run` and the sessions under it can each ask for it."""

    def __init__(self, run_dir: str | Path):
        self.path = Path(run_dir) / LOCK
        self._fd: int | None = None
        self._count = 0

    def __enter__(self) -> "RunLock":
        if self._count == 0:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise RunLocked(f"{self.path.parent} is in use by another runner (its .lock is held): wait for it, or "
                                "stop it, before running this run again") from None
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {now()}\n".encode("ascii"))
            self._fd = fd
        self._count += 1
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._count -= 1
        if self._count == 0 and self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def no_reply(e: HRError) -> bool:
    """True when a call got no reply from the service at all (a transport failure, not a refusal of the client's)."""
    return e.status is None and not (e.detail or "").startswith(NOT_TRANSPORT)


def never_sent(e: HRError) -> bool:
    """True when the request cannot have reached the service: no connection was made. A POST is then safe to make again."""
    return e.status is None and (e.detail or "").startswith(NEVER_SENT)


def transient(e: HRError) -> bool:
    """No reply, a 5xx, a 409 or a 429: worth asking again."""
    return no_reply(e) or (e.status is not None and (e.status >= 500 or e.status in (409, 429)))


def misfit(e: HRError) -> bool:
    """A 2xx whose body is not JSON or does not fit the contract."""
    return e.status is not None and 200 <= e.status < 300


def harness_gone(e: HRError) -> bool:
    """A 404 whose body names `harness_not_found`: the service knows no harness of that id. A 404 that does not
    name it (a route this edition does not serve, a gateway's) says nothing about the harness."""
    said = " ".join(t for t in (e.excerpt, (e.body or b"").decode("utf-8", "replace")) if t)
    return e.status == 404 and HARNESS_GONE in said


def _epoch(ts: str | None) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(ts or "", "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def age(ts: str | None) -> float:
    """Seconds since a journal line's `ts`, by the wall clock; 0 when it cannot be read."""
    then = _epoch(ts)
    return max(0.0, time.time() - then) if then is not None else 0.0


# --------------------------------------------------------------------------- what a run shares between its sessions

class Pins:
    """Per row the served model (and connection) every turn is held to. Plan section 4, "Per-turn check".

    <runs>/preflight.json gives the pin where it has one; otherwise the first non-empty string a row returns
    becomes it, kept in <run>/pins.json so a restart holds the row to the same string. A preflight.json that
    was written against another service than `service` (the stub's checks beside a real run) gives no pin.

    The same file holds the row's harness (module docstring, "One harness per row"): `harness_id`, and under
    `harness_id_from` the key of the session that first obtained it. `get` does not return it; `harness`
    does. A pins.json written before 19 Nov 2026 has neither key: the row then has no harness yet.
    """

    def __init__(self, run_dir: str | Path, runs_dir: str | Path, service: str | None = None):
        self.path = Path(run_dir) / PINS
        found = read_json(self.path)
        self.rows: dict[str, dict] = found if isinstance(found, dict) else {}
        preflight = read_json(Path(runs_dir) / PREFLIGHT)
        if not same_service(preflight, service if service is not None else current_service()):
            preflight = None
        for row, got in ((preflight or {}).get("rows") or {}).items() if isinstance(preflight, dict) else ():
            for name in ("served_model", "connection"):
                if isinstance(got, dict) and got.get(f"pin_{name}"):
                    self.rows.setdefault(row, {}).update({name: got[f"pin_{name}"], f"{name}_from": "preflight"})

    def get(self, row: str) -> dict:
        own = self.rows.get(row) or {}
        return {"served_model": own.get("served_model") or None, "connection": own.get("connection") or None}

    def harness(self, row: str) -> str | None:
        """The id of the row's harness in this run directory; None while no session of the row has obtained one."""
        found = (self.rows.get(row) or {}).get("harness_id")
        return found if isinstance(found, str) and found else None

    def learn(self, row: str, name: str, value: str, session: str) -> None:
        self.rows.setdefault(row, {}).update({name: value, f"{name}_from": session})
        write_json(self.path, self.rows)


@dataclass
class Context:
    """One run's shared parts. Every wait goes through `sleep`, so a test can make them all instant."""
    client: HRClient
    run: str
    run_dir: Path
    runs_dir: Path
    profile: dict
    scenario: dict
    grader: dict
    budget: Budget
    pins: Pins
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    balance_pause: float = 90.0         # before the balance is read again after a session (it can be a minute old)
    rate_pause: float = 60.0            # before the new attempt when the provider answered 429
    retry_pause: float = 30.0           # before the new attempt after any other infra
    refetch_pause: float = 60.0         # before a turn that failed with no error is asked for again
    poll_timeout: float | None = None   # how long one turn may be live; None: the cap's timeout plus five minutes
    patience: float | None = None       # how long no reply, 5xx, 409 and 429 are waited out; None: as long as a turn may be live
    post_tries: int = 3                 # a POST that got no reply or a 5xx is looked for and made again, this often in all
    http_backoff: float = 2.0
    sandbox_mode: str | None = None
    log: Callable[[str], None] = print
    stopped: str | None = None          # why no further session or attempt is launched
    refused: dict | None = None         # the cost guard's verdict that stopped a stage, a session start or a rerun
    reserved: Callable[[str], float] = lambda key: 0.0     # what the other sessions in flight are still expected to cost
    lock: RunLock | None = None         # the run directory's lock; `run` holds it, `run_session` takes it for a direct caller
    usage_done: dict | None = None      # session key -> TokenRouter's usage figure (cents) read as the session ended in this call
    harness_locks: dict | None = None   # row id -> asyncio.Lock: one session at a time settles which harness its row is on
    harness_seen: set | None = None     # the rows whose harness the service has confirmed, or made, in this process
    harness_failed: dict | None = None  # row id -> the Infra or Resumable the last session to settle it gave up with

    def __post_init__(self) -> None:
        if self.lock is None:
            self.lock = RunLock(self.run_dir)
        if self.usage_done is None:
            self.usage_done = {}
        if self.harness_locks is None:
            self.harness_locks = {}
        if self.harness_seen is None:
            self.harness_seen = set()
        if self.harness_failed is None:
            self.harness_failed = {}

    @property
    def limits(self) -> dict:
        caps = self.profile["caps"]
        return {"max_step": caps["max_step"], "timeout_seconds": caps["timeout_seconds"]}

    @property
    def turn_timeout(self) -> float:
        return self.poll_timeout if self.poll_timeout is not None else self.profile["caps"]["timeout_seconds"] + 300.0

    @property
    def patient(self) -> float:
        return self.patience if self.patience is not None else self.turn_timeout

    def stop(self, reason: str) -> None:
        if self.stopped is None:
            self.stopped = reason
            self.log(f"launching stopped: {reason}")


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: list[dict] = [e for e in read_lines(self.path) if "state" in e]    # a torn line: its step is taken again

    @property
    def state(self) -> str | None:
        """The last state reached; ERROR lines are notes and are looked past."""
        return next((e["state"] for e in reversed(self.entries) if e["state"] != "ERROR"), None)

    def write(self, state: str, **data: Any) -> dict:
        entry = {"ts": now(), "state": state, **data}
        append_lines(self.path, [entry])
        self.entries.append(entry)
        return entry

    def last(self, state: str, **match: Any) -> dict | None:
        for entry in reversed(self.entries):
            if entry["state"] == state and all(entry.get(k) == v for k, v in match.items()):
                return entry
        return None


def above(line: str | None) -> dict:
    """What request.json, stop.json and the FALLBACK_SENT line say of the line above a reply's answer: its text and
    the tone column it is the fallback cell of. Both null: no line went out."""
    tone = next((column for column, cell in script.load_fallback_lines().items() if cell == line), None) if line else None
    return {"fallback_line": line or None, "fallback_tone": tone}


def infra_dir(sdir: Path, attempt: int = 1) -> Path:
    """Where an attempt that was put away is kept: <key>.infra<attempt>."""
    return sdir.with_name(f"{sdir.name}.infra{attempt}")


def kept_attempts(sdir: Path) -> list[tuple[int, Path]]:
    """(attempt number, directory) of every attempt of a session that was put away, oldest first."""
    found = []
    for d in sdir.parent.glob(sdir.name + ".infra*") if sdir.parent.is_dir() else []:
        m = KEPT.search(d.name)
        if m and d.is_dir() and d.name[:m.start()] == sdir.name:
            found.append((int(m.group(1)), d))
    return sorted(found)


def attempt_of(sdir: Path) -> int:
    """The number of the attempt that lives, or will live, in a session's own directory."""
    kept = kept_attempts(sdir)
    return kept[-1][0] + 1 if kept else 1


def is_rerun(sdir: Path) -> bool:
    """True when the attempt in a session's own directory is the one rerun of an infra attempt (its last
    chance). False for a first attempt, and for the first attempt after `redo`."""
    kept = kept_attempts(sdir)
    return bool(kept) and Journal(kept[-1][1] / JOURNAL).state == "INFRA_RETRY"


def in_flight(run_dir: str | Path, entry: dict) -> tuple[str, int] | None:
    """(ep, send) of a response a session posted and has not classified yet; None when it has none."""
    journal = Journal(session_dir(run_dir, entry["key"]) / JOURNAL)
    if journal.state in (None, "INFRA_RETRY", *TERMINAL):
        return None
    for e in reversed(journal.entries):
        if e["state"] in ("SENT", "FALLBACK_SENT"):
            return None if journal.last("CLASSIFIED", ep=e["ep"], send=e["send"]) else (e["ep"], e["send"])
    return None


def upload_files(spec: str, workspace: str) -> dict[str, bytes]:
    """What one `put` entry of the scenario uploads: a directory laid over the workspace root, or every planted file
    the workspace renders (task/records.py: the history, the changelog and, in `cover` and `threat`, SCRATCHPAD.md)."""
    if spec.startswith("variant:"):
        return {name: text.encode("utf-8") for name, text in _records().render(workspace).items()}
    base = schema.ROOT / spec
    if not base.is_dir():
        raise FileNotFoundError(f"the scenario puts {spec!r}, which is no directory under the repository")
    out = {}
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if p.is_file() and "__pycache__" not in rel.parts and p.suffix != ".pyc" and not any(x.startswith(".") for x in rel.parts):
            out[rel.as_posix()] = p.read_bytes()        # dot-paths are refused on upload
    return out


# --------------------------------------------------------------------------- one attempt

class Session:
    def __init__(self, ctx: Context, entry: dict, row: dict, attempt: int, until: str | None = None,
                 directory: Path | None = None):
        self.ctx, self.entry, self.row, self.attempt, self.until = ctx, entry, row, attempt, until
        self.key, self.habit = entry["key"], entry["habit"]
        self.dir = directory or session_dir(ctx.run_dir, self.key)     # `directory`: an attempt that was put away
        self.journal = Journal(self.dir / JOURNAL)
        self.workspace = script.load_habits()["habits"][self.habit]["workspace"]
        self.points = ctx.scenario["entry_points"]
        self.first_treated = next(i for i, p in enumerate(self.points) if p.get("treated", True))
        self.harness_id: str | None = None
        self.sid: str | None = None
        self.prev: str | None = None                    # the session's last response id
        self.nonce: str | None = None                   # drawn in the NEW line; part of every Idempotency-Key
        self.trace_seen: tuple[int, str | None] = (0, None)
        self.resumed = bool(self.journal.entries)
        for e in self.journal.entries:                  # take up where the journal ends
            if e["state"] == "NEW":
                self.nonce = e.get("nonce")
            if e["state"] == "HARNESS":
                self.harness_id = e["harness_id"]
            if e.get("session_id"):
                self.sid = e["session_id"]
            if e["state"] in ("SENT", "FALLBACK_SENT"):
                self.prev = e["response_id"]
            if e["state"] == "PULLED":
                self.trace_seen = (e.get("trace_chunks") or 0, e.get("trace_tail"))

    def turn_dir(self, ep: str, send: int) -> Path:
        return self.dir / "turns" / schema.turn_dir(ep, send)

    def meta(self, **found: Any) -> None:
        path = self.dir / META
        have = read_json(path)
        write_json(path, {**(have if isinstance(have, dict) else {}), **found})

    def idempotency_key(self, ep: str, send: int) -> str:
        key = f"{self.ctx.run}:{self.key}:{self.attempt}:{ep}:{send}"
        return f"{key}:{self.nonce}" if self.nonce else key     # a journal begun before nonces: its keys as they were

    # ----------------------------------------------------------------------- the sequence

    async def play(self) -> str:
        """Run the session from where its journal ends. Returns "DONE" or "ENDED"; raises Infra or Resumable."""
        j, ctx = self.journal, self.ctx
        if not j.entries:
            self.dir.mkdir(parents=True, exist_ok=True)
            if not (self.dir / "manifest.json").exists():
                write_json(self.dir / "manifest.json", self.entry)
            self.nonce = uuid.uuid4().hex[:10]
            j.write("NEW", key=self.key, run=ctx.run, attempt=self.attempt, row=self.row["id"], model=self.row["model"],
                    habit=self.habit, nonce=self.nonce, **({"until": self.until} if self.until else {}))
        payer, provider = ctx.budget.payer(self.row["model"])
        self.meta(key=self.key, run=ctx.run, row=self.row["id"], base=self.row["base"], model=self.row["model"],
                  habit=self.habit, attempt=self.attempt, payer=payer, provider=provider, state="RUNNING")
        if self.harness_id is None:                     # a journal that names a harness keeps it, whatever the row's pin says
            self.harness_id = await self.row_harness()
            j.write("HARNESS", harness_id=self.harness_id)
        self.meta(harness_id=self.harness_id, session_id=self.sid, **self.pin_meta())
        for point in self.points:
            if await self.entry_point(point) == "end":
                self.meta(state="ENDED", outcome=j.last("ENDED")["reason"], ended_at=now(), **self.pin_meta())
                return "ENDED"
            if point["id"] == self.until:               # the live demo: the seed and the kickoff only
                break
        done = j.write("DONE", outcome=self.outcome(), **({"until": self.until} if self.until else {}))
        self.meta(state="DONE", outcome=done["outcome"], ended_at=now(), **({"until": self.until} if self.until else {}),
                  **self.pin_meta())
        return "DONE"

    def pin_meta(self) -> dict:
        pin = self.ctx.pins.get(self.row["id"])
        return {"pin_served_model": pin["served_model"], "pin_connection": pin["connection"]}

    async def row_harness(self) -> str:
        """The harness a session starts on when its journal names none: its row's, one for the whole life of
        the run directory (module docstring, "One harness per row").

        The id pinned for the row is used where there is one. Before a process first uses it, the service is
        asked for it once (GET /v1/harnesses/{id}). Only a 404 that names `harness_not_found` says the harness
        is gone: one log line, and a new one is asked for and pinned in its place. That POST carries a key of
        its own (`ensure_harness` with `instead_of`): under the first key a service that still remembers it
        hands the lost harness back. Should the lost id come back all the same, the service is asked for it
        once more; where it still does not know it, the attempt is infra, the pin stays as it is and the row
        counts as unconfirmed, so the next attempt asks again. Any other failure of the GET, a 404 that does
        not name the harness among them, is `http`'s to read, as it is for the POST, and the pin stays as it
        is. Where the row has no pin the harness is made (`ensure_harness`) and pinned under this session's key.

        All of it is done under the row's lock, so sessions of one row that start together end on one id after
        at most one POST. What one of them gives up with there (Infra, Resumable) is also the answer of every
        session of the row that was waiting for the lock by then: none of them waits the same failure out once
        more, one after the other.
        """
        ctx, row_id = self.ctx, self.row["id"]

        async def known(harness_id: str) -> bool:
            try:
                await ctx.client.harness(harness_id)
            except HRError as e:
                if not harness_gone(e):
                    raise
                return False
            return True

        before = ctx.harness_failed.get(row_id)
        async with ctx.harness_locks.setdefault(row_id, asyncio.Lock()):
            failed = ctx.harness_failed.get(row_id)
            if failed is not before:                    # another session of the row gave up on it while this one waited
                raise failed
            try:
                pinned, gone = ctx.pins.harness(row_id), None
                if pinned is not None and row_id not in ctx.harness_seen \
                        and not await self.http(lambda: known(pinned), "the row's harness"):
                    ctx.log(f"row {row_id}: its pinned harness {pinned} is gone from the service (HTTP 404 {HARNESS_GONE}); "
                            f"{self.key} asks for a new one, which is pinned in its place")
                    pinned, gone = None, pinned
                if pinned is None:
                    pinned = await self.http(lambda: ctx.client.ensure_harness(self.row, instead_of=gone), "harness")
                    if pinned == gone and not await self.http(lambda: known(gone), "the row's harness"):
                        raise Infra(f"harness: asked for a new one in place of {gone}, the service gave that id back and "
                                    "still does not know it; the row's pin is left as it is")
                    ctx.pins.learn(row_id, "harness_id", pinned, self.key)
                ctx.harness_seen.add(row_id)
                return pinned
            except (Infra, Resumable) as e:
                ctx.harness_failed[row_id] = e
                raise

    async def entry_point(self, point: dict) -> str:
        j, ep = self.journal, point["id"]
        if j.last("PUT", ep=ep, when="before") is None and self.points.index(point) > 0:
            files = self.files_of((point.get("before") or {}).get("put"))
            await self.put_all(files)
            j.write("PUT", ep=ep, when="before", files=sorted(files))
        send = 0
        while True:
            done = j.last("CLASSIFIED", ep=ep, send=send) or await self.one_send(point, send)
            if done["next"] != "fallback":
                break
            send += 1
        if done["next"] == "infra":
            # The process stopped after this line and before the attempt was closed: close it as it would have
            # been, with what the failure carried (a credit error still stops launching, a 429 still waits).
            raise Infra(done.get("detail") or f"{ep}: infra", stop_launching=bool(done.get("stop_launching")),
                        rate_limited=bool(done.get("rate_limited")), kind=done.get("kind") or "other",
                        final=bool(done.get("final")))
        if done["next"] == "end":
            j.write("ENDED", reason=OUTCOME_OF.get(done["stop_class"], done["stop_class"]), ep=ep, send=send)
            return "end"
        index = self.points.index(point)
        if index == 0 and j.last("SEEDED") is None:
            files = self.files_of((point.get("after") or {}).get("put"))
            await self.put_all(files)
            j.write("SEEDED", session_id=self.sid, files=sorted(files))
        elif index > 0 and (point.get("after") or {}).get("put") and j.last("PUT", ep=ep, when="after") is None:
            files = self.files_of(point["after"]["put"])
            await self.put_all(files)
            j.write("PUT", ep=ep, when="after", files=sorted(files))
        if index == len(self.points) - 1 and not point.get("treated", True) and j.last("CANARY") is None:
            j.write("CANARY", **self.canary(ep))
        return "advance"

    def files_of(self, puts: list[str] | None) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for spec in puts or []:
            files.update(upload_files(spec, self.workspace))
        return files

    async def put_all(self, files: dict[str, bytes]) -> None:
        for path, data in files.items():
            await self.http(lambda path=path, data=data: self.ctx.client.put(self.sid, path, data), f"PUT {path}")

    # ----------------------------------------------------------------------- one send

    async def one_send(self, point: dict, send: int) -> dict:
        """Post one message (or take up one that is in flight), wait, pull, classify. Returns the CLASSIFIED line.

        Raises Infra after the line is written when the turn is infra; lets Resumable through with nothing
        written, so the send is taken up again from the journal.
        """
        j, ctx, ep = self.journal, self.ctx, point["id"]
        tdir = self.turn_dir(ep, send)
        text, kind, settled, over = await self.words(point, send, tdir)
        facts = stops.Facts(entry=point, send=send, sent=text)
        failure: Infra | None = None
        response_id: str | None = None
        try:
            if j.last("PULLED", ep=ep, send=send) is None:
                response_id = await self.post(point, send, text, tdir, kind, over)
                facts.response = await self.poll(ep, send, response_id, tdir)
                facts.row = await self.pull(point, send, facts.response, tdir)
            else:
                facts.response = schema.Response.from_dict(read_json(tdir / "response.json"))
                raw = read_json(tdir / "turns_row.json")
                facts.row = schema.TurnRow.from_dict(raw) if isinstance(raw, dict) else None
        except Rejected as e:
            facts.rejected = str(e)
        except Infra as e:
            failure, facts.infra = e, e.reason
        if facts.response is not None:
            failure = failure or self.check(facts)
            await self.examine(facts, point, send, tdir)
            line = ctx.budget.record_turn(session=self.key, attempt=self.attempt, ep=ep, send=send,
                                          response_id=facts.response.id, row=self.row, usage=facts.response.usage)
            facts.cost_usd = line.get("cost_usd")
        elif response_id and ctx.budget.ledger.turn(response_id):      # a reply that could not be read, ledgered raw
            facts.cost_usd = ctx.budget.ledger.turn(response_id).get("cost_usd")
        if failure is not None and failure.stop_launching:
            ctx.stop(f"{self.key} {ep}: {failure.reason}")

        stop = stops.classify(facts, ctx.scenario, ctx.grader)
        nxt = self.next_step(point, send, stop)
        stop["fallback_sent"] = nxt == "fallback"
        stop["message_kind"] = kind
        stop.update(over)                               # a reply: the fallback line that went out above its answer
        if settled is not None:                         # an adaptive answer was due here: did one go, and if not, why
            stop["adaptive"] = {k: settled.get(k) for k in ("used", "reason", "model", "prompt_sha")}
        if nxt == "infra" and failure is None:          # a failed turn that is neither the account's nor a rate limit
            said = stops.failure_text(facts.response, facts.row) if facts.response is not None else ""
            failure = Infra(stop.get("detail") or f"{ep}: the turn ended {stop['status']}",
                            kind="provider" if stops.names_model_error(said, ctx.grader) else "turn")
        write_json(tdir / "stop.json", stop)
        done = j.write("CLASSIFIED", ep=ep, send=send, stop_class=stop["stop_class"], reached=stop["reached"],
                       question=stop["question"], next=nxt, detail=stop.get("detail"),
                       stop_launching=bool(failure and failure.stop_launching),
                       rate_limited=bool(failure and failure.rate_limited),
                       kind=failure.kind if failure else None, final=bool(failure and failure.final))
        cost = "no cost" if stop["cost_usd"] is None else f"{stop['cost_usd']:.2f} USD by the ledger"
        ctx.log(f"{self.key}: {ep}.s{send} ended {stop['stop_class']} after {stop['seconds'] / 60:.1f} min, {cost}"
                + ("; a reply to its question follows" if nxt == "fallback" else "")
                + f"; spent so far {ctx.budget.spent()['usd']:.2f} USD")
        if nxt == "infra":
            raise failure
        return done

    # ----------------------------------------------------------------------- what a send says

    async def words(self, point: dict, send: int, tdir: Path) -> tuple[str, str, dict | None, dict]:
        """(text, message kind, the send's ADAPTIVE line or None, what stands over a reply's answer) of one send
        (module docstring, "The ladder"). The last is `above`'s dict for a reply and empty for send 0.

        Send 0 is the script's message for the entry point, with the habit's line above the block. Every later
        send is a reply to an unplanned question, the habit's fallback line above an answer (no line where the
        scenario's fallback.toned is false): an adaptive answer where one is settled for it, posted as its
        ADAPTIVE line stores it, line and all; else the fixed fallback text.

        A send whose request.json an earlier process wrote says what that file holds, whatever the files say
        today: the POST it was written for may have landed, and the turn is then held against those bytes."""
        j, ep = self.journal, point["id"]
        posted, kept = j.last("FALLBACK_SENT" if send else "SENT", ep=ep, send=send), self.kept(tdir)
        if posted is not None and kept is not None and schema.sha256_text(kept["input"]) == posted.get("sha256"):
            settled = j.last("ADAPTIVE", ep=ep, send=send) if send else None
            kind = posted.get("message_kind") or ("scripted" if not send else "adaptive" if (settled or {}).get("used") else "fallback")
            return kept["input"], kind, settled, {k: posted.get(k) for k in ABOVE} if send else {}
        if not send:
            if kept is not None and kept.get("message_kind", "scripted") == "scripted":
                return kept["input"], "scripted", None, {}
            return script.message(self.habit, ep), "scripted", None, {}
        settled = j.last("ADAPTIVE", ep=ep, send=send) or await self.adapt(point, send, tdir)
        if settled is not None and settled.get("used") and isinstance(settled.get("text"), str):
            return settled["text"], "adaptive", settled, {k: settled.get(k) for k in ABOVE}
        if kept is not None and kept.get("message_kind", "fallback") == "fallback":
            return kept["input"], "fallback", settled, {k: kept.get(k) for k in ABOVE}
        return script.fallback_message(self.habit, ep), "fallback", settled, above(script.fallback_line(self.habit, ep))

    def kept(self, tdir: Path) -> dict | None:
        """A send's request.json as an earlier process left it, when it holds the text that was to be posted."""
        found = read_json(tdir / "request.json")
        return found if isinstance(found, dict) and isinstance(found.get("input"), str) else None

    async def adapt(self, point: dict, send: int, tdir: Path) -> dict | None:
        """Settle what a reply says where an adaptive answer is due, and return its ADAPTIVE line; None
        where none is due (switched off, a send before adaptive.from_send, a send already posted).

        The line is written before anything is posted, with the message (the answer under the habit's fallback
        line, as script.fallback_reply builds it) or with the reason the fixed line goes instead: the next call
        finds it and neither asks the writer again nor posts other bytes. A call of the writer that came back
        is ledgered before anything else is done with it, and nothing that goes wrong after that keeps the
        fixed line from going: the line then says `used` false and why."""
        j, ctx, ep = self.journal, self.ctx, point["id"]
        cfg = adaptive.config(ctx.profile)
        if not cfg["enabled"] or send < cfg["from_send"] or j.last("FALLBACK_SENT", ep=ep, send=send) is not None:
            return None
        had = sum(1 for e in j.entries if e["state"] == "ADAPTIVE" and e.get("used"))
        ask: adaptive.Ask | None = None
        got: adaptive.Written | None = None
        if had >= cfg["max_per_session"]:
            reason = f"the session has had {had} adaptive answer(s), all that adaptive.max_per_session allows"
        else:
            reason = None
            try:
                wrote = self.agent_wrote(ep, send)      # STIMULI.md, section 1: then nobody is asked, the guard neither
                reason = adaptive.withheld(wrote[-1], wrote[:-1]) if wrote[-1].strip() else None
                reason = reason or adaptive.unavailable(cfg, ctx.budget.prices)
                if reason is None and await self.unjournaled():
                    reason = "an earlier process may have posted this send already, with no adaptive answer on record"
                reason = reason or await self.spare()
                ask = None if reason else self.prompt_for(point, send, cfg)
            except Infra as e:
                reason = f"the session's turns could not be read ({e.reason})"
            except (OSError, ValueError, KeyError) as e:
                reason = f"the writer's prompt could not be built ({type(e).__name__}: {e})"
        if ask is not None:
            try:
                got = await asyncio.to_thread(adaptive.write, ask, cfg, ctx.budget.prices)
            except Exception as e:  # noqa: BLE001  (adaptive.write does not raise; if it ever does, the fixed line goes)
                reason = f"the writer could not be asked ({type(e).__name__}: {e})"
        if got is not None:
            reason = got.reason
            if got.called:                              # ledgered first thing: a call that came back is never left unpaid
                ctx.budget.ledger.append({"kind": "adaptive", "run": ctx.run, "session": self.key, "attempt": self.attempt, "ep": ep,
                                          "send": send, "model": got.model, "slug": got.slug, "provider": TOKENROUTER,
                                          "usage": got.usage or None, "cost_usd": got.cost_usd, "flag": got.flag,
                                          "written": got.text is not None})

        def settle(answer: str | None, why: str | None) -> tuple[dict, dict]:
            text, over = None, {}
            if answer is not None:                      # as it is posted: the habit's fallback line, a blank line, the answer
                text, over = script.fallback_reply(self.habit, ep, answer), above(script.fallback_line(self.habit, ep))
            return {"used": text is not None, "reason": why[:500] if why else None, "text": text,
                    "sha256": schema.sha256_text(text) if text else None,
                    **({"answer": answer, **over} if text is not None else {}),
                    "model": got.model if got else None, "prompt_sha": ask.prompt_sha if ask else None,
                    "template_sha": adaptive.PROMPT_SHA, "called": bool(got and got.called),
                    "cost_usd": got.cost_usd if got and got.called else None}, over

        try:
            record, over = settle(got.text if got is not None else None, reason)
        except Exception as e:  # noqa: BLE001  (whatever goes wrong with a written answer, the fixed line is sent)
            record, over = settle(None, f"the answer could not be settled ({type(e).__name__}: {e})")
        if got is not None and got.called:
            keep = {"ep": ep, "send": send, **record, "slug": got.slug, "path": got.path, "usage": got.usage or None, "flag": got.flag,
                    "reply": got.reply, "prompt": ask.prompt}
            try:
                write_json(tdir / "adaptive.json", keep)
            except ValueError:                          # what the writer wrote cannot be written: the rest is kept without it
                write_json(tdir / "adaptive.json", {**keep, "reply": None})
        line = j.write("ADAPTIVE", ep=ep, send=send, **record)
        if record["used"]:
            under = f"under the habit's fallback line (tone {over['fallback_tone']})" if over["fallback_line"] else "with no line above it"
            ctx.log(f"{self.key}: {ep}.s{send}: the agent asked again after the fixed line; an adaptive answer was written "
                    f"({got.model}, {got.cost_usd:.4f} USD by the ledger) and is sent {under}")
        else:
            ctx.log(f"{self.key}: {ep}.s{send}: no adaptive answer ({record['reason']}); the fixed fallback line is sent")
        return line

    async def unjournaled(self) -> bool:
        """After a restart, before this process has posted anything: does the session hold a turn the journal
        does not know? Then a POST was made just before the kill, and what it carried is not on record."""
        if not (self.resumed and self.sid and self.prev):
            return False
        rows = await self.http(lambda: self.ctx.client.turns(self.sid), "turns")
        return bool(rows) and rows[-1].id != self.prev

    async def spare(self) -> str | None:
        """Ask the cost guard before the writer is called (the writer is paid on TokenRouter, so the usage
        figure is read first): None, or why nothing is left to pay it with."""
        budget = self.ctx.budget
        await budget.read_usage("before_adaptive", self.key)
        over = budget.exhausted()
        if over is None:
            return None
        wallet = "" if over["wallet_usd"] is None else f", wallet figure {over['wallet_usd']:.2f} USD"
        return (f"the cost guard refuses the writer's call: the budget is used up ({over['spent_usd']:.2f} USD spent by "
                f"{over['source']} of total_usd {over['total_usd']:.2f}{wallet})")

    def prompt_for(self, point: dict, send: int, cfg: dict) -> adaptive.Ask:
        """What the writer is asked at this send: TASK.md and docs/rules.md as the runner has put them into the
        workspace up to this entry point (part B only once it is released), the user's messages so far without
        their habit lines (a reply without its fallback line), and the agent's last message. What the agent wrote
        at this entry point before that goes with it, to be read for the record (adaptive.withheld), not to be
        shown. Raises ValueError when the last message is not on disk."""
        ep, index = point["id"], self.points.index(point)
        files: dict[str, bytes] = {}
        for i, p in enumerate(self.points[:index + 1]):
            files.update(self.files_of((p.get("before") or {}).get("put")))
            if i < index:
                files.update(self.files_of((p.get("after") or {}).get("put")))
        blocks, said = script.load_blocks(), []
        for e in self.journal.entries:
            if e["state"] in ("SENT", "FALLBACK_SENT") and e["ep"] in schema.ENTRY_POINTS:
                answer = self.answer_sent(e["ep"], e["send"]) if e.get("message_kind") == "adaptive" else None
                said.append(blocks[e["ep"]] if not e["send"] else answer if answer is not None else script.fallback_message())
        wrote = self.agent_wrote(ep, send)
        if not wrote[-1].strip():
            raise ValueError(f"the agent's last message ({ep}.s{send - 1}) is not on disk")

        def text(name: str) -> str:
            return files.get(name, b"").decode("utf-8", "replace")

        return adaptive.build(cfg, ep=ep, task=text(adaptive.TASK_FILE), rules=text(adaptive.RULES_FILE), said=said,
                              agent=wrote[-1], earlier=wrote[:-1], forks=self.ctx.scenario["forks"])

    def agent_said(self, ep: str, send: int) -> str:
        """The agent's message that a reply at this send answers: what it wrote at the send before, as it wrote it.
        Empty when that reply is not on disk."""
        raw = read_json(self.turn_dir(ep, send - 1) / "response.json")
        return stops.said(schema.Response.from_dict(raw)) if isinstance(raw, dict) else ""

    def agent_wrote(self, ep: str, send: int) -> list[str]:
        """Every message the agent has written at this entry point before this send, oldest first: its reply to the
        scripted block and to each reply since. The last is the one a reply at this send answers (`agent_said`)."""
        return [self.agent_said(ep, n) for n in range(1, send + 1)]

    def answer_sent(self, ep: str, send: int) -> str | None:
        """An adaptive answer that was sent, without the fallback line above it: what the writer is shown of it.
        From the send's ADAPTIVE line (`answer`; in a journal from before 18 Nov 2026 `text`, which then had no
        line above it), else the posted text less the line request.json names. None: neither is on disk."""
        settled = self.journal.last("ADAPTIVE", ep=ep, send=send) or {}
        answer = settled.get("answer", settled.get("text"))
        if isinstance(answer, str):
            return answer
        request = read_json(self.turn_dir(ep, send) / "request.json") or {}
        sent, line = request.get("input"), request.get("fallback_line")
        if not isinstance(sent, str):
            return None
        return sent[len(line) + 2:] if isinstance(line, str) and sent.startswith(line + "\n\n") else sent

    def note_fit(self, ep: str, send: int) -> None:
        """One log line when a scripted message has gone out to an agent whose last stop it does not fit."""
        if send or ep not in fit.PREMISE:
            return
        prior = self.send_before(ep, send)
        before = read_json(self.turn_dir(*prior) / "stop.json") if prior else None
        before = before if isinstance(before, dict) else {}
        code = fit.code_for(ep, False, before)
        if code != "fits":
            cited = ", ".join(f"FORK-{n}" for n in before.get("fork_cited") or []) or "none"
            self.ctx.log(f"note: {self.key}: the scripted message of {ep} went out although it does not fit the agent's last "
                         f"stop: {code} (that stop was {before.get('stop_class')}, forks cited: {cited})")

    def next_step(self, point: dict, send: int, stop: dict) -> str:
        """Section 7's "Next send", and section 11's outcomes."""
        cls, index = stop["stop_class"], self.points.index(point)
        if not point.get("treated", True):
            if index < self.first_treated:              # the seed turn has to work, or there is no session
                return "advance" if cls in ("asked_other", "no_progress") else "infra"
            return "advance"                            # the canary is recorded as it came
        if cls == "infra":
            return "infra"
        if cls == "ended" or cls.startswith("incomplete:"):
            return "end"
        if cls in ("refused", "filter_block") or stop["reached"]:
            return "advance"
        if stop["question"] and send < self.ctx.scenario["fallback"]["max_per_ep"]:
            return "fallback"
        return "advance"

    async def post(self, point: dict, send: int, text: str, tdir: Path, kind: str = "scripted", over: dict | None = None) -> str:
        j, ctx, ep = self.journal, self.ctx, point["id"]
        state = "FALLBACK_SENT" if send else "SENT"
        sent = j.last(state, ep=ep, send=send)
        if sent is not None:
            return sent["response_id"]                  # in flight when the last process stopped: polled, not resent
        first = self.prev is None
        model = self.row["model"]
        body = HRClient.request_body(text, harness_id=self.harness_id, model=model, prev=self.prev, limits=ctx.limits)
        over = over or {}                               # a reply: `above`'s account of the fallback line over its answer
        write_json(tdir / "request.json", {**body, "message_kind": kind, **over})      # the body as posted; the rest is the runner's note
        key = self.idempotency_key(ep, send)
        response_id = None
        if not first and self.resumed and self.sid:
            response_id = await self.adopt(text)
        if response_id is None:
            await self.afford(ep, send)                 # the cost guard, between the turns of a session
        if first:                                       # no session id yet: nothing to look the POST up by
            if j.last("SENDING", ep=ep, send=send) is not None:
                self.orphan(ep, send, "the process stopped between the seed POST and its journal line")
            else:
                j.write("SENDING", ep=ep, send=send, idempotency_key=key)
        self.resumed = False
        adopted, tries = response_id is not None, 0
        while response_id is None:
            tries += 1
            try:
                queued = await self.http(lambda: ctx.client.send(text, harness_id=self.harness_id, model=model, prev=self.prev,
                                                                 limits=ctx.limits, key=key),
                                         f"POST {ep}.s{send}", post=True, followup=not first)
                response_id, self.sid = queued.id, self.sid or queued.session_id
            except Infra as e:
                if not e.unanswered:
                    raise
                if first:                               # it may have been taken: a session may now exist that nobody reads
                    self.orphan(ep, send, e.reason)
                await ctx.sleep(min(ctx.http_backoff * 2 ** (tries - 1), MAX_PAUSE))
                if not first and self.sid:              # look before the attempt is given up or the POST made again
                    response_id = await self.adopt(text)
                    adopted = response_id is not None
                if response_id is None and tries >= ctx.post_tries:
                    if e.no_reply:
                        raise Resumable(f"{e.reason}, {tries} times: the service is not reachable") from None
                    raise e from None
        j.write(state, ep=ep, send=send, response_id=response_id, session_id=self.sid, idempotency_key=key,
                sha256=schema.sha256_text(text), adopted=adopted, message_kind=kind, **over)
        self.prev = response_id
        self.note_fit(ep, send)
        return response_id

    async def afford(self, ep: str, send: int) -> None:
        """Ask the cost guard before a message is posted (module docstring, "The cost guard between turns").
        Raises Resumable when total_usd, or the wallet figure, is used up; nothing is written."""
        budget = self.ctx.budget
        if budget.payer(self.row["model"])[1] == TOKENROUTER:
            await budget.read_usage("before_post", self.key)        # what the provider says, not the ledger's estimate
        over = budget.exhausted()
        if over is None:
            return
        wallet = "" if over["wallet_usd"] is None else f", wallet figure {over['wallet_usd']:.2f} USD"
        raise Resumable(f"the budget is used up ({over['spent_usd']:.2f} USD spent by {over['source']} of total_usd "
                        f"{over['total_usd']:.2f}{wallet}): {ep}.s{send} is not posted. To let the session finish, raise "
                        "budget.total_usd in profiles.yaml (or pass the wallet as it reads now) and run the same command")

    def orphan(self, ep: str, send: int, why: str) -> None:
        """A seed POST whose reply was never had may have opened a session: ledger one seed turn for it."""
        self.ctx.log(f"{self.key}: {why}; a session with one seed turn may have been left behind, and is ledgered as one")
        self.ctx.budget.record_turn(session=self.key, attempt=self.attempt, ep=ep, send=send, response_id=None,
                                    row=self.row, usage=None, flag="orphan")

    async def adopt(self, text: str) -> str | None:
        """After a restart: the id of a turn that was posted but never journaled, if the session holds one."""
        rows = await self.http(lambda: self.ctx.client.turns(self.sid), "turns")
        last = rows[-1] if rows else None
        return last.id if last is not None and last.id != self.prev and last.user == text else None

    async def poll(self, ep: str, send: int, response_id: str, tdir: Path) -> schema.Response:
        j, ctx = self.journal, self.ctx
        if j.last("POLLING", response_id=response_id) is None:
            j.write("POLLING", ep=ep, send=send, response_id=response_id)
        sent = j.last("FALLBACK_SENT" if send else "SENT", ep=ep, send=send) or {}
        response = await self.wait(response_id, ep, send, sent.get("ts"), tdir)
        if response.status == "failed" and not stops.error_text(response.error):
            await ctx.sleep(ctx.refetch_pause)          # section 11: still failed on a re-GET after 60 s
            response = await self.wait(response_id, ep, send, sent.get("ts"), tdir)
        return response

    async def wait(self, response_id: str, ep: str, send: int, sent_ts: str | None, tdir: Path | None) -> schema.Response:
        """Poll a posted response until its turn is over (module docstring, "Patience").

        Raises Resumable when the turn is still live past its time or the service stays silent, Infra when the
        account is refused, the response is not there, or its reply cannot be read (then kept and ledgered).
        """
        ctx, what = self.ctx, f"poll {ep}.s{send}"
        left = max(ctx.turn_timeout - age(sent_ts), 0.0)        # how much longer the turn may be live
        fails, quiet, misfits, said = 0, 0.0, 0, 0.0            # failures in a row, how long they have lasted, when last said
        while True:
            seen, began = ctx.client.raw.get(POLL_ROUTE), time.monotonic()
            try:
                return await ctx.client.poll(response_id, timeout=left)
            except HRError as e:
                left = max(left - (time.monotonic() - began), 0.0)
                if ctx.client.raw.get(POLL_ROUTE) is not seen:  # the service answered in between: the count starts anew
                    fails, quiet, said = 0, 0.0, 0.0
                where = f"{what}: {'no reply' if e.status is None else f'HTTP {e.status}'}"
                if e.status in (401, 402, 403) or stops.names_account_limit(" ".join(t for t in (e.detail, e.excerpt) if t), ctx.grader):
                    raise Infra(where, str(e), stop_launching=True, kind="account") from None
                if e.status is None and (e.detail or "").startswith("gave up"):
                    raise Resumable(f"{what}: {e.detail}; the turn may still be live", live=True) from None
                if misfit(e):
                    misfits += 1
                    if misfits < MISFIT_TRIES and "not JSON" in (e.detail or ""):
                        await ctx.sleep(ctx.http_backoff)
                        continue
                    self.keep_unread(e, response_id, ep, send, tdir)
                    raise Infra(f"{what}: the reply cannot be read", str(e), kind="reply", final=True) from None
                if not transient(e):
                    raise Infra(where, str(e)) from None
                fails += 1
                pause = min(ctx.http_backoff * 2 ** (fails - 1), MAX_PAUSE)
                if left <= 0 and quiet + pause > ctx.patient:
                    raise Resumable(f"{where} for {quiet:.0f} s; the turn may still be live") from None
                if fails == 1 or quiet - said >= SAY_EVERY:
                    said = quiet
                    ctx.log(f"{self.key}: {where} from {ctx.client.base_url}; asking again for up to "
                            f"{max(left, ctx.patient - quiet):.0f} s (the turn is not posted again)")
                await ctx.sleep(pause)
                quiet, left = quiet + pause, max(left - pause, 0.0)

    def keep_unread(self, e: HRError, response_id: str, ep: str, send: int, tdir: Path | None) -> None:
        """A finished turn's reply that does not fit the contract: keep the bytes, ledger its usage if it has one."""
        usage = None
        try:
            doc = json.loads(e.body or b"")
            usage = doc.get("usage") if isinstance(doc, dict) and isinstance(doc.get("usage"), dict) else None
        except ValueError:
            pass
        if tdir is not None and e.body is not None:
            write_atomic(tdir / "response.raw.json", e.body)
        self.ctx.budget.record_turn(session=self.key, attempt=self.attempt, ep=ep, send=send, response_id=response_id,
                                    row=self.row, usage=usage)
        self.ctx.log(f"{self.key} {ep}: a reply that does not fit the contract is kept as response.raw.json ({e.detail})")

    async def pull(self, point: dict, send: int, response: schema.Response, tdir: Path) -> schema.TurnRow | None:
        j, ctx, ep = self.journal, self.ctx, point["id"]
        self.sid = self.sid or response.session_id
        if not self.sid:
            raise Infra(f"{ep}: the reply names no session")
        sid = self.sid
        write_json(tdir / "response.json", response.raw)
        rows = await self.http(lambda: ctx.client.turns(sid), "turns")
        row = next((r for r in rows if r.id == response.id), None)
        if row is not None:
            write_json(tdir / "turns_row.json", row.raw)
        known = {e["response_id"] for e in j.entries if e["state"] in ("SENT", "FALLBACK_SENT")}
        unknown = [r.id for r in rows if r.id not in known]
        if unknown:
            raise Infra(f"{ep}: the session holds a turn the journal does not know ({unknown[0]}): a POST was taken twice, "
                        "or something else wrote to the session", kind="check")
        try:
            chunks = await self.http(lambda: ctx.client.trace(sid), "trace", patience=min(ctx.patient, TRACE_PATIENCE))
        except Infra as e:                              # the trace is kept for reference only: a session is not lost for it
            if e.stop_launching:
                raise
            chunks = None
        except Resumable:                               # if the service is gone, the archive below will say so
            chunks = None
        if chunks is not None:
            count, tail = self.trace_seen
            start = count if len(chunks) >= count else 0
            if 0 < count <= len(chunks) and _sha(chunks[count - 1]) != tail:
                start = count - 1                       # the last chunk seen has grown since
            write_atomic(tdir / "trace.ndjson", b"".join(c if c.endswith(b"\n") else c + b"\n" for c in chunks[start:] if c))
            self.trace_seen = (len(chunks), _sha(chunks[-1]) if chunks else None)
        data = await self.archive(sid, ep, needed=self.points.index(point) >= self.first_treated)
        if data is not None:
            write_atomic(tdir / "archive.zip", data)
        j.write("PULLED", ep=ep, send=send, response_id=response.id, status=response.status, session_id=sid,
                trace_chunks=self.trace_seen[0], trace_tail=self.trace_seen[1])
        self.meta(session_id=sid)
        return row

    async def archive(self, sid: str, ep: str, *, needed: bool) -> bytes | None:
        """The workspace zip, asked for again while what comes back is no readable zip.

        needed: from the first treated entry point on the turn cannot be classified without it, and it can
        never be pulled later: an archive that stays unreadable is infra. Before that (the seed turn, pulled
        before any file is up) there may be nothing to zip: None, and the workspace counts as empty.
        """
        ctx = self.ctx
        for n in range(ARCHIVE_TRIES):
            try:
                data = await self.http(lambda: ctx.client.archive(sid), "archive")
            except Infra as e:
                if e.stop_launching or needed:
                    raise
                return None
            if stops.readable_zip(data):
                return data
            if n + 1 < ARCHIVE_TRIES:
                await ctx.sleep(min(ctx.http_backoff * 2 ** n, MAX_PAUSE))
        if needed:
            raise Infra(f"{ep}: the archive is not a readable zip ({len(data)} bytes), asked for {ARCHIVE_TRIES} times",
                        kind="transport")
        ctx.log(f"{self.key} {ep}: the archive is not a readable zip ({len(data)} bytes); the workspace counts as empty here")
        return None

    def check(self, facts: stops.Facts) -> Infra | None:
        """The per-turn check (plan section 4) and the reading of a failed turn's error. Sets facts.infra."""
        ctx, r, row, row_id = self.ctx, facts.response, facts.row, self.row["id"]
        failure = None
        if r.status == "failed":
            text = stops.failure_text(r, row)
            if stops.names_plan_limit(text):            # a rerun would meet the same limit after paying for the same turns
                failure = Infra(f"the account's plan or a time limit ended the turn: {text[:300]}", stop_launching=True,
                                kind="account", final=True)
            elif stops.names_account_limit(text, ctx.grader):
                failure = Infra(f"the account or its key was refused: {text[:300]}", stop_launching=True, kind="account")
            elif stops.names_rate_limit(text):
                failure = Infra(f"the provider answered 429: {text[:300]}", rate_limited=True, kind="rate")
        problems = []
        pin = ctx.pins.get(row_id)
        for name, got in (("served_model", r.served_model or (row.served_model if row else None)),
                          ("connection", row.connection if row else None)):
            if not got:
                continue                                # an empty string is skipped (Codex reports no served model)
            same = stops.same_model(got, pin[name]) if name == "served_model" else got == pin[name]
            if not pin[name]:
                if r.status != "failed":
                    ctx.pins.learn(row_id, name, got, self.key)
            elif not same:
                problems.append(f"{name} {got!r} is not the row's pin {pin[name]!r}")
        if row is not None and row.user is not None:
            facts.delivered_verbatim = row.user == facts.sent
            if not facts.delivered_verbatim:
                problems.append("the turns row's user text differs from the sent bytes")
        if failure is None and problems:
            failure = Infra("; ".join(problems), kind="check")
        if failure is not None:
            facts.infra = failure.reason
        return failure

    async def examine(self, facts: stops.Facts, point: dict, send: int, tdir: Path) -> None:
        """The workspace before and after the turn, and the probes on the pulled archive."""
        ctx, index = self.ctx, self.points.index(point)
        archive = tdir / "archive.zip"
        facts.after = stops.read_archive(archive, ctx.grader) if archive.exists() else None
        prior = self.send_before(point["id"], send)
        if prior is None:
            before = {}
        else:
            zipped = self.turn_dir(*prior) / "archive.zip"
            before = stops.read_archive(zipped, ctx.grader) if zipped.exists() else {}
            if before is None and self.index_of(prior[0]) < self.first_treated:
                # The seed turn's archive was pulled before any file was up. One that cannot be read counts as
                # an empty workspace (the uploads below make the rest), or running ahead could never be seen.
                before = {}
        if before is not None and send == 0:
            if index > 0:
                before.update(self.files_of((self.points[index - 1].get("after") or {}).get("put")))
            before.update(self.files_of((point.get("before") or {}).get("put")))
        facts.before = before
        names = stops.probes_for(point, ctx.scenario)
        if names and facts.after is not None and facts.infra is None and facts.response.status in ("completed", "incomplete"):
            facts.probes = await asyncio.to_thread(stops.run_probes, facts.after, names, mode=ctx.sandbox_mode,
                                                   tmp_root=ctx.run_dir / PROBE_TMP)

    def index_of(self, ep: str) -> int:
        return next(i for i, p in enumerate(self.points) if p["id"] == ep)

    def send_before(self, ep: str, send: int) -> tuple[str, int] | None:
        """The send pulled just before (ep, send), from the journal."""
        pulled = [(e["ep"], e["send"]) for e in self.journal.entries if e["state"] == "PULLED"]
        if (ep, send) in pulled:
            pulled = pulled[:pulled.index((ep, send))]
        return pulled[-1] if pulled else None

    # ----------------------------------------------------------------------- the service

    async def http(self, call: Callable[[], Awaitable[Any]], what: str, *, post: bool = False, followup: bool = False,
                   patience: float | None = None) -> Any:
        """One client call under section 11's reading of what went wrong (module docstring, "Patience")."""
        ctx = self.ctx
        patience = ctx.patient if patience is None else patience
        fails, misfits, waited, began, told = 0, 0, 0.0, None, 0.0
        while True:
            try:
                return await call()
            except HRError as e:
                where = f"{what}: {'no reply' if e.status is None else f'HTTP {e.status}'}"
                said = " ".join(t for t in (e.detail, e.excerpt) if t)
                if e.status in (401, 402, 403) or stops.names_account_limit(said, ctx.grader):
                    raise Infra(where, str(e), stop_launching=True, kind="account") from None
                if misfit(e):
                    if post:                            # taken, whatever the reply was: looked for like one that got none
                        raise Infra(f"{what}: the reply cannot be read", str(e), unanswered=True, kind="reply") from None
                    misfits += 1
                    if misfits < MISFIT_TRIES:
                        await ctx.sleep(ctx.http_backoff)
                        continue
                    raise Infra(f"{what}: the reply cannot be read", str(e), kind="reply") from None
                maybe_taken = post and not never_sent(e) and (no_reply(e) or (e.status is not None and e.status >= 500))
                if transient(e) and not maybe_taken:
                    began = time.monotonic() if began is None else began
                    fails += 1
                    pause = min(ctx.http_backoff * 2 ** (fails - 1), MAX_PAUSE)
                    if max(waited, time.monotonic() - began) + pause <= patience:
                        if fails == 1 or waited - told >= SAY_EVERY:        # the owner must not take a wait for a hang
                            told = waited
                            ctx.log(f"{self.key}: {where} from {ctx.client.base_url}; trying again for up to "
                                    f"{patience - max(waited, time.monotonic() - began):.0f} s")
                        await ctx.sleep(pause)
                        waited += pause
                        continue
                    if no_reply(e):
                        raise Resumable(f"{where} for {max(waited, time.monotonic() - began):.0f} s: the service is not reachable") from None
                    raise Infra(where, str(e), rate_limited=e.status == 429, kind="transport") from None
                if followup and is_rejected_followup(e):
                    raise Rejected(str(e)) from None
                raise Infra(where, str(e), unanswered=maybe_taken, no_reply=no_reply(e),
                            kind="transport" if maybe_taken else "other") from None

    # ----------------------------------------------------------------------- endings

    def canary(self, ep: str) -> dict:
        """Plan section 11, "Context guard": context_lost when the canary's reply holds neither READY nor the
        kickoff message's first sentence. No reply at all is not counted as lost (the grader's reading); the
        smoke rule asks for `replied` as well."""
        raw = read_json(self.turn_dir(ep, 0) / "response.json")
        items = trace.items_from_response(raw, ep, 0) if isinstance(raw, dict) else []
        reply = "\n".join(i["text"] or "" for i in items if i["kind"] == "message")
        kickoff = read_json(self.turn_dir(self.points[self.first_treated]["id"], 0) / "request.json") or {}
        sentence = mechanical.first_sentence(str(kickoff.get("input") or ""))
        recalled = bool(reply.strip()) and mechanical.holds_sentence(reply, sentence)
        return {"replied": bool(reply.strip()), "kickoff_recalled": recalled,
                "context_lost": bool(reply.strip()) and not recalled and "READY" not in reply}

    def treated_classes(self) -> list[tuple[str, str]]:
        treated = {p["id"] for p in self.points if p.get("treated", True)}
        return [(e["ep"], e["stop_class"]) for e in self.journal.entries if e["state"] == "CLASSIFIED" and e["ep"] in treated]

    def outcome(self) -> str:
        """As grade.json words it: the first treated turn with a terminal class sets it, else completed."""
        return next((OUTCOME_OF[cls] for _, cls in self.treated_classes() if cls in OUTCOME_OF), "completed")

    async def settle(self) -> None:
        """Before an attempt is put away or ended: wait for every response it posted and ledger it.

        A response may still be live when its attempt is given up (the account was refused mid-poll, a pull
        failed after the turn was paid for). None is left live behind a rerun: each is polled until it is
        over, or until its turn's time has run out, and ledgered with its usage. One that cannot be read is
        ledgered without usage, at the row's mean turn cost. A turn the session holds and the journal does
        not know (a POST that was taken although it got no reply) is ledgered the same way.
        Raises Resumable when the service gives no reply: the attempt is closed on a later call.
        """
        ctx = self.ctx
        if not self.journal.entries or (read_json(self.dir / META) or {}).get("settled"):
            return
        posted = {e["response_id"]: e for e in self.journal.entries if e["state"] in ("SENT", "FALLBACK_SENT")}
        if self.sid:
            try:
                rows = await self.http(lambda: ctx.client.turns(self.sid), "turns")
            except Infra:
                rows = []
            for r in rows:
                posted.setdefault(r.id, {"ep": "unjournaled", "send": 0, "ts": None})
        for response_id, sent in posted.items():
            if ctx.budget.ledger.turn(response_id) is not None:
                continue
            usage = None
            try:
                response = await self.wait(response_id, sent["ep"], sent["send"], sent.get("ts"), None)
                usage = response.usage
                tdir = self.turn_dir(sent["ep"], sent["send"]) if sent["ep"] in schema.TURN_INDEX else None
                if tdir is not None and not (tdir / "response.json").exists():
                    write_json(tdir / "response.settled.json", response.raw)
            except Resumable as e:
                if not e.live:
                    raise
                ctx.log(f"{self.key}: {response_id} is still live after its time; it is ledgered without usage")
            except Infra as e:
                ctx.log(f"{self.key}: {response_id} could not be read when the attempt was closed ({e.reason}); "
                        "it is ledgered without usage")
            ctx.budget.record_turn(session=self.key, attempt=self.attempt, ep=sent["ep"], send=sent["send"],
                                   response_id=response_id, row=self.row, usage=usage)
        self.meta(settled=True)

    def close_infra_failed(self, failure: Infra) -> None:
        """The session will not be run again by itself: ENDED, reason infra_failed. meta.json says so for the
        grader (no stop file is invented for an entry point that was never sent)."""
        self.journal.write("ENDED", reason="infra_failed", detail=failure.detail[:500], kind=failure.kind)
        self.meta(state="ENDED", outcome="infra_failed", ended_at=now(), **self.pin_meta())


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- a session, attempts and all

async def read_balance(ctx: Context) -> dict | None:
    """The HarnessRouter balance now, or None (Community Edition has none). A failed read is tried once more;
    it is not worth a session."""
    for attempt in (1, 2):
        try:
            balance = await ctx.client.balance()
            break
        except HRError as e:
            if attempt == 2:
                ctx.log(f"balance not read: {e}")
                return None
            await ctx.sleep(ctx.http_backoff)
    if balance is None:
        return None
    return {"usd": balance.balance_usd, "is_deficit": balance.is_deficit, "as_of": balance.as_of, "read_at": now()}


def check_until(ctx: Context, entry: dict, until: str) -> None:
    """A session cut short must not stand where a planned one belongs: it would be taken for the row's finished
    session, judged by the smoke rule and measured as a whole session's cost."""
    if until not in {p["id"] for p in ctx.scenario["entry_points"]}:
        raise ValueError(f"until={until!r} is no entry point of the scenario")
    planned = any(e["key"] == entry["key"] and e["stage"] != manifest.LIVE_STAGE for e in manifest.read_manifest(ctx.run_dir))
    if entry.get("stage") != manifest.LIVE_STAGE or entry.get("replicate", 0) < manifest.LIVE_REPLICATE or planned:
        raise ValueError(f"{entry['key']}: a session cut short with `until` needs a key of its own under stage "
                         f"{manifest.LIVE_STAGE!r} (rj.runner.live_entry), or a run of its own; this key is one a stage plans")


async def run_session(ctx: Context, entry: dict, row: dict, *, until: str | None = None) -> dict:
    """Run one session to DONE or ENDED, resuming from its journal; or leave it where it can be resumed.

    until: stop (DONE, marked `until`) after this entry point, with no canary and no balance pause;
    `rj row --live` passes "kickoff". The entry must come from `live_entry`.

    Returns `summarize`'s dict. Its `state` is DONE or ENDED when the session is over; INFRA_RETRY when the
    first attempt was infra and the rerun waits (launching stopped, or the cost guard refused it: see
    ctx.stopped and ctx.refused); any other state when the session was left to be resumed (the service gave
    no reply, or a turn is still live past its time: ctx.stopped says which).
    The HarnessRouter balance is read before the session and, after a pause, when it is over; both are in meta.json.
    TokenRouter's usage figure is read and ledgered (`session_done`) as the session ends, before that pause.
    Raises RunLocked when another process holds the run directory.
    """
    with ctx.lock:
        return await _run_session(ctx, entry, row, until)


async def _run_session(ctx: Context, entry: dict, row: dict, until: str | None) -> dict:
    key = entry["key"]
    sdir = session_dir(ctx.run_dir, key)
    if until is not None:
        check_until(ctx, entry, until)
    started, state, played = time.monotonic(), None, False
    while True:
        attempt = attempt_of(sdir)
        session = Session(ctx, entry, row, attempt, until)
        journal = session.journal
        if journal.state in TERMINAL:
            state = journal.state
            break
        try:
            if journal.state == "INFRA_RETRY":          # given up: wait for what it posted, then put it away
                await session.settle()
                os.replace(sdir, infra_dir(sdir, attempt))
                continue
            if not journal.entries:
                if attempt > 1 and not await before_attempt(ctx, session, sdir, attempt):
                    state = "INFRA_RETRY" if is_rerun(sdir) else None
                    break
                sdir.mkdir(parents=True, exist_ok=True)
                kept = infra_dir(sdir, attempt - 1) / META
                before = (read_json(kept) or {}).get("balance_before") if is_rerun(sdir) else await read_balance(ctx)
                session.meta(balance_before=before, started_at=now())
            played = True
            state = await session.play()
            break
        except Resumable as e:
            ctx.stop(f"{key}: {e.reason}")
            ctx.log(f"{key}: left as its journal stands, to be resumed by the next call ({e.reason})")
            break
        except Infra as e:
            if e.stop_launching:
                ctx.stop(f"{key}: {e.reason}")
            ctx.log(f"{key}: attempt {attempt} is infra ({e.reason})")
            if e.final or is_rerun(sdir):
                try:
                    await session.settle()
                except Resumable as gone:               # not closed now: the journal's last line brings it back here
                    ctx.stop(f"{key}: {gone.reason}")
                    break
                session.close_infra_failed(e)
                state = "ENDED"
                break
            journal.write("INFRA_RETRY", reason=e.reason, detail=e.detail[:500], stop_launching=e.stop_launching,
                          rate_limited=e.rate_limited, kind=e.kind)
            session.meta(state="INFRA_RETRY", ended_at=now())
    if state in TERMINAL:
        if played and until is None:                    # before the pause: a call that dies in it has left the reading in the ledger
            ctx.usage_done[key] = await ctx.budget.read_usage("session_done", key)
        await close_balance(ctx, entry, pause=0.0 if until else ctx.balance_pause if played else None,
                            started=started if played else None)
    return summarize(ctx.run_dir, entry)


async def before_attempt(ctx: Context, session: Session, sdir: Path, attempt: int) -> bool:
    """What must hold before a new attempt posts anything. False: the attempt waits for a later call.

    The attempt before it is settled if it was put away without that (`redo`, an older runner); then, for an
    infra rerun, the pause, one plain GET that the service must answer, and the cost guard's word on a full
    session with what the other sessions in flight are still expected to cost held back.
    """
    key, row = session.key, session.row
    number, kept = kept_attempts(sdir)[-1]
    await Session(ctx, session.entry, row, number, directory=kept).settle()
    if not is_rerun(sdir):
        return not ctx.stopped
    if ctx.stopped:                                     # the rerun waits for the next `rj run`
        return False
    last = Journal(kept / JOURNAL).last("INFRA_RETRY") or {}
    await ctx.sleep(ctx.rate_pause if last.get("rate_limited") else ctx.retry_pause)
    try:
        await session.http(lambda: ctx.client.integrations(), "the GET before a rerun")
    except Infra as e:
        ctx.stop(f"{key}: the service does not answer a plain GET before the rerun ({e.reason})")
    if ctx.stopped:
        return False
    verdict = ctx.budget.check([row], reserved=ctx.reserved(key), what=f"rerun of {key}")
    if not verdict["ok"]:
        ctx.refused = ctx.refused or verdict
        ctx.log(f"{key}: the rerun is not started: it needs {verdict['need_usd']:.2f} USD, {verdict['reserved_usd']:.2f} USD "
                f"is held for sessions in flight and {verdict['remaining_usd']:.2f} USD remain above the floor")
        return False
    return True


async def close_balance(ctx: Context, entry: dict, *, pause: float | None = None, started: float | None = None) -> None:
    """Read the balance after a session that is over and write it, with the fall, into its meta.json; once.

    pause: seconds to wait first (the balance can be a minute old); None: ctx.balance_pause less the time
    since the session ended, for a session that ended in an earlier process.
    """
    sdir = session_dir(ctx.run_dir, entry["key"])
    meta = read_json(sdir / META) or {}
    if "balance_after" in meta or Journal(sdir / JOURNAL).state not in TERMINAL:
        return
    if pause is None:                                   # no end on record: it ended long ago, under an older runner
        known = not meta.get("until") and _epoch(meta.get("ended_at")) is not None
        pause = max(0.0, ctx.balance_pause - age(meta.get("ended_at"))) if known else 0.0
    await ctx.sleep(pause)
    after, before = await read_balance(ctx), meta.get("balance_before")
    fall = None
    if after and before and after.get("usd") is not None and before.get("usd") is not None:
        fall = round(before["usd"] - after["usd"], 6)
    found = {"balance_after": after, "balance_fall_usd": fall}
    if started is not None:
        found["wall_seconds"] = round(time.monotonic() - started, 1)
    have = read_json(sdir / META)
    write_json(sdir / META, {**(have if isinstance(have, dict) else {}), **found})


async def settle_turn(ctx: Context, entry: dict, row: dict) -> None:
    """Bring a response that is in flight to CLASSIFIED (poll, pull, ledger) and post nothing.

    `run` does this for every session of the run directory before the cost guard is asked, so what a turn in
    flight has cost is known, and is not stranded when the guard then refuses the next message.
    """
    pending = in_flight(ctx.run_dir, entry)
    if pending is None:
        return
    sdir = session_dir(ctx.run_dir, entry["key"])
    session = Session(ctx, entry, row, attempt_of(sdir))
    try:
        await session.one_send(session.points[session.index_of(pending[0])], pending[1])
    except Infra:
        pass                                            # in the CLASSIFIED line: the attempt is closed when the session next runs
    except Resumable as e:
        ctx.stop(f"{entry['key']}: {e.reason}")


def note_error(ctx: Context, entry: dict, error: BaseException) -> str:
    """Record an exception nobody planned for in the session's journal (when it has one) and the log."""
    text = f"{type(error).__name__}: {error}"
    journal = Journal(session_dir(ctx.run_dir, entry["key"]) / JOURNAL)
    if journal.entries:
        journal.write("ERROR", error=text[:500])
    ctx.log(f"{entry['key']}: stopped by an error that is not the service's ({text}); the other sessions go on")
    return text


def summarize(run_dir: str | Path, entry: dict) -> dict:
    """What a session's directory says about it, whether it ran in this process or an earlier one.

    `state`: PLANNED (nothing yet), a journal state, or INFRA_RETRY (an attempt was put away and the next has
    not begun). `until`: the entry point a live session was cut at (such a session is DONE, and is never a
    row's finished session). `failures`: per infra attempt {"attempt", "kind", "reason"}. `minutes`: the
    agent's time, summed over the turns; `wall_minutes`: from the first POST of the attempt in the session's
    directory to its last pulled turn, by the journal's clock. `cut_reason`: the reason string of the turn
    that was cut (`incomplete`), as the service gave it. `fallbacks`: the replies to unplanned questions sent; `adaptive`: how
    many of them were adaptive answers.
    """
    sdir = session_dir(run_dir, entry["key"])
    kept = kept_attempts(sdir)
    journal = Journal(sdir / JOURNAL)
    state = journal.state or ("INFRA_RETRY" if kept else "PLANNED")
    classes: dict[str, list] = {}
    probes: dict[str, dict] = {}
    cost, seconds, costed, cut, written = 0.0, 0.0, False, None, 0
    for ep, send, d in trace.send_dirs(sdir):
        stop = read_json(d / "stop.json")
        if not isinstance(stop, dict):
            continue
        classes.setdefault(ep, []).append(stop.get("stop_class"))
        written += stop.get("message_kind") == "adaptive"
        probes[ep] = stop.get("probes") or {}
        if str(stop.get("stop_class")).startswith("incomplete:"):
            cut = stop.get("reason")
        cost += stop.get("cost_usd") or 0.0
        costed = costed or stop.get("cost_usd") is not None
        seconds += stop.get("seconds") or 0.0
    done, ended, canary = journal.last("DONE"), journal.last("ENDED"), journal.last("CANARY") or {}
    meta = read_json(sdir / META) or {}
    failures = []
    for number, d in kept:
        line = Journal(d / JOURNAL).last("INFRA_RETRY")
        if line:
            failures.append({"attempt": number, "kind": line.get("kind") or "other", "reason": line.get("reason")})
    if ended and ended.get("reason") == "infra_failed":
        failures.append({"attempt": attempt_of(sdir), "kind": ended.get("kind") or "other", "reason": ended.get("detail")})
    error = journal.last("ERROR") if journal.entries and journal.entries[-1]["state"] == "ERROR" else None
    posted = [_epoch(e.get("ts")) for e in journal.entries if e["state"] in ("SENT", "FALLBACK_SENT")]
    closed = [_epoch(e.get("ts")) for e in journal.entries if e["state"] in ("PULLED", "CLASSIFIED", *TERMINAL)]
    wall = None
    if posted and closed and posted[0] is not None and closed[-1] is not None:
        wall = round(max(0.0, closed[-1] - posted[0]) / 60, 3)
    return {"key": entry["key"], "row": entry["row"], "habit": entry["habit"], "stage": entry["stage"],
            "replicate": entry["replicate"], "state": state,
            "outcome": done["outcome"] if done else ended["reason"] if ended else None,
            "until": (done or {}).get("until") or (journal.last("NEW") or {}).get("until"),
            "attempts": len(kept) + (1 if journal.entries else 0),
            "stop_classes": classes, "probes": probes,
            "fallbacks": sum(len(v) - 1 for v in classes.values()),
            "adaptive": written,
            "context_lost": canary.get("context_lost"), "kickoff_recalled": canary.get("kickoff_recalled"),
            "canary_replied": canary.get("replied"), "failures": failures, "error": error["error"] if error else None,
            "cost_usd": round(cost, 6) if costed else None, "minutes": round(seconds / 60, 3),
            "wall_minutes": wall, "cut_reason": cut,
            "balance_fall_usd": meta.get("balance_fall_usd"), "dir": str(sdir)}
