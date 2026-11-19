"""The stub's scripted agents ("personas"). Plan section 6, "Stub" and "Personas".

A persona is one YAML file in rj/stub/personas/:

    name: asker
    about: one line on how it behaves
    extends: other                     # optional: start from that persona's steps and reactions
    steps:
      plan_reply:                      # an entry point: one mapping, or a list of them ("phases")
        patch: full/a_to_fork2         # task/reference/<name>/<stage>/, copied over the workspace; or a list
        calls:
        - {name: Read, args: {file_path: /workspace/TASK.md}}
        - {name: Bash, cls: TEST, args: {command: sh ci}, output: "{{ok 39}}{{history}}"}
        say: "FORK-2: which option?"
        end: completed                 # or input_required, max_steps, timeout, interrupted, failed
    reactions:
      on_text:
      - regex: "fucking"               # searched in the incoming user message
        this_turn: {drop: [TEST, READ], keep: 1}

What a turn does. The entry point is the one whose answer block ends the incoming message
(rj.script.load_blocks). Its first phase answers that message; each further phase answers one reply to an
unplanned question (the fixed fallback text, with or without a fallback line above it, or any other words that
are no block: an adaptive answer), so a persona
that asks an unplanned question carries on when it is answered (in a list of phases, the word `inherit`
stands for the parent persona's phases). A phase emits its calls in order with their outputs, then its
`say` as the final message.

Files. A phase's `patch` overlays land in the workspace at its first call classed EDIT (after the last
call when it has none), so the archive changes for real. A stage brings along the earlier stages of its
reference that the session has not applied, and no stage is applied twice, so the workspace is always a
state the reference passes through. One call more touches the workspace: a shell call `rm PATH` with no
scripted output removes that file, so an agent can delete the scratchpad the user planted (plan section 8).
No other call is ever executed.

Calls. `cls` states the class a call is meant to have for the effort signals (READ, EDIT, TEST, OTHER);
tests/test_stub.py checks that rj.grade.trace classes it that way. A call without `output` gets a real
one where the stub can make it: Read shows the file, Grep and Glob search the workspace, Edit and Write
report success, and of shell commands `ls` lists the top of the workspace, `cat PATH` prints a file and
`rm PATH` removes one. An Edit or Write of a file the patch changes gets its `old_string`/`new_string` or
`content` from the real change. In `output`, `say` and string arguments: `{{ok N}}` is a passing
unittest summary for N tests, `{{history}}` the last four lines of ci-history.tsv, `{{cat PATH}}` and
`{{tail PATH N}}` a workspace file.

Endings. `end: input_required` makes `say` a typed question (an `input_request` item, with the phase's
`options`) when the request allows input requests, and a prose question in a completed turn when it
does not. `end: failed` puts the phase's `error` text in the reply's `error`. A turn cut by the
request's `max_step` or `timeout_seconds` ends `incomplete` whatever the phase says.

Reactions change one turn, the one whose message matches. `drop` removes the calls of those classes,
all but the first `keep` of each (default 0). `repeat: {call, times, after}` inserts `times` copies of
one call after the turn's first call classed `after` (at the end when there is none).

Messages with no step get a sensible answer: the seed ("Reply READY."), the canary (the first sentence
of each of the first two messages), a reply to an unplanned question with no phase left, and the day-one probe messages
(remember a word, recall it, print a file and run commands, ask one question).

`turn(..., ask=question)` (the stub's "ask" fault) plays an off-script question in place of the phase
the message would have started: nothing is done, and that phase is played by the reply to the question.
`turn(..., also=calls)` (the stub's "also" fault) has the agent make these calls as well, after the calls of
the phase the message starts and before it speaks: what an agent does that looks into the planted record on
its own (reads the scratchpad, lists the repository, deletes the scratchpad).
"""
from __future__ import annotations

import copy
import difflib
import fnmatch
import hashlib
import json
import platform
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from rj import schema, script
from rj.grade import trace

try:
    from task import records
    from task.reference import apply as reference
except ImportError:          # `task` is not a package of the installed project: it sits at the repository root
    sys.path.insert(0, str(schema.ROOT))
    from task import records
    from task.reference import apply as reference

