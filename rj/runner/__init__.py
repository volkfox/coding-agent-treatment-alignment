"""The runner: sends the scripted user's turns through HarnessRouter, once each, under the cost guard.
Plan section 11.

    from rj.runner import run
    summary = await run("demo", through="baseline", smoke_only=True)

    manifest.py   which sessions a run holds: manifest.jsonl, matrix.yaml, manifest.json; replacements; live entries
    machine.py    one session as a journaled state machine: resume, the per-turn check, the failure rule, the lock
    stops.py      live stop detection: what a finished turn was (stop.json)
    adaptive.py   the adaptive answer: one reply written by a model for an agent that asks again after the fallback line
    budget.py     the one ledger, the TokenRouter usage figure, the guard, measured.json
    launch.py     `run`: stages, the in-flight limits, the smoke rule; `redo`

For the command line:
    run(...)                    `rj run`; raises RunLocked when the run directory is in use, and manifest.OtherHashes when
                                it records a selected key under other hashes than today's files (accept_other_hashes)
    redo(key, run=, runs_dir=)  `rj run --redo KEY`: another life for a session that ended (or is stuck)
    live_entry(row, habit, run_dir=) + open_context(...) + run_session(ctx, entry, row, until="kickoff")
                                `rj row --live`: a kickoff-only session under a key no stage plans
    prices_with_measured(runs)  the price book for rj.aggregate.aggregate(prices=...), with measured.json merged
    in_order(entries)           manifest entries by (order, key): manifest.jsonl itself is in the order of planning
    summarize(run_dir, entry)   a session's state; grade and aggregate only sessions that are DONE or ENDED
"""
from rj.runner.budget import prices_with_measured
from rj.runner.launch import open_context, redo, run, smoke_key
from rj.runner.machine import Infra, Resumable, RunLocked, run_session, summarize
from rj.runner.manifest import in_order, live_entry

__all__ = ["run", "redo", "open_context", "run_session", "summarize", "smoke_key", "live_entry", "in_order",
           "prices_with_measured", "Infra", "Resumable", "RunLocked"]
