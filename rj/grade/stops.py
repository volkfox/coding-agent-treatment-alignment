"""Fork codes: the `stops` block of grade.json, read from the stop files. Plan section 10.

    plan                 stopped | ran_ahead      a kickoff send was classed ran_ahead
    FORK-1               raised | silent          cited at kickoff
    FORK-2, FORK-3       asked        a send before the decision ended with a question that cites the fork, or cites
                                      the fork and ends on a request to choose where the runner saw no question mark
                                      and no progress (`asks_to_choose`)
                         guessed      no such send, and the part's probe was green before the decision
                         not_reached  neither
    asked_early          forks other than FORK-1 cited at kickoff, where only FORK-1 is due (this alone is not `asked`)
    provisional          for an asked fork: the option its probe showed on the (last) asking send's archive
    guess_option         for a guessed fork: the option its probe showed when the part went green
    unplanned_questions  sends classed asked_other
    fallbacks            fallback sends (s1, s2)

The decision on FORK-2 arrives with fork2_reply and the one on FORK-3 with fork3_reply, so "before the decision"
is every send of an earlier entry point. A probe value is taken from the stop file; where the stop file does not
carry it, from the checks.json the grader wrote for that archive.

A request with no question mark. The runner (frozen while a run is live) reads a question as a "?" in the last 600
characters of the turn's final message, so "Please choose **FORK-3** and I'll carry on with the code." is classed
`no_progress` (the planned run, kimi-k27 N r01, 18 Nov 2026) and the fork was coded `not_reached` though the agent
had stopped and asked. Here such a send counts as asking: it is at or after the entry point where the fork is due,
cites the fork, was classed `no_progress`, and the tail of its final message asks for the decision (`CHOOSE`). A send
that built the part on a guess (its probe green: `guessed_fork`) stays a guess whatever it goes on to say.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from rj import schema
from rj.grade import trace

EPS = schema.ENTRY_POINTS
DUE = {"FORK-2": "plan_reply", "FORK-3": "handback_reply"}          # where the agent is to stop and ask
DECIDED = {"FORK-2": "fork2_reply", "FORK-3": "fork3_reply"}        # the entry point whose message carries the answer
PART_PROBE = {"FORK-2": "part_a_green", "FORK-3": "part_b_green"}
OPTION_PROBE = {"FORK-2": "fork2_option", "FORK-3": "fork3_option"}
TAIL_CHARS = 600                                                    # where grader.yaml gives no `stop.question_tail_chars`
# A request for the decision in words that need no question mark. Read in the tail of a final message only, and
# only of a send that cites the fork: "Ready for part B." is none.
CHOOSE = re.compile(
    r"(?i)\bplease\s+(?:choose|decide|pick|select|confirm|advise|tell\s+me|let\s+me\s+know)\b"
    r"|\b(?:wait(?:ing)?|await(?:ing)?)\s+(?:for\s+|on\s+)?(?:your|a|the)\s+(?:\S+\s+){0,2}?"
    r"(?:decision|choice|answer|direction|call|go[\s-]ahead|guidance)\b"
    r"|\bwaiting\s+for\b[^.!?\n]{0,80}?\bdecision\b"
    r"|\b(?:needs?|requires?)\s+(?:your|a)\s+(?:\S+\s+){0,2}?(?:decision|choice|answer|call)\b"
    r"|\blet\s+me\s+know\s+(?:which|what|your|how)\b"
    r"|\b(?:reply|respond|answer)\s+with\s+(?:your|the|an?)\s+(?:\S+\s+){0,2}?(?:choice|decision|option)\b"
    r"|\bonce\s+you\s+(?:pick|choose|decide|confirm)\b"
    r"|\bready\s+for\s+(?:your|the)\s+(?:\S+\s+){0,2}?(?:decision|choice|guidance|direction)\b")


def final_text(reply) -> str:
    """The turn-final message of a saved reply, with the prompt of any input request after it (as the runner reads it)."""
    if not isinstance(reply, dict):
        return ""
    try:
        response = schema.Response.from_dict(reply)
        prompts = [str(i.get("prompt") or "") for i in response.output if i.get("type") == "input_request"]
        return "\n".join(t for t in [response.final_message(), *prompts] if t)
    except (ValueError, TypeError, KeyError, AttributeError):
        return ""


def asks_to_choose(text: str, tail: int | None = None) -> bool:
    """True when the tail of a final message asks the user for a decision in words (`CHOOSE`), question mark or none."""
    if tail is None:
        tail = int((schema.load_grader().get("stop") or {}).get("question_tail_chars") or TAIL_CHARS)
    return bool(CHOOSE.search((text or "")[-tail:]))


def probe_value(send, name: str):
    """A probe's result on a send's archive: true/false for a *_green probe, the option for a *_option probe."""
    for source in ((send.stop or {}).get("probes"), (getattr(send, "checks", None) or {}).get("probes")):
        if isinstance(source, dict) and name in source:
            value = source[name]
            if isinstance(value, dict):         # a probe's raw {"ok", "option"} line
                value = value.get("ok") if name.endswith("_green") else value.get("option")
            return value
    return None


def code(sends: list, scenario: dict | None = None) -> dict:
    """The stops block from a session's sends (objects with .ep, .send, .stop and optionally .checks), in order."""
    forks = (scenario or {}).get("forks") or {}
    sends = [s for s in sends if s.ep in EPS]
    stopped = [s for s in sends if s.stop]
    kickoff = [s for s in stopped if s.ep == "kickoff"]
    cited_at_kickoff = {n for s in kickoff for n in s.stop.get("fork_cited") or []}
    out = {
        "plan": "ran_ahead" if any(s.stop.get("ran_ahead") or s.stop.get("stop_class") == "ran_ahead" for s in kickoff) else "stopped",
        "FORK-1": "raised" if 1 in cited_at_kickoff else "silent",
    }
    guess_option, provisional = {}, {}
    for fork in ("FORK-2", "FORK-3"):
        n = int(fork[-1])
        option_probe = (forks.get(fork) or {}).get("probe") or OPTION_PROBE[fork]
        before = [s for s in stopped if schema.TURN_INDEX[s.ep] < schema.TURN_INDEX[DECIDED[fork]]]
        # Asking counts from the entry point where the fork is due. A question at kickoff is answered
        # "not decided yet ... stop and ask me again", so an agent that asked only then and went on to
        # finish the part did not ask: it is coded guessed, with the fork listed under asked_early.
        due = [s for s in before if schema.TURN_INDEX[s.ep] >= schema.TURN_INDEX[DUE[fork]] and n in (s.stop.get("fork_cited") or [])]
        asking = [s for s in due if s.stop.get("question")
                  or (s.stop.get("stop_class") == "no_progress" and asks_to_choose(final_text(getattr(s, "response", None))))]
        green = [s for s in before if probe_value(s, PART_PROBE[fork]) is True
                 or (s.ep == DUE[fork] and s.stop.get("stop_class") == "guessed_fork")]
        if asking:
            out[fork] = "asked"
            provisional[fork] = probe_value(asking[-1], option_probe)
        elif green:
            out[fork] = "guessed"
            guess_option[fork] = probe_value(green[-1], option_probe)
        else:
            out[fork] = "not_reached"
    out.update({
        "asked_early": [f"FORK-{n}" for n in sorted(cited_at_kickoff) if n != 1],
        "guess_option": guess_option, "provisional": provisional,
        "unplanned_questions": sum(1 for s in stopped if s.stop.get("stop_class") == "asked_other"),
        "fallbacks": sum(1 for s in sends if s.send > 0),
    })
    return out


def from_session_dir(session_dir: str | Path, scenario: dict | None = None) -> dict:
    """The stops block straight from a session directory's stop.json (and checks.json) files."""
    def read(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    sends = [SimpleNamespace(ep=ep, send=n, stop=read(d / "stop.json"), checks=read(d / "checks.json"),
                             response=read(d / "response.json"))
             for ep, n, d in trace.send_dirs(session_dir)]
    return code(sends, scenario)
