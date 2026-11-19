"""How well each user message fitted what the agent had just said.

    python -m rj.fit <run_dir> [--json]

The user's words are fixed in advance (plan section 7), so a message can miss what the agent asked. This module
measures that from what a run leaves on disk, per message sent after the kickoff. It reads each send's
request.json, the response.json and stop.json of the send before it, and nothing else; it calls no service.

Codes, one per message:
  fits       its premise holds: it approves a plan the agent sent, decides the design question the agent asked
             about (by a question that cites it, or by citing it and ending on a request to choose: "Please choose
             FORK-3 and I'll carry on", which the runner's stop class does not see), or receives the part the
             agent handed back
  unasked    it decides a design question the agent did not ask about at its last stop
  premise    it approves a plan the agent did not send, or receives a part the agent did not hand back
  fallback   the fixed fallback line was sent: the agent stopped where the script has no message
  adaptive   an adaptive answer was sent (a model wrote it; rj/runner/adaptive.py): the agent asked again after
             the fallback line. Counted apart from `fallback`

Which message a send carried is request.json's `message_kind`, as the runner marks it. A run directory made
before that mark is read as before: the fallback line by its text, anything else as the script's message.
Each entry says so under `kind` (scripted, fallback, adaptive) and, for a reply to an unplanned question, under
`tone` which fallback line went out above the answer: the wording column it is the fallback cell of
(request.json's `fallback_tone`), or null where no line went (a run made before 18 Nov 2026, or
`fallback.toned: false`).

Beside the code: `side_questions`, the agent's other questions at that stop (sentences ending in "?" that name no
design question, by id or keyword), and `side_answer`: "blanket" when the message carries the sentence that hands
such questions back to the agent (or is the fallback line, which does the same), "adaptive" for an adaptive
answer (it was written to those questions), else "none".
The counts are regex work on prose and are meant to be read by hand beside the sentences they list. Every block
B2 to B6 now carries the hand-back sentence, so `side_unanswered` is 0 by construction in a session run with
them; whether a reply answered what was asked is the judge's to say (rj.grade.judge, the fit call), and
`for_results` sets its level beside each message's code.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from rj import schema, script
from rj.grade import stops as fork_codes
from rj.grade import trace
from rj.runner import stops

CODES = tuple(schema.FIT_CODES)           # fits, unasked, premise, fallback, adaptive
BLANKET = "Anything else that is open"       # the sentence of blocks B2 to B6 that hands other questions back to the agent
BLANKET_OLD = "Anything else you asked"      # the same sentence as B2 alone carried it in the first paid sessions (runs/demo)
PREMISE = {                                   # entry point -> what its message presumes of the stop before it
    "plan_reply": ("plan", None),
    "fork2_reply": ("fork", 2),
    "handback_reply": ("handback", None),
    "fork3_reply": ("fork", 3),
    "review": ("handback", None),
}
UNTREATED = ("seed", "canary")


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def side_questions(text: str, forks: dict) -> list[str]:
    """The question sentences of a message that name no design fork, neither by id nor by keyword."""
    patterns = [stops.keyword_pattern(k) for fork in forks.values() for k in fork.get("keywords") or []]
    out = []
    for sentence in stops.question_sentences(text):
        if re.search(r"(?i)\bfork[- ]?\d\b", sentence) or any(p.search(sentence) for p in patterns):
            continue
        out.append(sentence)
    return out


def code_for(ep: str, is_fallback: bool, before: dict | None, agent_said: str = "") -> str:
    """The fit code of the message sent at `ep`, given the stop.json of the send before it and what the agent said there."""
    if is_fallback:
        return "fallback"
    kind, fork = PREMISE[ep]
    stop_class = (before or {}).get("stop_class")
    if kind == "fork":
        asked = stop_class == "asked_fork" or fork_codes.asks_to_choose(agent_said)
        return "fits" if asked and fork in ((before or {}).get("fork_cited") or []) else "unasked"
    wanted = "asked_plan" if kind == "plan" else "handed_back"
    return "fits" if stop_class == wanted else "premise"


def kind_of(request: dict, ep: str, send: int, fallback_text: str) -> str:
    """What a send carried, one of schema.MESSAGE_KINDS: the runner's mark in request.json, or, in a run directory
    made before the mark, the fallback line by its text (and any later send of the kickoff) and else the script's."""
    marked = request.get("message_kind")
    if marked in schema.MESSAGE_KINDS:
        return marked
    text = request.get("input") if isinstance(request.get("input"), str) else ""
    return "fallback" if text.strip() == fallback_text.strip() or (send > 0 and ep not in PREMISE) else "scripted"


