"""`rj run`: plan the sessions of a stage selection and run them under the cost guard, the in-flight limits
and the smoke rule. Plan section 3 ("Smoke rule", "Replacing a row"), section 11 and section 14 ("The rule").

    summary = await run("demo", through="baseline", smoke_only=True)

One runner per run directory: `run` holds an exclusive lock on <runs>/<run>/.lock for its whole life and
raises RunLocked when another process has it. A relative runs directory is under the repository root.

Running it again resumes: sessions that are DONE or ENDED are left alone, a session in flight is taken up from
its journal, and a session whose first attempt was infra while launching was stopped gets its second attempt.
That holds for a run directory planned under today's files. Where it holds a selected key under other hashes
(the scenario, the blocks, the texts, the seed, the workspace or the grader changed since that key was planned)
`run` refuses before it writes or sends anything (manifest.OtherHashes): a finished session of another wording
would stand as this run's, and an unfinished one would be sent today's texts under yesterday's record and
stage. `accept_other_hashes` goes on all the same, with one warning line per key; the planned run gets a run
name of its own instead.
Before the cost guard is asked anything, every response that an earlier call left in flight (in any session
of the run directory, selected or not) is polled, pulled and ledgered; nothing is posted for that.

Stages run one after the other, in the profile's order. Within a stage sessions start in manifest order, at
most in_flight.per_row per row and in_flight.total overall (2 at most where <runs>/preflight.json records the
account's two-task limit), and at most in_flight.tokenrouter of them paid on TokenRouter where the profile sets
it (the key's rate limit: on 18 Nov 2026 three such sessions started together and two failed on a 429 within a
minute). A row's first neutral session (N r01) runs alone in its row; nothing else of the row
starts before it has passed the smoke rule, and its measured cost and minutes are written to
<runs>/measured.json before any further session of the row starts. Where the row is paid on TokenRouter that
session also runs alone on TokenRouter (no other session paid there runs beside it, in any row), so that
the rise of the usage figure across it is its cost: two such sessions side by side would both fall back to
the ledger's estimate, which overstated Codex nearly threefold on 17 Nov 2026. The figure is read and ledgered
when the session starts (`session_start`), as it ends (`session_done`) and again after the balance pause
(`session_end`). A call that dies in that pause has judged and measured nothing; the next call measures the
row from the ledgered readings (`_Run.from_ledger`), if they show one start, an end, and no line paid on
TokenRouter by anything else between them.

The account's 30-minute task limit (plan checks 7 and 11; whether it covers a turn or a session is open until
`rj preflight --steps ...,late` has run). A session that was cut or ended with 27 to 35 minutes on its clock,
or lost its conversation after more than 30, is flagged as possibly the account's limit; for a row's first
neutral session launching then stops and no step-up is proposed. Not flagged once preflight.json says
`account.task_is_session: false`.

The smoke rule (section 3): the row passes when part_a_green is true on its fork2_reply archive and the
canary turn replied and showed the conversation was kept. A row that fails is held back and its step-up is
proposed, not done. No replacement is proposed when the canary gave no reply (that is no finding about the
model). A first neutral session that ended infra_failed holds its row for the call and leaves no verdict, so
`redo` can run it again; when both its attempts failed on a provider error that names neither the account
nor a rate limit, the step-up is proposed "if this is a model reason".

Adaptive answers (rj/runner/adaptive.py, machine.py's "The ladder"): the summary's `adaptive` gives this run's
writer calls by the ledger (`calls`) and what they cost, and how many adaptive answers were sent (`used`), read
from the journals of the run's sessions, not from the ledger: a call whose answer was never posted (the process
died before its ADAPTIVE line, and the writer was asked again) is a call and no answer. The cost is part of
`budget.spent_usd` like every other ledger line paid on TokenRouter.

A probe unpacks a pulled archive under <runs>/<run>/.probe-tmp and removes it again. What a killed runner left
there is removed when the next `run` has the run directory's lock.

Files this module writes: <runs>/<run>/smoke.json ({row: verdict}) and <runs>/agent_work.json (is agent work
billed: read at the start of every call); everything else is written by manifest.py (manifest.jsonl,
matrix.yaml, manifest.json), machine.py (the session directories, pins.json, .lock) and budget.py
(<runs>/ledger.jsonl, <runs>/measured.json).

The summary's `exit_code`: 0 every selected session is DONE or ENDED and no row is held; 1 a row is held back,
a session ended infra_failed, a session was left unfinished or stopped on an error of the runner's own; 2
launching stopped (the cost guard refused a stage, a session start, a rerun or a session's next message, a
row has no price, the account ran out of credit, met a limit of its plan or had its key refused, the service
gave no reply or a turn is still live past its time, or agent work is billed and was not accepted), or what
remains above the floor is below zero when the call ends (`over_budget`).
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from rj import schema, script
from rj.hr import HRClient
from rj.runner import adaptive, machine, manifest
from rj.runner.budget import TOKENROUTER, Budget, _usd, is_loopback, no_tr_usage, now, same_service, tr_base
from rj.runner.manifest import read_json, write_json

SMOKE, AGENT_WORK = "smoke.json", "agent_work.json"
SMOKE_EP, SMOKE_PROBE = "fork2_reply", "part_a_green"       # section 3's smoke rule
AGENT_WORK_SESSION_USD = 1.04       # section 14: one session of agent work with reruns, if it is billed
BALANCE_EPS_USD = 0.005
BILLED = ("billed", "granted")      # verdicts under which the HarnessRouter balance falls as sessions run
MODEL_REASONS = ("completed", "refused", "filter_block", "capped")      # outcomes that can step a row up
KNOWN_CUTS = (None, "max_steps", "timeout")     # the reasons of a cut turn that rj sets itself (the profile's caps)
TASK_LIMIT_MINUTES = 30.0                       # the service plan's "longest single task" (plan section 2)
NEAR_TASK_LIMIT = (27.0, 35.0)                  # "ended near 30 minutes" (plan check 11), in minutes
OVER_EPS_USD = 0.005
PAUSES = {"balance_pause": 90.0, "rate_pause": 60.0, "retry_pause": 30.0, "refetch_pause": 60.0}
STUB_BASE = "stub"                  # rows of matrix.stub.yaml: no pause is worth waiting for
STUB_POLL_SECONDS = 0.05


def smoke_key(row_id: str) -> str:
    """A row's first neutral session."""
    return schema.session_key(row_id, manifest.NEUTRAL, 1, manifest.VARIANT, manifest.WORDING)


