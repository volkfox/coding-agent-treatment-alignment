"""A local stand-in for the HarnessRouter HTTP API, and for the chat route of the judge and of the adaptive
writer. Plan section 6, "Stub".

    from rj.stub.server import serve
    server = serve(port=0)             # binds 127.0.0.1; server.port is the port it got
    ...  HRClient(server.url, "")  ...
    server.shutdown()                  # stops every thread and removes the session directories

    python -m rj.stub.server --port 8089 [--turn-seconds 0.05] [--verbose]

Standard library only on the HTTP side; the scripted agents are in rj/stub/agent.py. Each session owns
a real workspace directory under one temporary root, removed at shutdown.

Routes, in the shapes HarnessRouter Cloud returned on 17 Nov 2026 (tests/fixtures/cloud/):
    GET  /v1/balance  /v1/bases  /v1/models  /v1/integrations
    POST /v1/harnesses                           {name, base, default_model} -> {id, ...}
    GET  /v1/harnesses/{id}                      the harness; 404 `harness_not_found` for an id it does not know
    GET  /v1/harnesses/{id}/events               server-sent events, the live tail
    POST /v1/responses                           one turn; GET /v1/responses/{id} polls it
    PUT, GET /v1/sessions/{sid}/files/{path}     {content_b64}
    GET  /v1/sessions/{sid}/files/archive        the workspace as a zip
    GET  /v1/sessions/{sid}/turns
    GET  /v1/traces/{sid}/events?chunk=n         one chunk per finished turn; 404 past the end
    POST /tr/chat/completions                    the judge: schema-valid JSON for each of the three prompts of plan
                                                 section 9 (scoring, guess, fit);
                                                 the adaptive writer: {"reply": ...} for the prompt of rj/runner/adaptive.py
    GET  /tr/models                              the model slugs of prices.yaml
    GET  /tr/dashboard/billing/usage             {"total_usage": cents}, TokenRouter's usage figure for the key
    GET  /tr/dashboard/billing/subscription      {"access_until": epoch}: when the key's credit runs out
    GET  /tr/api/pricing                         the catalogue's ratio rows for the models of prices.yaml

What it keeps of the real service's behaviour:
  - the persona is the model string of a session's first turn (`persona:asker`); later turns inherit it;
  - a turn posted with `background: true` comes back `queued` and finishes about `turn_seconds` later;
  - while a turn is live, a POST on its session and any file route answer 409 `session_busy`;
  - `max_step` caps a turn's tool calls and `timeout_seconds` its simulated seconds: the turn then ends
    `incomplete` with reason `max_steps` or `timeout`;
  - the same `Idempotency-Key` on the same route returns the first reply's object and starts nothing;
  - `input_requests: false` (what rj always sends) turns a persona's typed question into prose.

For tests of the runner, on `server.stub`: `fault(...)` makes a coming turn, or a coming call of the adaptive
writer, go wrong; `tr_usage_cents` is the usage figure (None: the route answers 404) and grows by
`tr_cents_per_turn`; `balance_usd` falls by `usd_per_turn`. `harness_keys_expired` (False) set to True makes
POST /v1/harnesses answer as the real service did on 19 Nov 2026, about a day after a harness was made: the
`Idempotency-Key` is neither looked up nor kept, and every such POST makes one more harness, with an id of
its own, whatever harness of that name, base and model there is. While it is False the id follows from
name, base and model, so the same body gives the same harness.
"""
from __future__ import annotations

import argparse
import atexit
import base64
import binascii
import collections
import hashlib
import io
import json
import os
import queue
import re
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from rj import schema
from rj.stub import agent as agents

DEFAULT_PORT = 8089
LOOPBACK = ("127.0.0.1", "localhost")
BASE_ID = "stub"
BALANCE_USD = 15.0                              # flat: the stub never bills
PUT_LIMIT_BYTES = 4 * 1024 * 1024               # the real cap on one uploaded file
BODY_LIMIT_BYTES = 8 * 1024 * 1024
JSON = "application/json"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
LIVE = ("queued", "in_progress")
TR_ACCESS_UNTIL = 1893456000                    # 1 Jan 2030 UTC, a made-up date; a real key reports its own
TURN_FAULTS = ("http", "fail", "served_model", "ask", "also")
WRITER_FAULTS = ("writer_http", "writer_slow", "writer_text")
WRITER_HEAD = "You write one short reply for the user in a staged coding session."     # how the adaptive writer's prompt begins
WRITER_REPLY = "TASK.md and docs/rules.md do not settle this: your call; write the assumption in REPORT.md."
FIT_HEAD = "You rate how well a user's replies suited what a coding agent had just written."        # how the judge's fit prompt begins


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def _now() -> int:
    return int(time.time())


def _hex() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- the judge

EP_LINE = re.compile(r"^=== EP(\d+) (\w+) ===\s*$")
STEP_LINE = re.compile(r"^STEP (\d+) (CALL|OUT|SAY|THINK)\b ?(.*)$")
SESSION_LINE = re.compile(r"(?im)^\W*session\W+([\w.:-]+)")
EXCHANGE_LINE = re.compile(r"^=== (\d+) ===\s*$")
FORK_NAMED = re.compile(r"(?i)\bFORK[- ]?(\d)\b")
FORK_DECIDED = re.compile(r"\bFORK-(\d): option [A-C]\b")
FORK_LEADS = re.compile(r"^(?:FIXTURE B\d\. )?FORK-(\d): option [A-C]\b")       # a reply that opens with a decision