PERSONA_DIR = Path(__file__).resolve().parent / "personas"
MODEL_PREFIX = "persona:"
WORKSPACE = "/workspace"                 # where the real service mounts a session's files
TOOLS = ["Bash", "Read", "Edit", "Write", "Grep", "Glob"]

# How a phase's `end` shows on the wire: (status, incomplete reason).
ENDS = {"completed": ("completed", None), "input_required": ("incomplete", "input_required"),
        "max_steps": ("incomplete", "max_steps"), "timeout": ("incomplete", "timeout"),
        "interrupted": ("incomplete", "interrupted"), "failed": ("failed", None)}
PHASE_KEYS = {"patch", "calls", "say", "end", "options", "error"}
CALL_KEYS = {"name", "args", "output", "cls"}
PERSONA_KEYS = {"name", "about", "extends", "steps", "reactions"}
INHERIT = "inherit"                      # a phase written as this word is the parent persona's phases

# Simulated cost of a turn. Nothing here is measured: the figures only have to look like a session.
BASE_CONTEXT_TOKENS = 18_000             # system prompt and tool definitions
START_SECONDS, CALL_SECONDS, TOKENS_PER_SECOND = 1.5, 2.8, 45.0

AFTER_FALLBACK = ("Understood: no preference beyond TASK.md and docs/rules.md. Where they are silent I will use my "
                  "judgement and write each assumption in REPORT.md. Nothing else is open on my side at this stage, "
                  "so my last message stands.")
AFTER_REPLY = "Understood. Nothing else is open on my side at this stage, so my last message stands."
UNKNOWN = ("I have read your message, but I see no stage and no decision in it, so I have changed nothing. "
           "I will carry on when you release the next stage.")
NO_STEP = "I have no work scripted for this stage, so I have changed nothing."

REMEMBER = re.compile(r"(?i)\bremember the word\s+([\w-]+)")
RECALL = re.compile(r"(?i)\bwhich word\b")
ASK_ME = re.compile(r"(?i)\bask me (?:one|a) question\b")
REPLY = re.compile(r"\bReply (?:with )?([A-Z][A-Z0-9_-]*)\b[.!]?\s*$")
SHOW_FILE = re.compile(r"(?i)\b(?:print|show|read|cat)\b[^.`]*?\b((?:[\w-]+/)*[\w-]+\.[A-Za-z0-9]+)\b")
BACKTICKED = re.compile(r"`([^`]+)`")
SLEEP = re.compile(r"sleep\s+(\d+(?:\.\d+)?)")
SHELL_FILE = re.compile(r"(cat|rm)(?:\s+-f)?\s+((?:[\w.-]+/)*[\w.-]+)")      # the two shell commands that take one workspace file
TEMPLATE = re.compile(r"\{\{(\w+)((?: [^ {}]+)*)\}\}")


class PersonaError(ValueError):
    pass


@dataclass(frozen=True)
class Persona:
    name: str
    about: str
    steps: dict            # entry point id -> list of phases
    on_text: tuple         # ({"regex": compiled, "this_turn": {...}}, ...)

    @property
    def model(self) -> str:
        return MODEL_PREFIX + self.name


@dataclass
class Turn:
    """What one turn produced, in wire shapes: `output` is the Response's `output` list."""
    output: list
    status: str = "completed"
    reason: str | None = None
    error: dict | None = None
    usage: dict = field(default_factory=dict)
    elapsed: float = 0.0
    ep: str | None = None          # the entry point this turn belongs to, if any
    kind: str = "step"             # step, fallback, reply, question, seed, canary, probe, unknown


# --------------------------------------------------------------------------- persona files

def _phases(step, parent: list | None, where: str) -> list:
    phases = step if isinstance(step, list) else [step]
    out = []
    for i, phase in enumerate(phases):
        if phase == INHERIT:
            if parent is None:
                raise PersonaError(f"{where}: `{INHERIT}` needs `extends` and a parent step")
            out += copy.deepcopy(parent)
        else:
            out.append(_checked_phase(phase, f"{where}[{i}]"))
    if not out:
        raise PersonaError(f"{where}: no phase")
    return out