def smoke_verdict(result: dict) -> dict:
    """Section 3: the row passes when part_a_green is true on its fork2_reply archive, the canary replied and
    context_lost is false. `reason` says why a row did not pass."""
    classes = result["stop_classes"]
    green = (result["probes"].get(SMOKE_EP) or {}).get(SMOKE_PROBE) is True
    done = result["state"] == "DONE"
    kept = done and result["canary_replied"] is True and result["context_lost"] is False
    first = next(iter(schema.ENTRY_POINTS))
    reason = None
    if not (green and kept):
        parts = [] if green else [f"{SMOKE_PROBE} is not true on its {SMOKE_EP} archive"]
        if done and result["canary_replied"] is not True:
            parts.append("the canary showed nothing (its turn gave no reply)")
        elif done and result["context_lost"] is not False:
            parts.append("the canary shows the conversation was not kept")
        parts.append(f"outcome {result['outcome']}")
        reason = ", ".join(parts)
    return {"session": result["key"], "passed": bool(green and kept), "part_a_green": green,
            "context_lost": result["context_lost"], "canary_replied": result["canary_replied"],
            "does_not_stop": "ran_ahead" in classes.get(first, []) and "asked_fork" not in classes.get("plan_reply", []),
            "state": result["state"], "outcome": result["outcome"], "reason": reason, "at": now()}