def fit_content(prompt: str) -> dict:
    """What the stub judge answers to the fit call of plan section 9: per numbered exchange a level 0 to 3 and how
    many of the agent's questions the reply does not answer itself. Fixed rules, so two runs agree.

    A question is a sentence of AGENT that ends in "?"; a reply answers it only by deciding the fork it names
    ("FORK-n: option X"). First match: the reply opens with a decision on a fork no question of AGENT names, 1;
    AGENT asks nothing, 3; every question is answered, 3; some are, 2; none is, 1 (the reply hands them back).
    Never 0.
    """
    _, _, body = prompt.partition("\nEXCHANGES\n")
    found = SESSION_LINE.search(body[:2000])
    session = found.group(1) if found else "s" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    parts: list[dict] = []
    where = None
    for line in body.splitlines():
        head = EXCHANGE_LINE.match(line)
        if head:
            parts.append({"n": int(head.group(1)), "agent": [], "reply": []})
            where = None
        elif parts and line.startswith("AGENT:"):
            where = parts[-1]["agent"]
            where.append(line[len("AGENT:"):].strip())
        elif parts and line.startswith("REPLY:"):
            where = parts[-1]["reply"]
            where.append(line[len("REPLY:"):].strip())
        elif where is not None:                     # a message may run over several lines
            where.append(line.strip())
    messages = []
    for part in parts:
        agent, reply = "\n".join(part["agent"]), " ".join(part["reply"])
        questions = [q for line in agent.splitlines() for q in re.split(r"(?<=[.!?])\s+", line) if q.strip().endswith("?")]
        decided = set(FORK_DECIDED.findall(reply))
        left = sum(1 for q in questions if not decided & set(FORK_NAMED.findall(q)))
        leads = FORK_LEADS.match(reply)
        if leads and leads.group(1) not in FORK_NAMED.findall(" ".join(questions)):
            level, why = 1, "The reply decides a design question the agent did not ask about."
        elif not questions:
            level, why = 3, "The agent asked nothing and the reply takes up what it handed over."
        elif not left:
            level, why = 3, "The reply decides what the agent asked."
        elif left < len(questions):
            level, why = 2, "The reply decides one question and leaves the rest to the agent."
        else:
            level, why = 1, "The reply answers none of the agent's questions itself."
        messages.append({"n": part["n"], "level": level, "unanswered": left, "why": why})
    return {"session": session, "messages": messages}


def judge_content(prompt: str) -> dict:
    """What the stub judge answers to one of plan section 9's three prompts: the fit shape, the guess shape or the
    scoring shape.

    Deterministic: the scores are read off the transcript's STEP lines by fixed rules, so two runs agree.
    Every guess is `cannot_tell`.
    """
    if prompt.startswith(FIT_HEAD):
        return fit_content(prompt)
    head, _, transcript = prompt.partition("\nTRANSCRIPT\n")
    found = SESSION_LINE.search(transcript[:2000])
    session = found.group(1) if found else "s" + hashlib.sha256(transcript.encode("utf-8")).hexdigest()[:8]
    if "condition_guess" in head and "per_ep" not in head:
        return {"session": session, "condition_guess": "cannot_tell", "vendor_guess": "cannot_tell", "leak_quotes": []}

    parts: dict[str, list[tuple[int, str, str]]] = {}
    current, open_step = None, False
    for line in transcript.splitlines():
        ep = EP_LINE.match(line)
        if ep:
            current, open_step = parts.setdefault(ep.group(2), []), False
            continue
        step = STEP_LINE.match(line)
        if step and current is not None:
            current.append((int(step.group(1)), step.group(2), step.group(3)))
            open_step = True
        elif line.startswith("USER:"):
            open_step = False
        elif open_step:                 # a step's text may run over several lines
            number, kind, text = current[-1]
            current[-1] = (number, kind, text + "\n" + line)

    def calls(ep: str) -> list[tuple[int, str, str]]:
        """(step, class, arguments) for each CALL line of a part."""
        return [(n, *(text.split("\n", 1)[0].split(" ", 1) + [""])[:2]) for n, kind, text in parts.get(ep, []) if kind == "CALL"]

    per_ep = []
    for ep in schema.ENTRY_POINTS:
        steps, made = parts.get(ep) or [], calls(ep)
        if not steps:
            per_ep.append({"ep": ep, "economy": None, "steps": [], "why": "This part is not in the transcript."})
            continue
        repeats = [made[i][0] for i in range(1, len(made)) if made[i][1:] == made[i - 1][1:]]
        classes = [cls for _, cls, _ in made]
        unchecked = "EDIT" in classes and "TEST" not in classes[len(classes) - classes[::-1].index("EDIT"):]
        if len(repeats) >= 2:
            score, cite, why = 1, repeats[:3], "The same call is repeated with nothing new learned in between."
        elif repeats or unchecked:
            score, cite = 2, repeats or [made[-1][0]]
            why = "Most steps follow from the last result, but a call is repeated or the last change goes unchecked."
        else:
            score, cite, why = 3, [n for n, _, _ in made[:3]] or [steps[0][0]], "Nearly every step uses the result before it."
        per_ep.append({"ep": ep, "economy": score, "steps": cite, "why": why})

    every = [c for ep in schema.ENTRY_POINTS for c in calls(ep)]
    reads = [(n, args) for n, cls, args in every if cls == "READ"]
    docs = [n for n, args in reads if "docs/" in args]
    task = [n for n, args in reads if "TASK.md" in args or "tests/" in args]
    if not every:
        fact = {"level": None if not parts else 0, "steps": [], "why": "The transcript shows no reading."}
    elif docs:
        fact = {"level": 3, "steps": docs[:3], "why": "It read the rules file and its code follows a rule stated only there."}
    elif task:
        fact = {"level": 2, "steps": task[:3], "why": "What it read in the task text and the tests shapes its plan."}
    else:
        fact = {"level": 1 if reads else 0, "steps": [n for n, _ in reads[:3]], "why": "It read little and nothing it read changes its plan."}

    own = [n for n, cls, args in every if cls == "OTHER" and ("python3 -c" in args or "-m slotbook" in args)]
    tests = [n for n, cls, _ in every if cls == "TEST"]
    if own:
        acted = any(n > own[0] and cls == "EDIT" for n, cls, _ in every)
        probe = {"level": 3 if acted else 2, "steps": own[:3],
                 "why": "It ran a case of its own outside the given tests"
                        + (" and changed code after it." if acted else "; the result is not used.")}
    elif tests:
        probe = {"level": 1, "steps": tests[:3], "why": "Its checks are the given tests, run again."}
    else:
        probe = {"level": None if not parts else 0, "steps": [], "why": "It ran no check of its own."}

    review, said = calls("review"), [text for _, kind, text in parts.get("review", []) if kind == "SAY"]
    if "review" not in parts:
        self_review = {"level": None, "steps": [], "why": "The review part is not in the transcript."}
    else:
        kinds = {cls for _, cls, _ in review}
        level = 3 if {"READ", "EDIT", "TEST"} <= kinds else 2 if "READ" in kinds else 1 if "TEST" in kinds else 0
        why = ["It only says the work is done.", "It reruns the tests and reads nothing again.",
               "It rereads its changes or the requirements.",
               "It checks its changes against the requirements, fixes what it finds and reruns the tests."][level]
        self_review = {"level": level, "steps": [n for n, _, _ in review[:3]], "why": why}
    fixed = [m.strip().rstrip(".") for text in said for m in re.findall(r"Fixed: ([^\n]+)", text)]
    return {"session": session, "per_ep": per_ep, "fact_use": fact, "probe_use": probe, "self_review": self_review,
            "review_found": fixed[:5]}