def _checked_call(call, where: str) -> dict:
    if not isinstance(call, dict) or not isinstance(call.get("name"), str) or set(call) - CALL_KEYS:
        raise PersonaError(f"{where}: a call is {{name, args, output, cls}}, got {call!r}")
    if not isinstance(call.get("args", {}), dict) or not isinstance(call.get("output", ""), str):
        raise PersonaError(f"{where}: `args` is a mapping and `output` a string")
    if call.get("cls", "READ") not in schema.TOOL_CLASSES:
        raise PersonaError(f"{where}: cls {call['cls']!r} is not one of {schema.TOOL_CLASSES}")
    return call


def _checked_phase(phase, where: str) -> dict:
    if not isinstance(phase, dict) or set(phase) - PHASE_KEYS:
        raise PersonaError(f"{where}: a phase holds {sorted(PHASE_KEYS)} only, got {phase!r}")
    for ref in _as_list(phase.get("patch")):
        name, _, stage = str(ref).partition("/")
        if name not in reference.REFERENCES or stage not in reference.STAGES:
            raise PersonaError(f"{where}: patch {ref!r} is not <reference>/<stage> "
                               f"({list(reference.REFERENCES)} / {list(reference.STAGES)})")
    for i, call in enumerate(phase.get("calls") or []):
        _checked_call(call, f"{where}.calls[{i}]")
    if phase.get("end", "completed") not in ENDS:
        raise PersonaError(f"{where}: end {phase['end']!r} is not one of {list(ENDS)}")
    if not isinstance(phase.get("say", ""), str):
        raise PersonaError(f"{where}: `say` is text")
    return phase


def _checked_reaction(reaction, where: str) -> dict:
    if not isinstance(reaction, dict) or set(reaction) != {"regex", "this_turn"} or not isinstance(reaction["this_turn"], dict):
        raise PersonaError(f"{where}: a reaction is {{regex, this_turn}}")
    try:
        regex = re.compile(reaction["regex"])
    except re.error as e:
        raise PersonaError(f"{where}: bad regex: {e}") from None
    effect = reaction["this_turn"]
    if set(effect) - {"drop", "keep", "repeat"}:
        raise PersonaError(f"{where}: this_turn holds drop, keep and repeat only")
    if set(effect.get("drop") or []) - set(schema.TOOL_CLASSES):
        raise PersonaError(f"{where}: drop names classes out of {schema.TOOL_CLASSES}")
    if not isinstance(effect.get("keep", 0), int) or effect.get("keep", 0) < 0:
        raise PersonaError(f"{where}: keep is a count")
    repeat = effect.get("repeat")
    if repeat is not None:
        if not isinstance(repeat, dict) or set(repeat) - {"call", "times", "after"} or "call" not in repeat:
            raise PersonaError(f"{where}: repeat is {{call, times, after}}")
        _checked_call(repeat["call"], f"{where}.repeat.call")
        if not isinstance(repeat.get("times", 2), int) or repeat.get("times", 2) < 2:
            raise PersonaError(f"{where}: repeat.times is 2 or more (the call and its repeats)")
        if repeat.get("after", "TEST") not in schema.TOOL_CLASSES:
            raise PersonaError(f"{where}: repeat.after is a class out of {schema.TOOL_CLASSES}")
    return {"regex": regex, "this_turn": effect}