async def run(profile: str | dict, *, run: str | None = None, through: str | None = None, only: str | None = None,
              rows: list[str] | str | None = None, smoke_only: bool = False, runs_dir: str | Path = "runs",
              client: HRClient | None = None, prices: dict | str | Path | None = None,
              tr_wallet_usd: float | None = None, accept_agent_work: bool = False, require_smoke: bool = True,
              accept_other_hashes: bool = False,
              sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, balance_pause: float | None = None,
              rate_pause: float | None = None, retry_pause: float | None = None, refetch_pause: float | None = None,
              poll_timeout: float | None = None, patience: float | None = None,
              http_backoff: float = 2.0, poll_seconds: float | None = None,
              tr_usage: Callable[[], Awaitable[float | None]] | None = None,
              sandbox_mode: str | None = None, log: Callable[[str], None] = print) -> dict:
    """Run the sessions of a stage selection and return a summary (module docstring).

    profile        a profile name of profiles.yaml, or a loaded profile (with "name")
    run            names <runs_dir>/<run>/; default: the profile's name
    through, only  the stages: all up to and including one, or one alone; neither: every stage
    rows           only these rows (a list or "a,b"); ids as a stage lists them or as replacements resolve them
    smoke_only     run only each row's first neutral session
    runs_dir       a relative path is taken under the repository root
    client         an HRClient; made from HR_BASE_URL and HR_API_KEY (and closed again) when not given, polling
                   every `poll_seconds` (default 2.5, or 0.05 when every selected row is served by the stub)
    prices         a loaded prices dict or a path; default prices.yaml
    tr_wallet_usd  the TokenRouter wallet as the owner reads it now; caps what remains
    accept_agent_work  go on although the HarnessRouter balance falls as sessions run
    require_smoke  hold back a row that has no smoke verdict and whose first neutral session is not in this selection
    accept_other_hashes  go on although the run directory records selected keys under other hashes than today's
                   files (module docstring); left False, such a selection is refused with manifest.OtherHashes
    sleep          every wait of the runner itself goes through it (the client keeps its own poll sleep)
    balance_pause  seconds before the balance is read again after a session; rate_pause: before the new attempt
                   after a provider 429; retry_pause: before the new attempt after any other infra;
                   refetch_pause: before a turn that failed with no error is asked for again. Left None they are
                   90, 60, 30 and 60, or 0 when every selected row is served by the stub.
    poll_timeout   how long one turn may be live; default the cap's timeout_seconds plus 300
    patience       how long no reply, 5xx, 409 and 429 are waited out on one call; default: poll_timeout
    tr_usage       reads TokenRouter's usage figure in cents; default GET {TR_BASE_URL}/dashboard/billing/usage
                   (never the real route when every selected row is served by the stub)

    Raises RunLocked when another process is running this run directory, manifest.OtherHashes (a ValueError) for
    a selection recorded under other hashes, before anything is written, and ValueError when every selected row
    is a stub row but the base URL is not this machine (the offline profile must not reach the real service).
    """
    cfg = schema.load_profile(profile) if isinstance(profile, str) else schema.validate("PROFILE", dict(profile))
    name = cfg.get("name") or "custom"
    run_name = run or name
    runs = manifest.resolve_runs(runs_dir)
    run_dir = runs / run_name
    if isinstance(rows, str):
        rows = [r.strip() for r in rows.split(",") if r.strip()]
    book = prices if isinstance(prices, dict) else schema.load_prices(prices or "prices.yaml")

    entries = manifest.plan(cfg, through=through, only=only, rows=rows, replacements=manifest.load_replacements(runs))
    if not accept_other_hashes:                         # before anything is written: a run of another wording is not continued
        check_hashes(run_dir, entries)
    lock = machine.RunLock(run_dir)
    with lock:
        shutil.rmtree(run_dir / machine.PROBE_TMP, ignore_errors=True)      # what a killed runner's probe left unpacked
        written = manifest.write(run_dir, entries, manifest.rows_of(entries, schema.load_matrix(cfg["matrix"])))
        sessions, row_of = written["sessions"], written["rows"]
        over = {s["key"] for s in sessions if machine.summarize(run_dir, s)["state"] in machine.TERMINAL} if written["changed"] else set()
        warnings = [f"{key}: recorded under other hashes than today's files (the scenario, texts, seed or grader changed) and taken "
                    "all the same (accept_other_hashes): "
                    + ("it is over and stands as this run's" if key in over else "it is sent today's texts under its recorded hashes and stage")
                    for key in written["changed"]]
        warnings += [f"row {r}: the matrix line differs from the one this run recorded; the run's record is used"
                     for r in written["rows_changed"]]
        warnings += [f"{s['key']}: this key holds a live (kickoff-only) session; the stage's session is not run"
                     for s in sessions if s["stage"] == manifest.LIVE_STAGE]
        if not sessions:
            warnings.append("the selection holds no session")
        for line in warnings:
            log(f"warning: {line}")

        stub = bool(row_of) and all(r.get("base") == STUB_BASE for r in row_of.values())
        base_url = client.base_url if client is not None else os.environ.get("HR_BASE_URL")
        if stub and base_url and not is_loopback(base_url):
            raise ValueError(f"every row of this selection is served by the stub, but the base URL ({base_url}) is not this "
                             "machine: start the stub and point HR_BASE_URL at it")
        if stub and tr_usage is None and not is_loopback(tr_base()):
            log("TR_BASE_URL is not this machine: a stub run does not read the usage figure there; the ledger is used")
            tr_usage = no_tr_usage
        pauses = {"balance_pause": balance_pause, "rate_pause": rate_pause, "retry_pause": retry_pause,
                  "refetch_pause": refetch_pause}
        pauses = {k: (0.0 if stub else PAUSES[k]) if v is None else v for k, v in pauses.items()}
        writer = adaptive.config(cfg)
        if writer["enabled"] and adaptive.unavailable(writer, book):       # said once, before a session finds out mid-way
            log(f"note: adaptive answers are on in the profile, but the writer cannot be asked ({adaptive.unavailable(writer, book)}): "
                "an agent that asks again after the fixed fallback line gets that line again")
        own_client = client is None
        if client is None:
            client = HRClient(edition=cfg["access"]["edition"],
                              poll_seconds=poll_seconds if poll_seconds is not None else STUB_POLL_SECONDS if stub else 2.5)
        try:
            ctx = await open_context(cfg, run=run_name, runs_dir=runs, client=client, prices=book, tr_wallet_usd=tr_wallet_usd,
                                     sleep=sleep, poll_timeout=poll_timeout, patience=patience, http_backoff=http_backoff,
                                     tr_usage=tr_usage, sandbox_mode=sandbox_mode, log=log, lock=lock, **pauses)
            runner = _Run(ctx, cfg, sessions, row_of, smoke_only=smoke_only, require_smoke=require_smoke,
                          accept_agent_work=accept_agent_work)
            await runner.go()
        finally:
            if own_client:
                await client.aclose()
        return runner.summary(name, warnings)


def check_hashes(run_dir: str | Path, entries: list[dict]) -> None:
    """Raise manifest.OtherHashes when a plan holds keys that the run directory records under other hashes than
    today's files (module docstring), saying how many of those sessions are over. Reads only."""
    stale = manifest.changed(run_dir, entries)
    if stale:
        manifest.refuse_changed(run_dir, entries, over=sum(1 for e in stale if machine.summarize(run_dir, e)["state"] in machine.TERMINAL))


async def open_context(cfg: dict, *, run: str, runs_dir: str | Path, client: HRClient, prices: dict | None = None,
                       tr_wallet_usd: float | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                       balance_pause: float = 90.0, rate_pause: float = 60.0, retry_pause: float = 30.0,
                       refetch_pause: float = 60.0, poll_timeout: float | None = None, patience: float | None = None,
                       http_backoff: float = 2.0,
                       tr_usage: Callable[[], Awaitable[float | None]] | None = None, sandbox_mode: str | None = None,
                       log: Callable[[str], None] = print, lock: machine.RunLock | None = None) -> machine.Context:
    """What a session needs from its run: the ledger and guard (payers and the usage figure read), the pins.

    `run` uses it; so can a caller that runs one session itself with machine.run_session, as `rj row --live`
    does (with an entry from `live_entry`; run_session then takes the run directory's lock itself).
    """
    runs = manifest.resolve_runs(runs_dir)
    budget = Budget(cfg["budget"], prices if prices is not None else schema.load_prices(), runs, run=run,
                    wallet_usd=tr_wallet_usd, tr_usage=tr_usage, log=log, judge=cfg.get("judge"))
    await budget.start(client)
    return machine.Context(client=client, run=run, run_dir=runs / run, runs_dir=runs, profile=cfg,
                           scenario=script.load_scenario(), grader=schema.load_grader(), budget=budget,
                           pins=machine.Pins(runs / run, runs, budget.service), sleep=sleep, balance_pause=balance_pause,
                           rate_pause=rate_pause, retry_pause=retry_pause, refetch_pause=refetch_pause,
                           poll_timeout=poll_timeout, patience=patience, http_backoff=http_backoff,
                           sandbox_mode=sandbox_mode, log=log, lock=lock)