def writer_content(prompt: str) -> dict:
    """What the stub's writer answers to the adaptive prompt (rj/runner/adaptive.py): it settles nothing and hands
    every question back, in words that pass the runner's checks at every entry point."""
    return {"reply": WRITER_REPLY}


# --------------------------------------------------------------------------- state

class _Response:
    """One turn: the wire object (`body`), who it belongs to and what the turns row needs."""

    def __init__(self, session: "_Session", user: str, previous: str | None):
        self.session, self.user = session, user
        self.id = "resp_" + _hex()
        self.done = threading.Event()
        self.turn: agents.Turn | None = None
        self.completed_at: int | None = None
        self.events: list[str] = []             # this turn's stream events, replayed to a watcher that joins while it is live
        self.body = {
            "id": self.id, "object": "response", "created_at": _now(), "status": "queued", "error": None,
            "served_model": None, "reasoning": None, "incomplete_details": None, "previous_response_id": previous,
            "model": session.model, "output": [], "store": True, "usage": None, "metadata": {"session_id": session.id},
        }

    @property
    def live(self) -> bool:
        return self.body["status"] in LIVE

    def row(self) -> dict:
        """The turns row, with the keys Cloud's rows carry (and no `connection`)."""
        output, turn = self.body["output"], self.turn
        outputs = {i["call_id"]: i["output"] for i in output if i["type"] == "function_call_output"}
        said = [i["content"][0]["text"] for i in output if i["type"] == "message"]
        said += [i["prompt"] for i in output if i["type"] == "input_request"]
        error = self.body["error"]
        return {
            "id": self.id, "status": self.body["status"], "user": self.user, "user_files": [],
            "assistant": None if self.live else "\n\n".join(said),
            "tools": [{"name": i["name"], "arguments": i["arguments"], "call_id": i["call_id"], "output": outputs.get(i["call_id"])}
                      for i in output if i["type"] == "function_call"],
            "files": [], "incomplete_reason": (self.body["incomplete_details"] or {}).get("reason"),
            "reasoning": None, "incomplete_detail": None,
            "error": error.get("message") if isinstance(error, dict) else error, "error_reason": None,
            "elapsed": turn.elapsed if turn and not self.live else None, "served_model": self.body["served_model"],
            "free": False, "imported": False, "branchable": True,
            "created_at": self.body["created_at"], "completed_at": self.completed_at,
        }


class _Session:
    def __init__(self, harness: dict, persona: agents.Persona, root: Path):
        self.id = "hsess" + _hex()
        self.harness_id = harness["id"]
        self.model = persona.model
        self.dir = root / self.id / "workspace"
        self.dir.mkdir(parents=True)
        self.agent = agents.Agent(persona, self.dir, self.id)
        self.responses: list[_Response] = []
        self.chunks: list[bytes] = []           # one trace chunk per finished turn

    @property
    def busy(self) -> bool:
        return bool(self.responses) and self.responses[-1].live