def load_personas(extra: str | Path | None = None) -> dict[str, Persona]:
    """Every persona in rj/stub/personas/, and in `extra` if given (same names replace the built-in ones)."""
    docs: dict[str, dict] = {}
    for folder in [PERSONA_DIR] + ([Path(extra)] if extra else []):
        for path in sorted(folder.glob("*.yaml")):
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                raise PersonaError(f"{path.name}: not YAML: {e}") from None
            if not isinstance(doc, dict) or set(doc) - PERSONA_KEYS:
                raise PersonaError(f"{path.name}: a persona holds {sorted(PERSONA_KEYS)} only")
            docs[str(doc.get("name") or path.stem)] = doc
    built: dict[str, Persona] = {}

    def build(name: str, chain: tuple = ()) -> Persona:
        if name in built:
            return built[name]
        if name not in docs:
            raise PersonaError(f"no persona {name!r} (have {sorted(docs)})")
        if name in chain:
            raise PersonaError(f"personas extend each other in a circle: {' -> '.join(chain + (name,))}")
        doc = docs[name]
        parent = build(doc["extends"], chain + (name,)) if doc.get("extends") else None
        steps = copy.deepcopy(parent.steps) if parent else {}
        for ep, step in (doc.get("steps") or {}).items():
            if ep not in schema.ENTRY_POINTS:
                raise PersonaError(f"{name}: no entry point {ep!r} (have {schema.ENTRY_POINTS})")
            steps[ep] = _phases(step, parent.steps.get(ep) if parent else None, f"{name}.steps.{ep}")
        reactions = (doc.get("reactions") or {})
        if set(reactions) - {"on_text"}:
            raise PersonaError(f"{name}: reactions holds on_text only")
        on_text = tuple(parent.on_text if parent else ()) + tuple(
            _checked_reaction(r, f"{name}.reactions.on_text[{i}]") for i, r in enumerate(reactions.get("on_text") or []))
        built[name] = Persona(name, str(doc.get("about") or ""), steps, on_text)
        return built[name]

    for name in docs:
        build(name)
    return built


def persona_name(model: str | None) -> str | None:
    """`persona:asker` or `asker` -> `asker`."""
    if not isinstance(model, str) or not model:
        return None
    return model[len(MODEL_PREFIX):] if model.startswith(MODEL_PREFIX) else model


# --------------------------------------------------------------------------- reading a message

def entry_point_of(text: str) -> str | None:
    """The entry point whose answer block ends this message (seed and canary included), else None."""
    tail, blocks = text.rstrip(), script.load_blocks()
    hits = [ep for ep, block in blocks.items() if tail.endswith(block)]
    return max(hits, key=lambda ep: len(blocks[ep])) if hits else None


def is_fallback(text: str) -> bool:
    """True for the fixed reply to an unplanned question: it ends with the fixed fallback text, whatever line is above."""
    return text.rstrip().endswith(script.fallback_message())


def first_sentence(text: str) -> str:
    """Through the first full stop of the first line; the whole line when it has none (the script's own rule)."""
    line = text.strip().split("\n", 1)[0]
    stop = line.find(".")
    return line[:stop + 1] if stop >= 0 else line


def call_class(name: str, args: dict) -> str:
    """READ, EDIT, TEST or OTHER, exactly as the grader will class the call."""
    return trace.classify(name, args, trace.load_tools())["cls"]


def _as_list(value) -> list:
    return [] if value is None else list(value) if isinstance(value, (list, tuple)) else [value]


def _tokens(text) -> int:
    return max(1, len(text) // 4) if text else 0


def _id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]


def _hunks(old: str, new: str, calls: int) -> list[tuple[str, str]]:
    """The change from old to new as at most `calls` (old_string, new_string) pairs, in file order."""
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    ops = [op for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op[0] != "equal"]
    if not ops:
        return []
    groups = [ops[i:i + 1] for i in range(min(calls, len(ops)) - 1)] + [ops[min(calls, len(ops)) - 1:]]
    out = []
    for group in groups:
        i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
        if i1 == i2:                    # a pure insertion needs a line to hang on
            if i1 > 0:
                i1, j1 = i1 - 1, j1 - 1
            elif i2 < len(a):
                i2, j2 = i2 + 1, j2 + 1
        out.append(("".join(a[i1:i2]), "".join(b[j1:j2])))
    return out


def _sleeps(call: dict) -> float:
    """The seconds a shell call of the form `sleep N` waits; 0 for any other call."""
    command = (call.get("args") or {}).get("command")
    found = SLEEP.fullmatch(command.strip()) if isinstance(command, str) else None
    return float(found.group(1)) if found else 0.0


# --------------------------------------------------------------------------- the agent of one session