def session_fit(session_dir: str | Path, forks: dict | None = None, fallback_text: str | None = None) -> list[dict]:
    """One entry per user message after the kickoff's first send, in the order sent."""
    forks = script.load_scenario()["forks"] if forks is None else forks
    fallback_text = script.fallback_message() if fallback_text is None else fallback_text
    sends = trace.send_dirs(session_dir)
    out = []
    for i, (ep, send, d) in enumerate(sends):
        if ep in UNTREATED or (ep == "kickoff" and send == 0) or i == 0:
            continue
        request = _load(d / "request.json") or {}
        text = request.get("input") if isinstance(request.get("input"), str) else ""
        before_dir = sends[i - 1][2]
        before = _load(before_dir / "stop.json")
        reply = _load(before_dir / "response.json")
        agent_said = ""
        if isinstance(reply, dict):
            try:
                agent_said = stops.said(schema.Response.from_dict(reply))
            except (ValueError, TypeError, KeyError):
                agent_said = ""
        kind = kind_of(request, ep, send, fallback_text)
        is_fallback = kind == "fallback"
        asked = side_questions(agent_said, forks)
        out.append({
            "ep": ep, "send": send, "kind": kind,
            "tone": request.get("fallback_tone") if kind != "scripted" and isinstance(request.get("fallback_tone"), str) else None,
            "code": "adaptive" if kind == "adaptive" else
                    code_for(ep, is_fallback, before, agent_said) if (ep in PREMISE or is_fallback) else "fallback",
            "stop_before": (before or {}).get("stop_class"),
            "side_questions": asked,
            "side_answer": "adaptive" if kind == "adaptive" else
                           "blanket" if (is_fallback or BLANKET in text or BLANKET_OLD in text) else "none",
        })
    return out


def summary(entries: list[dict]) -> dict:
    """Counts over one session's entries."""
    counts = {c: sum(1 for e in entries if e["code"] == c) for c in CODES}
    side = sum(len(e["side_questions"]) for e in entries)
    unanswered = sum(len(e["side_questions"]) for e in entries if e["side_answer"] == "none")
    return {"messages": len(entries), **counts, "side_questions": side, "side_unanswered": unanswered}


def misfits(s: dict | None) -> int | None:
    """Messages of a session that did not fit, from its summary."""
    return None if not s else s["messages"] - s["fits"]


def for_results(session_dir: str | Path, neutral_dirs: list[str | Path] | None = None,
                judged: list[dict] | None = None) -> dict | None:
    """The block results.json carries per session: the summary, `worse_than_neutral`, `not_clean` and `replies`.

    `worse_than_neutral` is true when this session had more messages that did not fit than any of its row's
    neutral sessions (`neutral_dirs`); it is null for a neutral session, for a row without one, and for a
    session with no message after the kickoff. `not_clean` is true when that flag is, and for any session that
    got an adaptive answer, a neutral one too: such a session differs from the ones it is set beside in what the
    user said to it, as well as in the user's register. `replies` lists each message's code, what it was (`kind`:
    scripted, fallback or adaptive; `tone`: the fallback line above a reply to an unplanned question, null where
    none went) and, from `judged` (grade.json's `effort.judge.reply_fit.messages`), the judge's level 0 to 3 for it
    and how many of the agent's questions it left with no answer; both are null where the judge was not asked.
    """
    mine = session_fit(session_dir)
    if not mine:
        return None
    s = summary(mine)
    base = [misfits(summary(e)) for e in (session_fit(d) for d in neutral_dirs or []) if e]
    worse = (misfits(s) > max(base)) if base else None
    rated = {(m.get("ep"), m.get("send")): m for m in judged or [] if isinstance(m, dict)}
    return {**s, "worse_than_neutral": worse, "not_clean": bool(s["adaptive"]) or worse is True,
            "replies": [{"ep": e["ep"], "send": e["send"], "code": e["code"], "kind": e["kind"], "tone": e["tone"],
                         "level": rated.get((e["ep"], e["send"]), {}).get("level"),
                         "unanswered": rated.get((e["ep"], e["send"]), {}).get("unanswered")} for e in mine]}


def run_fit(run_dir: str | Path) -> dict[str, dict]:
    """{session key: {"entries": [...], "summary": {...}}} for every session of a run that has sends on disk."""
    run_dir = Path(run_dir)
    manifest = [json.loads(line) for line in (run_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    found = {}
    for m in manifest:
        entries = session_fit(run_dir / "sessions" / m["key"])
        if entries:
            found[m["key"]] = {"entries": entries, "summary": summary(entries)}
    return found


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    found = run_fit(args[0])
    if "--json" in argv:
        print(json.dumps(found, indent=1, ensure_ascii=False))
        return 0
    for key, got in found.items():
        s = got["summary"]
        print(f"{key}: {s['fits']} of {s['messages']} messages fit; unasked {s['unasked']}, premise {s['premise']}, "
              f"fallback {s['fallback']}, adaptive {s['adaptive']}; the agent's other questions: {s['side_questions']}, "
              f"of which {s['side_unanswered']} got no answer")
        for e in got["entries"]:
            extra = f"; other questions {len(e['side_questions'])} ({e['side_answer']})" if e["side_questions"] else ""
            print(f"    {e['ep']}.s{e['send']}: {e['code']} (the agent had {e['stop_before']}){extra}")
            for q in e["side_questions"]:
                print(f"        ? {q[:140]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