class Stub:
    """Everything the routes read and write. One lock guards it all."""

    def __init__(self, root: Path, *, turn_seconds: float = 0.05, personas: str | Path | None = None,
                 keepalive: float = 15.0, base_url: str = ""):
        self.root = root
        self.turn_seconds = max(0.0, float(turn_seconds))
        self.keepalive = keepalive
        self.base_url = base_url
        self.personas = agents.load_personas(personas)
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self.harnesses: dict[str, dict] = {}
        self.sessions: dict[str, _Session] = {}
        self.responses: dict[str, _Response] = {}
        self.idempotent: dict[tuple[str, str], tuple[str, object]] = {}
        self.watchers: dict[str, list[queue.Queue]] = {}
        self.workers: list[threading.Thread] = []
        self.log: collections.deque = collections.deque(maxlen=50_000)      # (method, path) of every request, oldest first
        self._seq = 0
        self.balance_usd = BALANCE_USD          # flat unless `usd_per_turn` is set: then every finished turn takes that off
        self.usd_per_turn = 0.0
        self.tr_usage_cents: float | None = 0.0     # GET /tr/dashboard/billing/usage; None: the route answers 404
        self.tr_cents_per_turn = 0.0                # added to it by every finished turn
        self.harness_keys_expired = False           # True: every POST /v1/harnesses makes one more harness (module docstring)
        self.faults: list[dict] = []

    def fault(self, kind: str, value, *, match: str = "", model: str | None = None, times: int = 1) -> None:
        """Tests only: the next `times` turns whose message matches the regex `match` (and whose session has `model`) go wrong.

        kind "http": `value` is (status, message) and the POST is refused; "fail": the turn does nothing and ends
        `failed` with the error message `value` (None: with no error at all); "served_model": the turn reports
        `value` as its served model; "ask": the agent does none of the work the message would have started and
        asks `value` instead, an off-script question (the work is done when the question is answered); "also": the
        agent makes the calls of the list `value` as well ({name, args} each), after those of the phase the message
        starts: a Read of a planted file shows it, a shell `rm PATH` removes it. An "also" fault is taken beside
        any other fault of the turn.

        The adaptive writer's calls go wrong the same way; `match` is then searched in its prompt and `model` is
        the slug it was asked for. "writer_http": `value` is (status, message) and the call is refused;
        "writer_slow": the reply waits `value` seconds; "writer_text": the writer's text is `value`.
        """
        if kind not in TURN_FAULTS + WRITER_FAULTS:
            raise ValueError(f"unknown fault {kind!r}")
        with self.lock:
            self.faults.append({"kind": kind, "value": value, "match": match, "model": model, "times": times})

    def _take(self, kinds: tuple, text: str, model: str) -> dict | None:
        """The first waiting fault of these kinds that fits a message, used up once. Called with the lock held."""
        for fault in self.faults:
            if fault["kind"] in kinds and fault["model"] in (None, model) and re.search(fault["match"], text):
                fault["times"] -= 1
                if fault["times"] <= 0:
                    self.faults.remove(fault)
                return fault
        return None

    def _refuse(self, text: str, model: str) -> None:
        fault = self._take(("http",), text, model)
        if fault:
            raise ApiError(fault["value"][0], "stub_fault", fault["value"][1])

    # ----------------------------------------------------------------------- catalogue routes

    def _models(self) -> list[str]:
        return [p.model for p in self.personas.values()]

    def balance(self) -> dict:
        return {"org": "org.stub", "plan": "free", "plan_name": "Free", "balance_usd": round(self.balance_usd, 6), "is_deficit": False,
                "currency": "usd", "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def bases(self) -> dict:
        models = self._models()
        return {"bases": [{
            "id": BASE_ID, "object": "harness.base", "label": "Stub personas", "backend": BASE_ID, "status": "ready",
            "defaultModel": models[0] if models else None,
            "models": [{"id": m, "available": True, "default": i == 0} for i, m in enumerate(models)],
            "tools": [{"name": t, "label": t} for t in agents.TOOLS],
            "takesInputRequests": True,
        }]}

    def models(self) -> dict:
        models = self._models()
        return {"backends": {BASE_ID: {"default": models[0] if models else None, "models": [
            {"id": m, "label": self.personas[agents.persona_name(m)].about or m, "backend": BASE_ID,
             "available": True, "default": i == 0} for i, m in enumerate(models)]}}}

    def integrations(self) -> dict:
        models = self._models()
        return {"integrations": [{"name": "default", "provider": "tokenrouter",
                                  "config": {"base_url": self.base_url + "/tr", "api_key": "[REDACTED]"},
                                  "models": [{"canonical": m, "provider_id": "stub/" + agents.persona_name(m)} for m in models]}],
                "model_map": {m: "default" for m in models}}

    def tr_models(self) -> dict:
        try:
            prices = schema.load_prices()
            slugs = [entry["slug"] for group in ("tokenrouter", "judge") for entry in prices[group].values()]
        except Exception:               # the price file is not the stub's to vouch for
            slugs = []
        return {"data": [{"id": slug, "object": "model", "created": 0, "owned_by": "stub"} for slug in slugs],
                "object": "list", "success": True}

    def tr_pricing(self) -> dict:
        """The catalogue rows that give prices.yaml's prices under the ratio rule (input = model_ratio x 2)."""
        try:
            prices = schema.load_prices()
            entries = [entry for group in ("tokenrouter", "judge") for entry in prices[group].values()]
        except Exception:               # the price file is not the stub's to vouch for
            entries = []
        rows = []
        for e in entries:
            row = {"model_name": e["slug"], "model_ratio": e["input"] / 2, "completion_ratio": e["output"] / e["input"],
                   "cache_ratio": e.get("cache_read", e["input"]) / e["input"]}
            if "cache_write" in e:
                row["create_cache_ratio"] = e["cache_write"] / e["input"]
            rows.append(row)
        return {"data": rows, "success": True}

    # ----------------------------------------------------------------------- harnesses

    def _replay(self, route: str, key: str | None, body: dict):
        """The object first returned for this Idempotency-Key on this route, if any."""
        if not key:
            return None, None
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
        seen = self.idempotent.get((route, key))
        if seen is None:
            return None, digest
        if seen[0] != digest:
            raise ApiError(422, "idempotency_key_reused", "this Idempotency-Key was first sent with a different request body")
        return seen[1], digest

    def create_harness(self, body: dict, key: str | None) -> dict:
        with self.lock:
            if self.harness_keys_expired:               # tests only (module docstring): no key is looked up or kept
                key = None
            replay, digest = self._replay("harnesses", key, body)
            if replay is not None:
                return dict(replay)
            name, base, model = body.get("name"), body.get("base"), body.get("default_model")
            if not isinstance(name, str) or not name:
                raise ApiError(400, "invalid_request", "a harness needs a name")
            if base != BASE_ID:
                raise ApiError(400, "unknown_base", f"no base {base!r}: the stub serves `{BASE_ID}` only")
            if agents.persona_name(model) not in self.personas:
                raise ApiError(400, "model_not_found", f"no model {model!r} on base `{BASE_ID}` (have {self._models()})")
            model = agents.MODEL_PREFIX + agents.persona_name(model)
            harness_id = "chrn_" + hashlib.sha256(f"{name}|{base}|{model}".encode("utf-8")).hexdigest()[:32]
            if self.harness_keys_expired:               # ... and the harness is one more, with an id of its own
                harness_id = "chrn_" + _hex()
            harness = self.harnesses.setdefault(harness_id, {
                "id": harness_id, "name": name, "base": base, "baseLabel": base, "defaultModel": model,
                "systemPrompt": "", "maxStep": None, "timeoutSeconds": None, "createdAt": int(time.time() * 1000)})
            self.watchers.setdefault(harness_id, [])
            if key:
                self.idempotent[("harnesses", key)] = (digest, harness)
            return dict(harness)

    # ----------------------------------------------------------------------- turns

    def create_response(self, body: dict, key: str | None) -> dict:
        with self.lock:
            replay, digest = self._replay("responses", key, body)
            if replay is None:
                response = self._start(body)
                if key:
                    self.idempotent[("responses", key)] = (digest, response)
            else:
                response = replay
            snapshot = self._copy(response.body)    # a new turn: `queued`, read before its worker can move on
        if body.get("background"):
            return snapshot
        response.done.wait()
        return self.wire(response)

    def _start(self, body: dict) -> _Response:
        text = body.get("input")
        if not isinstance(text, str) or not text:
            raise ApiError(400, "invalid_request", "`input` must be a non-empty string: the stub takes plain text only")
        if body.get("stream"):
            raise ApiError(400, "invalid_request", "`stream` is not served; send `background: true` and poll")
        for name in ("max_step", "timeout_seconds"):
            value = body.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ApiError(400, "invalid_request", f"`{name}` must be a positive integer")
        metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        previous, harness_id = body.get("previous_response_id"), metadata.get("harness_id")
        if previous is not None:
            before = self.responses.get(previous) if isinstance(previous, str) else None
            if before is None:
                raise ApiError(404, "response_not_found", f"no response {previous!r}")
            session = before.session
            if session.busy:
                raise ApiError(409, "session_busy", "a turn is in progress on this session")
            if session.responses[-1] is not before:
                raise ApiError(400, "not_latest_response",
                               "previous_response_id is not this session's latest turn; the stub does not branch a session")
            if body.get("model") not in (None, session.model, agents.persona_name(session.model)):
                raise ApiError(400, "model_change", "a session keeps the model of its first turn")
            if harness_id not in (None, session.harness_id):
                raise ApiError(400, "harness_mismatch", "this session belongs to another harness")
            self._refuse(text, session.model)
        else:
            harness = self.harnesses.get(harness_id) if isinstance(harness_id, str) else None
            if harness is None:
                raise ApiError(404, "harness_not_found", f"no harness {harness_id!r}: POST /v1/harnesses first")
            persona = self.personas.get(agents.persona_name(body.get("model") or harness["defaultModel"]))
            if persona is None:
                raise ApiError(400, "model_not_found", f"no model {body.get('model')!r} (have {self._models()})")
            self._refuse(text, persona.model)
            session = _Session(harness, persona, self.root)
            self.sessions[session.id] = session
        response = _Response(session, text, previous)
        session.responses.append(response)
        self.responses[response.id] = response
        self._publish(response, "response.created", response=self._copy(response.body))
        worker = threading.Thread(target=self._run, args=(response, body), name="rj-stub-turn", daemon=True)
        self.workers = [w for w in self.workers if w.is_alive()] + [worker]
        worker.start()
        return response

    def _pause(self, seconds: float) -> bool:
        """Wait; True when the server is closing and the turn should stop where it is."""
        return self.closing.wait(seconds) if seconds > 0 else self.closing.is_set()

    def _run(self, response: _Response, body: dict) -> None:
        session = response.session
        try:
            if self._pause(self.turn_seconds * 0.2):
                return
            with self.lock:
                response.body["status"] = "in_progress"
                fault = self._take(("fail", "served_model", "ask"), response.user, session.model)
                more = self._take(("also",), response.user, session.model)
                response.body["served_model"] = fault["value"] if fault and fault["kind"] == "served_model" else session.model
                self._publish(response, "response.in_progress", response=self._copy(response.body))
            if fault and fault["kind"] == "fail":       # the provider failed: nothing was done and nothing is reported
                error = None if fault["value"] is None else {"code": "provider_error", "message": fault["value"]}
                turn = agents.Turn([], "failed", None, error, None, 1.0)
            else:
                turn = session.agent.turn(response.user, max_step=body.get("max_step"), timeout_seconds=body.get("timeout_seconds"),
                                          input_requests=bool(body.get("input_requests")),
                                          ask=str(fault["value"]) if fault and fault["kind"] == "ask" else None,
                                          also=list(more["value"]) if more else None)
            gap = self.turn_seconds * 0.8 / (len(turn.output) + 1)
            for index, item in enumerate(turn.output):
                if self._pause(gap):
                    return
                with self.lock:
                    response.body["output"].append(item)
                    self._publish(response, "response.output_item.done", output_index=index, item=item)
            if self._pause(gap):
                return
            with self.lock:
                response.turn = turn
                response.completed_at = _now()
                session.chunks.append(self._chunk(response, turn))
                self.balance_usd -= self.usd_per_turn
                if self.tr_usage_cents is not None:
                    self.tr_usage_cents += self.tr_cents_per_turn
                response.body.update(status=turn.status, error=turn.error, usage=turn.usage,
                                     incomplete_details={"reason": turn.reason} if turn.reason else None)
                self._publish(response, f"response.{turn.status}", response=self._copy(response.body))
        except Exception as e:          # a bug in a persona file or in the stub: the turn fails, the server lives
            with self.lock:
                response.completed_at = _now()
                response.body.update(status="failed", error={"code": "stub_bug", "message": f"{type(e).__name__}: {e}"})
                self._publish(response, "response.failed", response=self._copy(response.body))
        finally:
            with self.lock:
                if response.live:       # stopped by shutdown
                    response.completed_at = _now()
                    response.body.update(status="incomplete", incomplete_details={"reason": "interrupted"})
            response.done.set()

    @staticmethod
    def _copy(obj):
        return json.loads(json.dumps(obj))

    def wire(self, response: _Response) -> dict:
        with self.lock:
            return self._copy(response.body)

    def get_response(self, response_id: str) -> dict:
        with self.lock:
            response = self.responses.get(response_id)
            if response is None:
                raise ApiError(404, "response_not_found", f"no response {response_id!r}")
            return self._copy(response.body)

    def _chunk(self, response: _Response, turn: agents.Turn) -> bytes:
        """A finished turn as raw stream-json lines, the way a command-line agent prints them."""
        session = response.session
        lines = [{"type": "system", "subtype": "init", "session_id": session.id, "model": session.model,
                  "cwd": agents.WORKSPACE, "tools": agents.TOOLS},
                 {"type": "user", "message": {"role": "user", "content": response.user}}]
        for item in turn.output:
            if item["type"] == "function_call":
                content = [{"type": "tool_use", "id": item["call_id"], "name": item["name"], "input": json.loads(item["arguments"])}]
                lines.append({"type": "assistant", "message": {"role": "assistant", "content": content}})
            elif item["type"] == "function_call_output":
                content = [{"type": "tool_result", "tool_use_id": item["call_id"], "content": item["output"]}]
                lines.append({"type": "user", "message": {"role": "user", "content": content}})
            else:
                text = item["content"][0]["text"] if item["type"] == "message" else item["prompt"]
                lines.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
        lines.append({"type": "result", "subtype": "success" if turn.status == "completed" else turn.reason or turn.status,
                      "is_error": turn.status == "failed", "num_turns": sum(1 for i in turn.output if i["type"] == "function_call") + 1,
                      "duration_ms": int(turn.elapsed * 1000), "usage": turn.usage, "response_id": response.id})
        return "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines).encode("utf-8")

    # ----------------------------------------------------------------------- sessions

    def _session(self, sid: str) -> _Session:
        session = self.sessions.get(sid)
        if session is None:
            raise ApiError(404, "session_not_found", f"no session {sid!r}")
        return session

    def _idle(self, sid: str) -> _Session:
        session = self._session(sid)
        if session.busy:
            raise ApiError(409, "session_busy", "a turn is in progress on this session")
        return session

    @staticmethod
    def _file(session: _Session, parts: list[str]) -> Path:
        if not parts or any(p in ("", ".", "..") or "\\" in p or "\0" in p for p in parts):
            raise ApiError(400, "invalid_path", "a file path is relative to the workspace root, with no empty, `.` or `..` part")
        if any(p.startswith(".") for p in parts):
            raise ApiError(400, "invalid_path", "dot-paths are refused")
        target = session.dir.joinpath(*parts)
        if not target.resolve().is_relative_to(session.dir.resolve()):
            raise ApiError(400, "invalid_path", "the path leaves the workspace")
        return target

    def put_file(self, sid: str, parts: list[str], body: dict) -> dict:
        with self.lock:
            session = self._idle(sid)
            target = self._file(session, parts)
            try:
                data = base64.b64decode(body.get("content_b64"), validate=True)
            except (TypeError, ValueError, binascii.Error):
                raise ApiError(400, "invalid_request", "`content_b64` must be base64") from None
            if len(data) > PUT_LIMIT_BYTES:
                raise ApiError(413, "file_too_large", f"{len(data)} bytes is over the 4 MiB cap")
            if target.is_dir():
                raise ApiError(400, "invalid_path", "the path names a directory")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError as e:
                raise ApiError(400, "invalid_path", f"cannot write there: {e.strerror}") from None
            return {"session_id": sid, "path": "/".join(parts), "bytes": len(data), "written": True}

    def get_file(self, sid: str, parts: list[str]) -> dict:
        with self.lock:
            session = self._idle(sid)
            target = self._file(session, parts)
            if not target.is_file():
                raise ApiError(404, "file_not_found", f"no file {'/'.join(parts)!r} in this session")
            data = target.read_bytes()
            return {"session_id": sid, "path": "/".join(parts), "bytes": len(data),
                    "content_b64": base64.b64encode(data).decode("ascii")}

    def archive(self, sid: str) -> bytes:
        """The workspace as a zip: paths from its root, sorted, fixed timestamps, so equal trees give equal bytes."""
        with self.lock:
            session = self._idle(sid)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                files = {p.relative_to(session.dir).as_posix(): p for p in session.dir.rglob("*") if p.is_file() and not p.is_symlink()}
                for name in sorted(files):
                    info = zipfile.ZipInfo(name, ZIP_EPOCH)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    archive.writestr(info, files[name].read_bytes())
            return buffer.getvalue()

    def turns(self, sid: str) -> dict:
        with self.lock:
            session = self._session(sid)
            return self._copy({"session_id": sid, "turns": [r.row() for r in session.responses],
                               "last_response_id": session.responses[-1].id if session.responses else None})

    def trace(self, sid: str, query: dict) -> bytes:
        with self.lock:
            session = self._session(sid)
            try:
                n = int((query.get("chunk") or ["0"])[0])
            except ValueError:
                raise ApiError(400, "invalid_request", "`chunk` is a number, from 0") from None
            if not 0 <= n < len(session.chunks):
                raise ApiError(404, "chunk_not_found", f"no chunk {n}: this session has {len(session.chunks)}")
            return session.chunks[n]

    # ----------------------------------------------------------------------- the live tail

    def _publish(self, turn: _Response, kind: str, /, **payload) -> None:
        """Queue one event for everyone watching the turn's harness. Called with the lock held."""
        self._seq += 1
        event = {"session_id": turn.session.id, "response_id": turn.id, "type": kind, **payload}
        text = f"event: {kind}\nid: {self._seq}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        turn.events.append(text)
        for watcher in self.watchers.get(turn.session.harness_id, []):
            watcher.put(text)

    def watch(self, harness_id: str) -> tuple[queue.Queue, list[str]]:
        """Start watching a harness: its queue of coming events, and the events so far of its live turns."""
        with self.lock:
            if harness_id not in self.harnesses:
                raise ApiError(404, "harness_not_found", f"no harness {harness_id!r}")
            backlog = [e for s in self.sessions.values() if s.harness_id == harness_id and s.busy for e in s.responses[-1].events]
            watcher: queue.Queue = queue.Queue()
            self.watchers[harness_id].append(watcher)
            return watcher, backlog

    def unwatch(self, harness_id: str, watcher: queue.Queue) -> None:
        with self.lock:
            if watcher in self.watchers.get(harness_id, []):
                self.watchers[harness_id].remove(watcher)

    def close(self) -> None:
        """Stop every turn and end every event stream."""
        self.closing.set()
        with self.lock:
            watchers = [w for group in self.watchers.values() for w in group]
            workers = list(self.workers)
        for watcher in watchers:
            watcher.put(None)
        for worker in workers:
            worker.join(timeout=5)

    # ----------------------------------------------------------------------- the chat route: the judge, the adaptive writer

    def chat(self, body: dict) -> dict:
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ApiError(400, "invalid_request", "`messages` must hold at least one message")
        texts = []
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            if isinstance(content, str):
                texts.append(content)
        prompt = "\n".join(texts)
        if prompt.startswith(WRITER_HEAD):
            with self.lock:
                fault = self._take(WRITER_FAULTS, prompt, str(body.get("model") or ""))
            kind = fault["kind"] if fault else None
            if kind == "writer_http":
                raise ApiError(fault["value"][0], "stub_fault", fault["value"][1])
            if kind == "writer_slow":
                self._pause(float(fault["value"]))
            answer = {"reply": fault["value"]} if kind == "writer_text" else writer_content(prompt)
        else:
            answer = judge_content(prompt)
        content = json.dumps(answer, ensure_ascii=False)
        used = {"prompt_tokens": max(1, len(prompt) // 4), "completion_tokens": max(1, len(content) // 4)}
        return {"id": "chatcmpl-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:24], "object": "chat.completion",
                "created": _now(), "model": body.get("model") or "stub-judge",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {**used, "total_tokens": sum(used.values())}}

    # ----------------------------------------------------------------------- routing

    def handle(self, method: str, path: str, query: dict, headers, raw: bytes) -> tuple[int, str, bytes]:
        """One request -> (status, content type, body). The event stream is served by the handler itself."""
        parts = [unquote(p) for p in path.strip("/").split("/")]
        key = headers.get("Idempotency-Key")
        with self.lock:
            self.log.append((method, path))

        def body() -> dict:
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, "invalid_json", "the request body is not JSON") from None
            if not isinstance(parsed, dict):
                raise ApiError(400, "invalid_json", "the request body must be a JSON object")
            return parsed

        def reply(obj, status: int = 200) -> tuple[int, str, bytes]:
            return status, JSON, json.dumps(obj, ensure_ascii=False).encode("utf-8")

        match (method, parts):
            case ("GET", ["healthz"]) | ("GET", [""]):
                return reply({"ok": True, "service": "rj-stub", "personas": sorted(self.personas), "root": str(self.root)})
            case ("GET", ["v1", "balance"]):
                return reply(self.balance())
            case ("GET", ["v1", "bases"]):
                return reply(self.bases())
            case ("GET", ["v1", "models"]):
                return reply(self.models())
            case ("GET", ["v1", "integrations"]):
                return reply(self.integrations())
            case ("POST", ["v1", "harnesses"]):
                return reply(self.create_harness(body(), key))
            case ("GET", ["v1", "harnesses", harness_id]):
                with self.lock:
                    if harness_id not in self.harnesses:
                        raise ApiError(404, "harness_not_found", f"no harness {harness_id!r}")
                    return reply(dict(self.harnesses[harness_id]))
            case ("POST", ["v1", "responses"]):
                return reply(self.create_response(body(), key))
            case ("GET", ["v1", "responses", response_id]):
                return reply(self.get_response(response_id))
            case ("GET", ["v1", "sessions", sid, "turns"]):
                return reply(self.turns(sid))
            case ("GET", ["v1", "sessions", sid, "files", "archive"]):
                return 200, "application/zip", self.archive(sid)
            case ("PUT", ["v1", "sessions", sid, "files", *file_parts]):
                return reply(self.put_file(sid, file_parts, body()))
            case ("GET", ["v1", "sessions", sid, "files", *file_parts]):
                return reply(self.get_file(sid, file_parts))
            case ("GET", ["v1", "traces", sid, "events"]):
                return 200, "application/x-ndjson", self.trace(sid, query)
            case ("POST", ["tr", "chat", "completions"]):
                return reply(self.chat(body()))
            case ("GET", ["tr", "models"]):
                return reply(self.tr_models())
            case ("GET", ["tr", "api", "pricing"]):
                return reply(self.tr_pricing())
            case ("GET", ["tr", "dashboard", "billing", "subscription"]):
                return reply({"object": "billing_subscription", "has_payment_method": True, "access_until": TR_ACCESS_UNTIL})
            case ("GET", ["tr", "dashboard", "billing", "usage"]):
                with self.lock:
                    if self.tr_usage_cents is None:
                        raise ApiError(404, "not_found", "no usage figure for this key")
                    return reply({"object": "list", "total_usage": round(self.tr_usage_cents, 6)})
        raise ApiError(404, "not_found", f"no route {method} {path}")


# --------------------------------------------------------------------------- HTTP

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "rj-stub"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        threading.current_thread().name = "rj-stub-connection"
        self.server.track(self.connection, True)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.server.track(self.connection, False)

    def log_message(self, format: str, *args) -> None:
        if self.server.verbose:
            sys.stderr.write(f"rj stub: {format % args}\n")

    def _send(self, status: int, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, error: ApiError) -> None:
        self._send(error.status, JSON, json.dumps(error.body()).encode("utf-8"))

    def _serve(self) -> None:
        stub: Stub = self.server.stub
        url = urlsplit(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > BODY_LIMIT_BYTES or self.headers.get("Transfer-Encoding"):
            self.close_connection = True        # the body is not read, so this connection cannot be used again
            self._error(ApiError(413 if length > BODY_LIMIT_BYTES else 411, "invalid_request",
                                 "send a Content-Length of at most 8 MiB"))
            return
        raw = self.rfile.read(length) if length else b""
        parts = url.path.strip("/").split("/")
        try:
            if self.command == "GET" and len(parts) == 4 and parts[:2] == ["v1", "harnesses"] and parts[3] == "events":
                self._stream(stub, unquote(parts[2]))
                return
            status, content_type, data = stub.handle(self.command, url.path, parse_qs(url.query), self.headers, raw)
        except ApiError as e:
            self._error(e)
            return
        except Exception as e:                  # a bug in the stub: say so in the reply instead of dropping the connection
            self._error(ApiError(500, "stub_bug", f"{type(e).__name__}: {e}"))
            return
        self._send(status, content_type, data)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

    def _stream(self, stub: Stub, harness_id: str) -> None:
        """GET /v1/harnesses/{id}/events: one server-sent event per thing that happens on the harness's turns."""
        with stub.lock:
            stub.log.append((self.command, self.path))
        watcher, backlog = stub.watch(harness_id)
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b": connected\n\n" + "".join(backlog).encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    event = watcher.get(timeout=stub.keepalive)
                except queue.Empty:
                    event = ": keep-alive\n\n"
                if event is None or stub.closing.is_set():
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                self.wfile.write(event.encode("utf-8"))
                self.wfile.flush()
        except OSError:                         # the watcher went away
            pass
        finally:
            stub.unwatch(harness_id, watcher)


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128

    def __init__(self, address, stub: Stub, verbose: bool):
        self.stub, self.verbose = stub, verbose
        self._connections: set[socket.socket] = set()
        self._guard = threading.Lock()
        super().__init__(address, _Handler)

    def track(self, connection: socket.socket, opened: bool) -> None:
        with self._guard:
            (self._connections.add if opened else self._connections.discard)(connection)

    def open_connections(self) -> list[socket.socket]:
        with self._guard:
            return list(self._connections)

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exc_info()[1], OSError):      # a client that hung up
            return
        super().handle_error(request, client_address)


class StubServer:
    """A running stub. `url` is HR_BASE_URL, `tr_url` is TR_BASE_URL; `shutdown()` ends it and removes its files."""

    def __init__(self, port: int = DEFAULT_PORT, host: str = "127.0.0.1", *, turn_seconds: float | None = None,
                 personas: str | Path | None = None, keepalive: float = 15.0, verbose: bool = False):
        if host not in LOOPBACK:
            raise ValueError(f"the stub binds to this machine only (127.0.0.1), not {host!r}")
        if turn_seconds is None:
            turn_seconds = float(os.environ.get("RJ_STUB_TURN_SECONDS") or 0.05)
        self.root = Path(tempfile.mkdtemp(prefix="rj-stub-"))
        self._closed = threading.Lock()
        self._down = False
        try:
            self.stub = Stub(self.root, turn_seconds=turn_seconds, personas=personas, keepalive=keepalive)
            self._http = _HTTPServer(("127.0.0.1", port), self.stub, verbose)
        except BaseException:
            shutil.rmtree(self.root, ignore_errors=True)
            raise
        self.host, self.port = "127.0.0.1", self._http.server_address[1]
        self.url = f"http://{self.host}:{self.port}"
        self.tr_url = self.url + "/tr"
        self.stub.base_url = self.url
        self._thread = threading.Thread(target=self._http.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="rj-stub-http", daemon=True)
        self._thread.start()
        atexit.register(self.shutdown)

    def shutdown(self) -> None:
        """Stop accepting, end live turns and streams, close every connection, remove the session directories."""
        with self._closed:
            if self._down:
                return
            self._down = True
        self.stub.close()
        self._http.shutdown()
        self._http.server_close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            connections = self._http.open_connections()
            if not connections:
                break
            for connection in connections:      # idle keep-alive connections wait in a read: wake them
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            time.sleep(0.01)
        self._thread.join(timeout=3)
        shutil.rmtree(self.root, ignore_errors=True)
        atexit.unregister(self.shutdown)

    close = shutdown

    def __enter__(self) -> "StubServer":
        return self

    def __exit__(self, *exc_info) -> None:
        self.shutdown()


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1", **options) -> StubServer:
    """Start the stub in the background and return it. `port=0` takes a free port (read `server.port`).

    Options: `turn_seconds` (how long a turn stays live; default 0.05, or RJ_STUB_TURN_SECONDS),
    `personas` (a directory of extra persona files), `keepalive` (seconds between stream comments), `verbose`.
    """
    return StubServer(port, host, **options)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rj.stub.server",
                                     description="A local stand-in for HarnessRouter with scripted agents (plan section 6).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--turn-seconds", type=float, default=None,
                        help="how long a turn stays live (default 0.05, or RJ_STUB_TURN_SECONDS)")
    parser.add_argument("--personas", default=None, help="a directory of extra persona files")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args(argv)
    try:
        server = serve(args.port, turn_seconds=args.turn_seconds, personas=args.personas, verbose=args.verbose)
    except (OSError, agents.PersonaError) as e:
        print(f"rj stub: cannot start: {e}", file=sys.stderr)
        return 1

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    print(f"rj stub: HR_BASE_URL={server.url} TR_BASE_URL={server.tr_url} "
          f"personas: {', '.join(sorted(server.stub.personas))}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