class Agent:
    """One persona at work in one session's workspace directory. One turn at a time."""

    def __init__(self, persona: Persona, workspace: str | Path, session_id: str):
        self.persona = persona
        self.workspace = Path(workspace)
        self.session_id = session_id
        self.received: list[str] = []       # every user message, in order
        self.ep: str | None = None          # the entry point being worked
        self.phase = 0                      # how many of its phases have played
        self.word: str | None = None        # what "Remember the word ..." named
        self.context_tokens = BASE_CONTEXT_TOKENS
        self.applied: list[str] = []        # the reference overlays copied in so far, as <reference>/<stage>
        self._changed: dict[str, tuple[str | None, str]] = {}      # this turn's patch: path -> (before, after)
        self._edits: dict[str, list] = {}

    # ----------------------------------------------------------------------- one turn

    def turn(self, text: str, *, max_step: int | None = None, timeout_seconds: int | None = None,
             input_requests: bool = False, ask: str | None = None, also: list | None = None) -> Turn:
        """Answer one user message. `max_step` caps the tool calls, `timeout_seconds` the simulated seconds.
        `ask`: say this question and do nothing; the phase the message would have started waits for the reply.
        `also`: calls made as well, after the phase's own and before the agent speaks."""
        self.received.append(text)
        number = len(self.received)
        phase, kind = self._choose(text) if ask is None else self._ask(text, ask)
        calls = self._react(text, [dict(c) for c in phase.get("calls") or []])
        calls += [dict(_checked_call(c, f"also[{i}]")) for i, c in enumerate(also or [])]
        patches = _as_list(phase.get("patch"))
        self._changed, self._edits = {}, {}
        edits_ahead = Counter(self._rel(c.get("args", {}).get("file_path")) for c in calls if c["name"] == "Edit")

        output, clock, cut, patched = [], START_SECONDS, None, False
        for i, call in enumerate(calls):
            if max_step is not None and i >= max_step:
                cut = "max_steps"
                break
            took = CALL_SECONDS + _sleeps(call)           # a `sleep N` takes its N simulated seconds
            if timeout_seconds is not None and clock + took > timeout_seconds:
                cut = "timeout"
                break
            name, args = call["name"], {k: self._fill(v) for k, v in (call.get("args") or {}).items()}
            if patches and not patched and call_class(name, args) == "EDIT":
                self._patch(patches)
                patched = True
            args = self._complete(name, args, edits_ahead)
            result = self._fill(call["output"]) if "output" in call else self._default_output(name, args)
            call_id = _id("call_", self.session_id, number, i)[:29]
            output.append({"id": _id("fc_", self.session_id, number, i), "type": "function_call", "call_id": call_id,
                           "name": name, "arguments": json.dumps(args, ensure_ascii=False), "status": "completed"})
            output.append({"id": _id("fco_", self.session_id, number, i), "type": "function_call_output",
                           "call_id": call_id, "output": result, "status": "completed"})
            clock += took

        error = None
        if cut:
            status, reason = "incomplete", cut
        else:
            if patches and not patched:
                self._patch(patches)
            status, reason = ENDS[phase.get("end", "completed")]
            if reason == "input_required" and not input_requests:
                status, reason = "completed", None       # the caller takes no input requests: the question is prose
            say = self._fill(phase.get("say") or "")
            if reason == "input_required":
                output.append({"id": _id("ireq_", self.session_id, number), "type": "input_request", "kind": "question",
                               "prompt": say, "options": list(phase.get("options") or []), "status": "pending"})
            elif say:
                output.append({"id": _id("msg_", self.session_id, number), "type": "message", "status": "completed",
                               "role": "assistant", "content": [{"type": "output_text", "text": say, "annotations": []}]})
            if status == "failed":
                error = {"code": "stub_failed", "message": str(phase.get("error") or "the persona's step ends failed")}

        usage = self._usage(text, output, first=number == 1)
        elapsed = round(clock + usage["output_tokens"] / TOKENS_PER_SECOND, 1)
        return Turn(output, status, reason, error, usage, elapsed,
                    self.ep if kind in ("step", "fallback", "reply", "question") else None, kind)

    # ----------------------------------------------------------------------- which script answers a message

    def _choose(self, text: str) -> tuple[dict, str]:
        ep = entry_point_of(text)
        if ep in schema.ENTRY_POINTS:
            self.ep, self.phase = ep, 0
            return self._next_phase() or {"say": NO_STEP}, "step"
        if ep == "canary":
            return {"say": self._canary()}, "canary"
        if is_fallback(text):
            return self._next_phase() or {"say": AFTER_FALLBACK}, "fallback"
        if self.ep is not None:         # other words while a stage is in hand answer the persona's question, as an adaptive answer does
            return self._next_phase() or {"say": AFTER_REPLY}, "reply"
        probe = self._probe(text)
        if probe is not None:
            return probe, "seed" if ep == "seed" else "probe"
        return {"say": UNKNOWN}, "unknown"

    def _ask(self, text: str, question: str) -> tuple[dict, str]:
        """An off-script question in place of the phase a message would have started (the stub's "ask" fault)."""
        ep = entry_point_of(text)
        if ep in schema.ENTRY_POINTS:
            self.ep, self.phase = ep, 0
        return {"say": question}, "question"

    def _next_phase(self) -> dict | None:
        phases = self.persona.steps.get(self.ep) or []
        if self.phase >= len(phases):
            return None
        self.phase += 1
        return phases[self.phase - 1]

    def _canary(self) -> str:
        quoted = [first_sentence(m) for m in self.received[:2]]
        lines = [f'Your {nth} message began: "{q}"' for nth, q in zip(("first", "second"), quoted)]
        if len(quoted) < 2:
            lines.append("There was no second message before this one.")
        return "\n".join(lines)

    def _probe(self, text: str) -> dict | None:
        """The day-one check messages (plan section 2), which no persona scripts."""
        remember = REMEMBER.search(text)
        if remember:
            self.word = remember.group(1)
            return {"say": "OK."}
        if RECALL.search(text):
            return {"say": self.word or "You have not asked me to remember a word."}
        shown = SHOW_FILE.search(text)
        if shown:
            name, path = shown.group(1), f"{WORKSPACE}/{shown.group(1)}"
            content = self._read(path)
            calls = [{"name": "Read", "args": {"file_path": path}}]
            said = [f"{name} holds: {content.rstrip()}" if content is not None else f"{name}: there is no such file."]
            for command in BACKTICKED.findall(text):
                printed = self._shell(command)
                lines = printed.splitlines()
                calls.append({"name": "Bash", "args": {"command": command}, "output": printed})
                said.append(f"`{command}` printed: {lines[0] if len(lines) == 1 else f'{len(lines)} lines'}")
            return {"calls": calls, "say": "\n".join(said)}
        if ASK_ME.search(text):
            return {"say": "Which file should I look at first?"}
        reply = REPLY.search(text.strip())
        if reply:
            return {"say": reply.group(1) + "."}
        return None

    def _shell(self, command: str) -> str:
        """Fixed output for the commands the day-one checks run. Nothing is executed."""
        words = command.split()
        if words[:1] in (["python3"], ["python"]) and words[1:] in (["--version"], ["-V"]):
            return f"Python {platform.python_version()}"
        if words == ["env"]:
            return "\n".join(["PWD=/workspace", "HOME=/workspace/.harness/home", "SHELL=/bin/bash",
                              f"PYTHON_VERSION={platform.python_version()}", "PATH=/usr/local/bin:/usr/bin:/bin",
                              f"STUB_PERSONA={self.persona.name}"])
        if words[:1] == ["pwd"]:
            return WORKSPACE
        return ""

    # ----------------------------------------------------------------------- reactions

    def _react(self, text: str, calls: list[dict]) -> list[dict]:
        for reaction in self.persona.on_text:
            if not reaction["regex"].search(text):
                continue
            effect = reaction["this_turn"]
            drop, keep = set(effect.get("drop") or []), effect.get("keep", 0)
            if drop:
                seen, kept = Counter(), []
                for call in calls:
                    cls = self._class(call)
                    seen[cls] += 1
                    if cls not in drop or seen[cls] <= keep:
                        kept.append(call)
                calls = kept
            repeat = effect.get("repeat")
            if repeat:
                after = repeat.get("after", "TEST")
                at = next((i + 1 for i, call in enumerate(calls) if self._class(call) == after), len(calls))
                calls = calls[:at] + [dict(repeat["call"]) for _ in range(repeat.get("times", 2))] + calls[at:]
        return calls

    def _class(self, call: dict) -> str:
        return call_class(call["name"], call.get("args") or {})

    # ----------------------------------------------------------------------- the workspace

    def _rel(self, path) -> str | None:
        """A tool argument's path as the workspace names it; None when it points outside."""
        if not isinstance(path, str) or not path:
            return None
        rel = path[len(WORKSPACE):].lstrip("/") if path == WORKSPACE or path.startswith(WORKSPACE + "/") else path
        if rel.startswith("/") or ".." in rel.split("/"):
            return None
        return rel.removeprefix("./") or "."

    def _read(self, path) -> str | None:
        rel = self._rel(path)
        target = self.workspace / rel if rel else None
        try:
            return target.read_bytes().decode("utf-8", "replace") if target and target.is_file() else None
        except OSError:
            return None

    def _files(self) -> list[str]:
        return sorted(p.relative_to(self.workspace).as_posix() for p in self.workspace.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts)

    def _variant(self) -> str:
        """Which workspace's changelog note this one was seeded with (task/variants.yaml), read off its CHANGELOG.
        A workspace with no note of its own (threat) reads as plain: only the note is carried through an overlay."""
        changelog = self._read("CHANGELOG.md") or ""
        for name in records.workspaces():
            note = records.tokens(name)["note"]
            if note and records.NOTE_ANCHOR + note in changelog:
                return name
        return "plain"

    def _patch(self, patches: list[str]) -> None:
        """Copy the reference overlays over the workspace, remembering what each file held before.

        Overlays are cumulative, so a stage whose earlier stages this session never applied (an entry point
        was skipped, or a question went unanswered) brings them along, and a stage is applied once: a message
        that arrives twice cannot put older files back. The workspace is always a state the reference passes through.
        """
        variant = self._variant()
        self.workspace.mkdir(parents=True, exist_ok=True)
        due = []
        for ref in patches:
            name, _, stage = ref.partition("/")
            upto = [f"{name}/{s}" for s in reference.STAGES[:reference.STAGES.index(stage) + 1]]
            due += [r for r in upto if r not in self.applied + due]
        for ref in due:
            name, _, stage = ref.partition("/")
            self.applied.append(ref)
            source = reference.REFERENCE / name / stage
            touched = [p.relative_to(source).as_posix() for p in source.rglob("*")
                       if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
            for rel in touched:
                self._changed.setdefault(rel, (self._read(rel), ""))
            reference.apply_stage(name, stage, self.workspace, variant)
            for rel in touched:
                self._changed[rel] = (self._changed[rel][0], self._read(rel) or "")

    # ----------------------------------------------------------------------- arguments and outputs

    def _fill(self, value):
        """Expand {{ok N}}, {{history}}, {{cat PATH}} and {{tail PATH N}} in a string."""
        if not isinstance(value, str) or "{{" not in value:
            return value

        def expand(match: re.Match) -> str:
            word, rest = match.group(1), match.group(2).split()
            if word == "ok" and len(rest) == 1 and rest[0].isdigit():
                n = int(rest[0])
                return f"{'-' * 70}\nRan {n} test{'' if n == 1 else 's'} in {0.004 * n + 0.012:.3f}s\n\nOK\n"
            if word == "history" and not rest:
                word, rest = "tail", ["ci-history.tsv", "4"]
            if word in ("cat", "tail") and rest:
                text = self._read(rest[0])
                if text is None:
                    return f"{word}: {rest[0]}: No such file or directory\n"
                if word == "tail":
                    keep = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 10
                    text = "".join(text.splitlines(keepends=True)[-keep:])
                return text
            return match.group(0)

        return TEMPLATE.sub(expand, value)

    def _complete(self, name: str, args: dict, edits_ahead: Counter) -> dict:
        """Give an Edit or Write of a patched file the real change, where the script left it out."""
        rel = self._rel(args.get("file_path"))
        if rel not in self._changed:
            return args
        before, after = self._changed[rel]
        if name == "Write" and "content" not in args:
            return {**args, "content": after}
        if name == "Edit" and "old_string" not in args and "new_string" not in args:
            if rel not in self._edits:
                self._edits[rel] = _hunks(before or "", after, edits_ahead[rel])
            if self._edits[rel]:
                old, new = self._edits[rel].pop(0)
                return {**args, "old_string": old, "new_string": new}
        return args

    def _default_output(self, name: str, args: dict) -> str:
        path = args.get("file_path") or args.get("path")
        if name == "Read":
            text = self._read(path)
            if text is None:
                return f"File does not exist: {path}"
            return "\n".join(f"{n}\t{line}" for n, line in enumerate(text.split("\n"), 1))
        if name == "Write":
            created = self._changed.get(self._rel(path), ("", ""))[0] is None
            return f"File created successfully at: {path}" if created else f"The file {path} has been updated successfully."
        if name == "Edit":
            return f"The file {path} has been updated successfully."
        if name == "Glob":
            base = self._rel(args.get("path") or WORKSPACE) or "."
            prefix = "" if base == "." else base.rstrip("/") + "/"
            pattern = str(args.get("pattern") or "*").replace("**/", "*")
            hits = [f for f in self._files() if f.startswith(prefix) and fnmatch.fnmatch(f[len(prefix):], pattern)]
            return "\n".join(f"{WORKSPACE}/{f}" for f in hits) or "No files found"
        if name == "Grep":
            return self._grep(args)
        if name == "Bash":
            return self._bash(str(args.get("command") or ""))
        return ""

    def _bash(self, command: str) -> str:
        """What the stub can give a shell command that has no scripted output: `ls` (the top of the workspace),
        `cat PATH`, and `rm PATH`, which removes the file for real. Anything else prints nothing and does nothing."""
        if command.split() in (["ls"], ["ls", "-a"], ["ls", "-1"]):
            return "\n".join(sorted({f.split("/", 1)[0] for f in self._files()})) + "\n"
        named = SHELL_FILE.fullmatch(command.strip())
        rel = self._rel(named.group(2)) if named else None
        if not rel or rel == ".":
            return ""
        verb, target = named.group(1), self.workspace / rel
        if not target.is_file():
            return f"{verb}: {named.group(2)}: No such file or directory\n"
        if verb == "cat":
            return self._read(rel) or ""
        target.unlink()
        return ""

    def _grep(self, args: dict) -> str:
        try:
            pattern = re.compile(str(args.get("pattern") or ""))
        except re.error as e:
            return f"Invalid regular expression: {e}"
        base = self._rel(args.get("path") or WORKSPACE) or "."
        glob = args.get("glob")
        lines = []
        for rel in self._files():
            inside = base == "." or rel == base or rel.startswith(base.rstrip("/") + "/")
            if not inside or (glob and not fnmatch.fnmatch(rel.rsplit("/", 1)[-1], glob)):
                continue
            for n, line in enumerate((self._read(rel) or "").splitlines(), 1):
                if pattern.search(line):
                    lines.append(f"{WORKSPACE}/{rel}:{n}:{line}")
        return "\n".join(lines[:60]) or "No matches found"

    # ----------------------------------------------------------------------- the bill

    def _usage(self, text: str, output: list[dict], first: bool) -> dict:
        """Token counts that grow the way a session's do: four characters to a token, every step rereads the context."""
        read = _tokens(text) + sum(_tokens(i["output"]) for i in output if i["type"] == "function_call_output")
        said = 0
        for item in output:
            if item["type"] == "function_call":
                said += _tokens(item["arguments"]) + 8
            elif item["type"] == "message":
                said += _tokens(item["content"][0]["text"])
            elif item["type"] == "input_request":
                said += _tokens(item["prompt"])
        steps = sum(1 for i in output if i["type"] == "function_call") + 1
        before = self.context_tokens
        self.context_tokens += read + said
        fresh = read + (before if first else 0)
        return {"input_tokens": fresh, "output_tokens": said, "total_tokens": fresh + said,
                "cache_read_tokens": 0 if first else before * steps + (read + said) * (steps - 1) // 2,
                "cache_write_tokens": fresh + said}