def redo(key: str, *, run: str, runs_dir: str | Path = "runs", log: Callable[[str], None] = print) -> dict:
    """Give a session another life: `rj run --redo KEY`, for the owner to call, never the runner.

    What the session has left is kept as <key>.infraN (N: its attempt number) and marked REDO, so the next
    `run` starts it afresh under the next attempt number (its Idempotency-Keys are new, and it has its one
    infra rerun again). When the session is its row's first neutral one, the row's smoke verdict is cleared,
    so the row is judged again. A turn that was in flight is waited for and ledgered before the new attempt posts.
    Returns {"key", "kept": the directory or None, "next_attempt", "smoke_cleared"}.
    """
    run_dir = manifest.resolve_runs(runs_dir) / run
    with machine.RunLock(run_dir):
        entry = next((e for e in manifest.read_manifest(run_dir) if e["key"] == key), None)
        if entry is None:
            raise ValueError(f"{run_dir}: no session {key!r} in manifest.jsonl")
        sdir = manifest.session_dir(run_dir, key)
        journal, kept = machine.Journal(sdir / machine.JOURNAL), None
        if journal.entries:
            kept = machine.infra_dir(sdir, machine.attempt_of(sdir))
            journal.write("REDO", was=journal.state)
            os.replace(sdir, kept)
        elif machine.kept_attempts(sdir):               # its rerun was waiting: the next attempt is a first one again
            last = machine.Journal(machine.kept_attempts(sdir)[-1][1] / machine.JOURNAL)
            if last.state != "REDO":
                last.write("REDO", was=last.state)
        smoke = read_json(run_dir / SMOKE)
        cleared = isinstance(smoke, dict) and key == smoke_key(entry["row"]) and entry["row"] in smoke
        if cleared:
            smoke.pop(entry["row"])
            write_json(run_dir / SMOKE, smoke)
        log(f"{key}: put away" + (f" as {kept.name}" if kept else "") + f"; the next run starts attempt {machine.attempt_of(sdir)}"
            + ("; the row's smoke verdict is cleared" if cleared else ""))
        return {"key": key, "kept": str(kept) if kept else None, "next_attempt": machine.attempt_of(sdir),
                "smoke_cleared": bool(cleared)}


class _Run:
    def __init__(self, ctx: machine.Context, cfg: dict, sessions: list[dict], row_of: dict, *, smoke_only: bool,
                 require_smoke: bool, accept_agent_work: bool):
        self.ctx, self.cfg, self.sessions, self.row_of = ctx, cfg, sessions, row_of
        self.smoke_only, self.require_smoke, self.accept_agent_work = smoke_only, require_smoke, accept_agent_work
        self.budget: Budget = ctx.budget
        self.log = ctx.log
        found = read_json(ctx.run_dir / SMOKE)
        self.smoke: dict[str, dict] = found if isinstance(found, dict) else {}
        self.held: dict[str, str] = {}
        self.proposals: list[str] = []
        self.results: dict[str, dict] = {}
        self.ran: list[str] = []
        self.skipped: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self.stages: list[dict] = []
        self.running: dict[str, asyncio.Task] = {}
        self.tr_running: set[str] = set()
        self.tr_measuring: set[str] = set()     # first neutral sessions in flight that are paid on TokenRouter
        self.alone: dict[str, bool] = {}
        self.limits = dict(cfg["in_flight"])
        service = self.budget.service           # what a stub left in these files is not this account's
        preflight = read_json(ctx.runs_dir / machine.PREFLIGHT)
        account = (preflight.get("account") or {}) if isinstance(preflight, dict) and same_service(preflight, service) else {}
        if account.get("two_task_limit") is True and self.limits["total"] > 2:      # check 10: two tasks at a time
            self.log(f"in_flight.total is {self.limits['total']}, but preflight.json found the account's two-task limit: 2 is used")
            self.limits["total"] = 2
        self.task_is_session = account.get("task_is_session")       # check 7's late recall: None until it has run
        own = read_json(ctx.runs_dir / AGENT_WORK)
        own = own if isinstance(own, dict) and same_service(own, service) else {}
        self.agent_work = own.get("verdict") or account.get("agent_work") or "cannot_tell"
        ctx.reserved = self.reserved

    @property
    def refused(self) -> dict | None:
        return self.ctx.refused

    @refused.setter
    def refused(self, verdict: dict | None) -> None:
        self.ctx.refused = verdict

    # ----------------------------------------------------------------------- the stages

    async def go(self) -> None:
        await self.settle()
        await self.balances()
        self.catch_up()
        for stage in dict.fromkeys(s["stage"] for s in self.sessions):
            if self.ctx.stopped or self.refused:
                break
            await self.stage(stage, [s for s in self.sessions if s["stage"] == stage])
        for s in self.sessions:
            if s["key"] not in self.results:
                self.results[s["key"]] = machine.summarize(self.ctx.run_dir, s)
                if self.results[s["key"]]["state"] not in machine.TERMINAL:
                    self.skipped.setdefault(s["key"], "not started: " + (self.ctx.stopped or "the cost guard refused"
                                                                         if self.ctx.stopped or self.refused else "not reached"))

    async def settle(self) -> None:
        """Every response an earlier call left in flight is polled, pulled and ledgered before the guard is asked."""
        rows = {**{r["id"]: r for r in manifest.read_matrix(self.ctx.run_dir)}, **self.row_of}
        jobs = [machine.settle_turn(self.ctx, e, rows[e["row"]]) for e in manifest.read_manifest(self.ctx.run_dir)
                if e["row"] in rows and machine.in_flight(self.ctx.run_dir, e)]
        if not jobs:
            return
        self.log(f"{len(jobs)} turn(s) left in flight by an earlier call: polled, pulled and ledgered before the guard is asked")
        for found in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(found, BaseException) and not isinstance(found, Exception):
                raise found
            if isinstance(found, Exception):
                self.log(f"a turn in flight could not be settled ({type(found).__name__}: {found}); its session will say so")

    async def balances(self) -> None:
        """The balance after a session that ended in a call which did not live to read it."""
        await asyncio.gather(*(machine.close_balance(self.ctx, s) for s in self.sessions))

    def catch_up(self) -> None:
        """Verdicts for first neutral sessions that ended in an earlier process which did not live to judge them.
        Such a session is measured from the usage figure as that process ledgered it (`from_ledger`)."""
        recorded = {e["key"]: e for e in manifest.read_manifest(self.ctx.run_dir)}
        for row_id in dict.fromkeys(s["row"] for s in self.sessions):
            entry = recorded.get(smoke_key(row_id))
            if row_id in self.smoke or entry is None or entry["stage"] == manifest.LIVE_STAGE:
                continue
            result = machine.summarize(self.ctx.run_dir, entry)
            if result["state"] in machine.TERMINAL and not result["until"]:
                self.judge({**result, **self.from_ledger(result["key"])})
        for row_id, verdict in self.smoke.items():
            if not verdict.get("passed") and row_id in self.row_of:
                self.held.setdefault(row_id, f"its first neutral session {verdict.get('session')} did not pass the smoke rule")

    async def stage(self, stage: str, planned: list[dict]) -> None:
        ctx = self.ctx
        todo = []
        for s in planned:
            found = machine.summarize(ctx.run_dir, s)
            if s["stage"] == manifest.LIVE_STAGE or found["until"]:
                self.skipped[s["key"]] = "its key holds a live (kickoff-only) session: not run, not judged"
            elif found["state"] in machine.TERMINAL:
                self.skipped[s["key"]] = "finished before this call"
            elif self.smoke_only and s["key"] != smoke_key(s["row"]):
                self.skipped[s["key"]] = "smoke-only: not a row's first neutral session"
            else:
                todo.append(s)
        report = {"stage": stage, "planned": len(planned), "to_run": len(todo), "launched": 0, "guard": None}
        self.stages.append(report)
        fit = [s for s in todo if not self.blocked(s, todo)]
        report["guard"] = verdict = self.budget.check([self.row_of[s["row"]] for s in fit], need=sum(self.need(s) for s in fit),
                                                      what=f"stage {stage}")
        self.log(f"stage {stage}: {len(fit)} of {len(planned)} sessions to run, projected {verdict['need_usd']:.2f} USD, "
                 f"{verdict['remaining_usd']:.2f} USD remain")
        if fit and not verdict["ok"]:
            self.refused = verdict
            if verdict.get("unpriced"):
                self.log(f"stage {stage} is not started: prices.yaml has no entry for {', '.join(verdict['unpriced'])}; "
                         "its turns could not be costed")
            else:
                self.log(f"stage {stage} is not started: its unfinished sessions need {verdict['need_usd']:.2f} USD "
                         f"and {verdict['remaining_usd']:.2f} USD remain above the floor")
            return
        report["launched"] = await self.schedule(todo)

    def from_ledger(self, key: str) -> dict:
        """What the ledger holds of TokenRouter's usage figure around a session that ended in an earlier call:
        {"tr_cents": [at its start, at its end], "whole": True, "alone": bool}, or {} when it does not hold both.

        The start is the session's one `session_start` reading (two: it was resumed, and the first reading is
        not followed by a whole session in one piece of the ledger this can vouch for); the end is its last
        `session_done` or `session_end` reading. `alone`: no line paid on TokenRouter by anything else, and no
        other session's start, lies between the two."""
        entries = self.budget.ledger.mine()
        mine = [i for i, e in enumerate(entries) if e.get("kind") == "tr_usage" and e.get("run") == self.ctx.run and e.get("session") == key
                and e.get("base") == self.budget.tr_base and isinstance(e.get("cents"), (int, float))]
        starts = [i for i in mine if entries[i].get("where") == "session_start"]
        ends = [i for i in mine if entries[i].get("where") in ("session_done", "session_end")]
        if len(starts) != 1 or not ends or ends[-1] < starts[0]:
            return {}
        between = entries[starts[0] + 1:ends[-1]]
        beside = any(e.get("session") != key and (e.get("provider") == TOKENROUTER or e.get("where") == "session_start") for e in between)
        return {"tr_cents": [entries[starts[0]]["cents"], entries[ends[-1]]["cents"]], "whole": True, "alone": not beside}

    def need(self, s: dict) -> float:
        """What a session is still expected to cost: its projection, less what its running attempt has cost
        already when it has started (that part is in what has been spent)."""
        row, sdir = self.row_of[s["row"]], manifest.session_dir(self.ctx.run_dir, s["key"])
        if machine.Journal(sdir / machine.JOURNAL).entries:
            return self.budget.outstanding(row, s["key"], machine.attempt_of(sdir))
        return self.budget.projection(row)

    def reserved(self, but: str | None = None) -> float:
        """What the sessions in flight (other than `but`) are still expected to cost."""
        return sum(self.need(s) for s in self.sessions if s["key"] in self.running and s["key"] != but)

    def blocked(self, s: dict, todo: list[dict]) -> bool:
        """True when a row is held back, or (require_smoke) has neither a verdict nor its first neutral session here."""
        row_id = s["row"]
        if row_id in self.held:
            return True
        if row_id in self.smoke or not self.require_smoke:
            return False
        return smoke_key(row_id) not in {t["key"] for t in todo}

    def on_tokenrouter(self, row_id: str) -> bool:
        return self.budget.payer(self.row_of[row_id]["model"])[1] == TOKENROUTER

    def measuring(self, s: dict) -> bool:
        """True for a row's first neutral session that is still to be judged and is paid on TokenRouter: the
        one session whose cost the usage figure can measure, if nothing else paid there runs beside it."""
        row_id = s["row"]
        return (s["key"] == smoke_key(row_id) and row_id not in self.smoke and row_id not in self.held
                and self.on_tokenrouter(row_id))

    async def account_allows(self) -> bool:
        """Section 11, "Agent work": once it is known to be billed, a launch needs the owner's word and a
        HarnessRouter balance above hr_floor_usd plus one session."""
        if self.agent_work not in BILLED:
            return True
        if not self.accept_agent_work:
            self.ctx.stop("agent work is billed (or drawn from granted credit): the HarnessRouter balance falls as sessions "
                          "run; pass --accept-agent-work (accept_agent_work) to go on")
            return False
        balance = await machine.read_balance(self.ctx)
        floor = self.cfg["budget"]["hr_floor_usd"] + AGENT_WORK_SESSION_USD
        if balance and balance.get("usd") is not None and balance["usd"] < floor:
            self.ctx.stop(f"the HarnessRouter balance ({balance['usd']:.2f} USD) is under hr_floor_usd plus one session ({floor:.2f} USD)")
            return False
        return True

    async def schedule(self, todo: list[dict]) -> int:
        ctx, limits, running = self.ctx, self.limits, self.running
        pending, entry_of, launched = list(todo), {}, 0
        try:
            while pending or running:
                i = 0
                while i < len(pending) and not ctx.stopped and not self.refused and len(running) < limits["total"]:
                    s = pending[i]
                    row_id, first = s["row"], smoke_key(s["row"])
                    in_row = sum(1 for k in running if entry_of[k]["row"] == row_id)
                    waiting = {p["key"] for p in pending} | set(running)
                    if row_id in self.held:
                        self.skipped[s["key"]] = f"row held back: {self.held[row_id]}"
                        pending.pop(i)
                        continue
                    if row_id not in self.smoke and s["key"] != first:
                        if first in waiting:            # the row's first neutral session runs alone in its row
                            i += 1
                            continue
                        if self.require_smoke:
                            self.held[row_id] = f"its first neutral session {first} has not run: run the stage that holds it first"
                            self.log(f"row {row_id} is held back: {self.held[row_id]}")
                            continue
                    if in_row >= (1 if s["key"] == first and row_id not in self.smoke else limits["per_row"]):
                        i += 1
                        continue
                    on_tr, measuring = self.on_tokenrouter(row_id), self.measuring(s)
                    if on_tr and limits.get("tokenrouter") is not None and len(self.tr_running) >= limits["tokenrouter"]:
                        i += 1                          # the key's rate limit: a session of another payer may still start
                        continue
                    if on_tr and (self.tr_measuring or (measuring and self.tr_running)
                                  or (not measuring and any(self.measuring(p) for p in pending))):
                        # A first neutral session is alone on TokenRouter, so the usage figure measures it; while
                        # one waits for that, nothing else paid there starts, so it need not wait a whole row out.
                        i += 1
                        continue
                    if not await self.account_allows():
                        break
                    verdict = self.budget.check([self.row_of[row_id]], need=self.need(s), reserved=self.reserved(),
                                                what=f"session {s['key']}")
                    if not verdict["ok"]:
                        self.refused = verdict
                        self.log(f"{s['key']} is not started: it needs {verdict['need_usd']:.2f} USD, "
                                 f"{verdict['reserved_usd']:.2f} USD is held for sessions in flight and "
                                 f"{verdict['remaining_usd']:.2f} USD remain above the floor")
                        break
                    pending.pop(i)
                    entry_of[s["key"]] = s
                    if on_tr:
                        self.alone[s["key"]] = not self.tr_running
                        for k in self.tr_running:
                            self.alone[k] = False
                        self.tr_running.add(s["key"])
                        if measuring:
                            self.tr_measuring.add(s["key"])
                    running[s["key"]] = asyncio.create_task(self.one(s), name=f"rj-session-{s['key']}")
                    launched += 1
                    self.ran.append(s["key"])
                    self.log(f"{s['key']}: started ({len(running)} in flight)")
                if not running:
                    break
                done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
                for key in [k for k, task in running.items() if task in done]:
                    task = running.pop(key)
                    self.tr_running.discard(key)
                    self.tr_measuring.discard(key)
                    self.finished(task.result())
        except BaseException:
            for task in running.values():
                task.cancel()
            await asyncio.gather(*running.values(), return_exceptions=True)
            running.clear()
            raise
        return launched

    async def one(self, s: dict) -> dict:
        whole = machine.summarize(self.ctx.run_dir, s)["state"] == "PLANNED"     # this call sees the session from its first step
        before = await self.budget.read_usage("session_start", s["key"])
        try:
            result = await machine.run_session(self.ctx, s, self.row_of[s["row"]])
        except Exception as error:  # noqa: BLE001  (one session's bug must not cancel the sessions beside it)
            self.errors[s["key"]] = machine.note_error(self.ctx, s, error)
            result = machine.summarize(self.ctx.run_dir, s)
        done = self.ctx.usage_done.pop(s["key"], None)  # read as the session ended, before the balance pause
        after = await self.budget.read_usage("session_end", s["key"])
        result["tr_cents"] = [before, after if after is not None else done]
        result["whole"] = whole
        return result

    # ----------------------------------------------------------------------- when a session is over

    def finished(self, result: dict) -> None:
        key, row_id = result["key"], result["row"]
        self.results[key] = result
        cost = "no cost" if result["cost_usd"] is None else f"{result['cost_usd']:.2f} USD by the ledger"
        self.log(f"{key}: {result['state']}" + (f" ({result['outcome']})" if result["outcome"] else "")
                 + f", {result['minutes']:.1f} agent minutes, {cost}")
        if result["state"] in machine.TERMINAL and key == smoke_key(row_id) and row_id not in self.smoke and not result["until"]:
            self.judge(result)
        elif self.limit_suspect(result):
            self.log(f"note: {key}: {self.limit_suspect(result)}: this may be the account's limit and no finding about the "
                     "model; check 7's late recall says which (rj preflight --paid --rows ROW --steps ready,resume,files,late)")
        self.read_agent_work(result)

    def limit_suspect(self, result: dict) -> str | None:
        """Why a session looks cut by the account's 30-minute task limit (plan checks 7 and 11); None when it
        does not, or when preflight.json says a session outlives a task (`account.task_is_session: false`)."""
        if self.task_is_session is False or result["state"] not in machine.TERMINAL or result["until"]:
            return None
        clocks = [m for m in (result.get("minutes"), result.get("wall_minutes")) if isinstance(m, (int, float))]
        near = [m for m in clocks if NEAR_TASK_LIMIT[0] <= m <= NEAR_TASK_LIMIT[1]]
        cut = result["state"] == "ENDED" and result["outcome"] in ("capped", "timeout", "ended", "infra_failed")
        if cut and near:
            return (f"it ended ({result['outcome']}) after {near[0]:.0f} minutes, near the {TASK_LIMIT_MINUTES:.0f} minutes "
                    "the account's plan allows a task")
        if result["context_lost"] is True and clocks and max(clocks) > TASK_LIMIT_MINUTES:
            return (f"its conversation was lost and it ran {max(clocks):.0f} minutes, past the {TASK_LIMIT_MINUTES:.0f} minutes "
                    "the account's plan allows a task")
        return None

    def judge(self, result: dict) -> None:
        """The smoke rule on a row's first neutral session, and section 14's rule: its cost and minutes are measured."""
        row_id, key = result["row"], result["key"]
        row = self.row_of.get(row_id) or next((r for r in manifest.read_matrix(self.ctx.run_dir) if r["id"] == row_id), None)
        step_up = (row or {}).get("next")
        if result["outcome"] == "infra_failed":         # no finding about the row: held for this call, no verdict kept
            self.held[row_id] = (f"its first neutral session {key} ended infra_failed; no smoke verdict is recorded, "
                                 f"and `redo` runs it again")
            self.log(f"row {row_id} is held back: {self.held[row_id]}")
            last = result.get("failures") or []
            provider = len(last) >= 2 and all(f["kind"] == "provider" for f in last[-2:])
            suspect = self.limit_suspect(result)
            if suspect:
                self.log(f"row {row_id}: {suspect}: that is the account's limit, not the model (plan check 11); "
                         "no replacement is proposed")
                self.ctx.stop(f"{key}: {suspect}")
            elif provider and step_up:
                self.proposals.append(f"rj replace {row_id} {step_up}")
                self.log(f"proposed, not done, if this is a model reason: rj replace {row_id} {step_up} "
                         f"(both attempts failed on a provider error: {last[-1]['reason']})")
            elif provider:
                self.log(f"row {row_id}: both attempts failed on a provider error ({last[-1]['reason']}): this may be a "
                         "model reason, but the row lists no step-up (`next`) in the matrix")
            else:
                self.log(f"row {row_id}: an account or infra reason; no replacement is proposed")
            return
        verdict = smoke_verdict(result)
        self.smoke[row_id] = verdict
        write_json(self.ctx.run_dir / SMOKE, self.smoke)
        if result["state"] == "DONE" and row is not None:
            before, after = result.get("tr_cents") or (None, None)
            attempt = machine.attempt_of(Path(result["dir"]))
            alone = bool(result["alone"] if "alone" in result else self.alone.get(key)) and bool(result.get("whole")) and result["attempts"] == 1
            measured = self.budget.measure(row, key, attempt, result["minutes"], tr_before=before, tr_after=after, alone=alone)
            self.log(f"row {row_id}: measured {measured['session_usd']:.2f} USD and {measured['minutes']:.0f} minutes "
                     f"per session (from {measured['source']})")
            if measured["ledger_within_20pct"] is False:    # check 11's rule, as a warning (budget.py says why not a stop)
                readings = measured["ledger_readings_usd"]
                self.log(f"WARNING, check 11: row {row_id}: the ledger's estimate of this session "
                         f"({readings['input_excludes_cache']:.2f} USD, or {readings['input_includes_cache']:.2f} USD if "
                         f"input_tokens counts cached tokens) is not within 20% of the rise of TokenRouter's usage figure "
                         f"({measured['session_usd']:.2f} USD). The guard goes by the usage figure from here on; every "
                         "per-session cost of this row stays an estimate and is that far off")
            elif measured["source"] == "ledger" and measured["provider"] == TOKENROUTER:
                self.log(f"row {row_id}: measured from the ledger's estimate, not from TokenRouter's usage figure (the session "
                         "was resumed or took two attempts, or the figure was not readable on both sides of it or did not "
                         "move); the ledger overstated Codex nearly threefold on 17 Nov 2026")
        if verdict["does_not_stop"]:
            self.log(f"row {row_id} does not stop: it neither stopped at the plan nor asked at FORK-2 "
                     "(flagged on the page; the row is not dropped)")
        if verdict["passed"]:
            self.log(f"row {row_id} passes the smoke rule")
            return
        why = verdict["reason"]
        self.held[row_id] = f"its first neutral session {key} did not pass the smoke rule ({why})"
        self.log(f"row {row_id} fails the smoke rule and is not launched further: {why}")
        suspect = self.limit_suspect(result)
        if suspect:                                     # plan check 11: "the account limit; stop, no step-up"
            self.log(f"row {row_id}: {suspect}: that may be the account's limit and is no finding about the model; "
                     "no replacement is proposed. Check 7's late recall says which: rj preflight --paid --rows "
                     f"{row_id} --steps ready,resume,files,late")
            self.ctx.stop(f"{key}: {suspect}")
        elif result.get("cut_reason") not in KNOWN_CUTS:
            self.log(f"row {row_id}: a turn was cut for a reason rj does not set and has not met ({result['cut_reason']!r}): "
                     "read it before replacing the row; no replacement is proposed")
        elif verdict["part_a_green"] and result["state"] == "DONE" and result["canary_replied"] is not True:
            self.log(f"row {row_id}: the canary turn gave no reply, which says nothing about the model; no replacement "
                     "is proposed, and `redo` runs the session again")
        elif verdict["outcome"] not in MODEL_REASONS:
            self.log(f"row {row_id}: outcome {verdict['outcome']} is an account or infra reason; no replacement is proposed")
        elif step_up:
            self.proposals.append(f"rj replace {row_id} {step_up}")
            self.log(f"proposed, not done: rj replace {row_id} {step_up}")
        else:
            self.log(f"row {row_id} lists no step-up (`next`) in the matrix")

    def read_agent_work(self, result: dict) -> None:
        """Check 9, from the balance read before and after a session: is agent work billed? The verdict is kept
        in <runs>/agent_work.json, so the next call knows before it launches anything."""
        fall = result.get("balance_fall_usd")
        if fall is None or result["state"] not in machine.TERMINAL:
            return
        if fall > BALANCE_EPS_USD:
            self.set_agent_work("billed", result, fall)
            if not self.accept_agent_work:
                self.ctx.stop(f"the HarnessRouter balance fell by {fall:.2f} USD across {result['key']}: agent work is "
                              "billed (or drawn from granted credit); pass --accept-agent-work (accept_agent_work) to go on")
        elif self.agent_work == "cannot_tell" and result["state"] == "DONE" and not result["until"]:
            self.set_agent_work("free", result, fall)

    def set_agent_work(self, verdict: str, result: dict, fall: float) -> None:
        self.agent_work = verdict
        write_json(self.ctx.runs_dir / AGENT_WORK, {"verdict": verdict, "session": result["key"], "run": self.ctx.run,
                                                    "balance_fall_usd": fall, "service": self.budget.service, "at": now()})

    # ----------------------------------------------------------------------- the summary

    def answers_sent(self) -> int:
        """How many adaptive answers this run has posted, by the journals of every session it records (an attempt
        that was put away included): its FALLBACK_SENT lines whose message was an adaptive answer."""
        sent = 0
        for entry in manifest.read_manifest(self.ctx.run_dir):
            sdir = manifest.session_dir(self.ctx.run_dir, entry["key"])
            for d in [sdir, *(kept for _, kept in machine.kept_attempts(sdir))]:
                sent += sum(1 for e in machine.Journal(d / machine.JOURNAL).entries
                            if e["state"] == "FALLBACK_SENT" and e.get("message_kind") == "adaptive")
        return sent

    def summary(self, profile: str, warnings: list[str]) -> dict:
        wanted = [s for s in self.sessions if not (self.smoke_only and s["key"] != smoke_key(s["row"]))]
        unfinished = [s["key"] for s in wanted
                      if self.results[s["key"]]["state"] not in machine.TERMINAL or self.results[s["key"]]["until"]]
        failed = [k for k, r in self.results.items() if r["outcome"] == "infra_failed"]
        held = {r: why for r, why in self.held.items() if any(s["row"] == r for s in self.sessions)}
        budget = self.budget.state()
        calls = [e for e in self.budget.ledger.mine() if e.get("kind") == "adaptive" and e.get("run") == self.ctx.run]
        written = {"calls": len(calls), "used": self.answers_sent(), "cost_usd": round(sum(_usd(e) for e in calls), 6)}
        over = budget["remaining_usd"] < -OVER_EPS_USD      # into the floor, or past the total: never a clean exit
        code = 2 if self.ctx.stopped or self.refused or over else 1 if held or failed or unfinished or self.errors else 0
        return {"profile": profile, "run": self.ctx.run, "run_dir": str(self.ctx.run_dir), "stages": self.stages,
                "sessions": self.results, "ran": self.ran, "skipped": self.skipped, "unfinished": unfinished,
                "infra_failed": failed, "errors": self.errors, "refused": self.refused, "stopped": self.ctx.stopped,
                "smoke": {r: v for r, v in self.smoke.items() if any(s["row"] == r for s in self.sessions)},
                "held": held, "proposals": self.proposals, "measured": self.budget.measured(),
                "budget": budget, "over_budget": over, "adaptive": written, "agent_work": self.agent_work, "warnings": warnings,
                "ok": code == 0, "exit_code": code}
