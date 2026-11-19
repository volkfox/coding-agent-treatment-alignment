"""Saved replies -> trace items, with every tool call classed. Plan section 5 (TraceItem) and section 9.

One place for this, because the effort signals, the judge's blinded transcript and the page all need
the same reading of the same replies. Shapes follow what HarnessRouter Cloud returned on 17 Nov 2026
(tests/fixtures/cloud/): message content is a list of {type: output_text, text}; a function_call's
arguments are a JSON string; outputs may all come after the calls, so they pair by call_id; paths are
absolute under /workspace; Kimi's Read takes `path`, Claude Code's takes `file_path`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rj import schema

SEND_DIR = re.compile(r"^(\d\d)-([a-z0-9_]+)\.s(\d+)$")


@dataclass(frozen=True)
class Tools:
    read: frozenset
    edit: frozenset
    shell: frozenset
    subagent: frozenset
    path_args: tuple
    command_args: tuple
    test: re.Pattern
    shell_edit: re.Pattern
    shell_read: re.Pattern
    self_check: re.Pattern
    revert: re.Pattern
    fail_output: re.Pattern
    shell_file: re.Pattern
    patch_file: re.Pattern
    root: str
    not_repo: tuple
    memory_path: re.Pattern
    raw: dict


@lru_cache(maxsize=4)
def load_tools(path: str = "grader/tools.yaml") -> Tools:
    d = schema.load_yaml(path)
    return Tools(
        read=frozenset(d["tools"]["READ"]), edit=frozenset(d["tools"]["EDIT"]), shell=frozenset(d["tools"]["SHELL"]),
        subagent=frozenset(d.get("subagent_tools") or []),
        path_args=tuple(d["path_args"]), command_args=tuple(d.get("command_args") or ["command"]),
        test=re.compile(d["shell"]["TEST"]), shell_edit=re.compile(d["shell"]["EDIT"]), shell_read=re.compile(d["shell"]["READ"]),
        self_check=re.compile(d["shell"]["SELF_CHECK"]), revert=re.compile(d["shell"]["REVERT"]),
        fail_output=re.compile(d.get("fail_output") or "(FAILED|Traceback|Error)"),
        shell_file=re.compile(d["shell_file"]), patch_file=re.compile(d["patch_file"], re.M),
        root=(d.get("workspace_root") or "/workspace").rstrip("/"), not_repo=tuple(d.get("not_repo") or []),
        memory_path=re.compile(d.get("memory_path") or r"^\.harness/home/.*memor(y|ies)/"), raw=d,
    )


def rel_path(path: str | None, tools: Tools) -> str | None:
    """A path as the repository names it: the workspace root and a leading ./ are dropped."""
    if not path or not isinstance(path, str):
        return None
    p = path.strip()
    if p == tools.root:
        return "."
    if p.startswith(tools.root + "/"):
        p = p[len(tools.root) + 1:]
    while p.startswith("./"):
        p = p[2:]
    return p or None


def in_repo(path: str | None, tools: Tools) -> bool:
    """False for the agent's own home, scratch space and anything outside the workspace."""
    if not path:
        return False
    if path.startswith("/") or path.startswith(".."):
        return False
    return not schema.matches_any(path, list(tools.not_repo))


def parse_args(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            got = json.loads(arguments)
            return got if isinstance(got, dict) else {"input": got}
        except ValueError:
            return {"input": arguments}
    return {}


def change_paths(args: dict) -> list[str]:
    """The files a patch-shaped edit lists under `changes`: each entry's `path`, and where it moves a file the
    `move_path` beside it or inside its `kind`. Codex's editing tool names its targets there and nowhere else
    (`{"changes": [{"path": "/workspace/x", "kind": {"type": "update", "move_path": null}, "diff": "..."}]}`,
    the planned run, 18 Nov 2026)."""
    out = []
    for change in args.get("changes") if isinstance(args.get("changes"), list) else []:
        if not isinstance(change, dict):
            continue
        kind = change.get("kind") if isinstance(change.get("kind"), dict) else {}
        out += [p for p in (change.get("path"), change.get("move_path"), kind.get("move_path")) if isinstance(p, str) and p]
    return out


def classify(name: str | None, args: dict, tools: Tools) -> dict:
    """{"cls", "path", "paths", "command", "self_check", "revert", "memory", "subagent"} for one tool call."""
    out = {"cls": "OTHER", "path": None, "paths": [], "command": None, "self_check": False, "revert": False,
           "memory": False, "subagent": name in tools.subagent}
    strings = [v for v in args.values() if isinstance(v, str)]
    patched = [m.strip() for s in strings for m in tools.patch_file.findall(s)]
    if name in tools.shell:
        command = next((args[k] for k in tools.command_args if isinstance(args.get(k), str)), None)
        if command is None and strings:
            command = strings[0]
        command = command or ""
        out["command"] = command
        bare = re.sub(r"(?<![\w/.-])\./", "", command.replace(tools.root + "/", ""))    # /workspace/x and ./x name x
        out["paths"] = [p for p in dict.fromkeys(rel_path(m, tools) for m in tools.shell_file.findall(bare)) if p]
        if tools.test.search(command):
            out["cls"] = "TEST"
        elif tools.shell_edit.search(command) or patched:
            out["cls"] = "EDIT"
        elif tools.shell_read.search(command):
            out["cls"] = "READ"
        out["self_check"] = bool(tools.self_check.search(command))
        out["revert"] = bool(tools.revert.search(command))
        if patched:
            out["paths"] = [p for p in dict.fromkeys(rel_path(m, tools) for m in patched) if p]
    elif name in tools.read or name in tools.edit:
        out["cls"] = "READ" if name in tools.read else "EDIT"
        first = next((args[k] for k in tools.path_args if isinstance(args.get(k), str)), None)
        named = [first, *change_paths(args)] if name in tools.edit else [first]
        out["paths"] = [p for p in dict.fromkeys([*(rel_path(n, tools) for n in named), *(rel_path(m, tools) for m in patched)]) if p]
    out["path"] = out["paths"][0] if out["paths"] else None
    out["memory"] = bool(out["path"] and tools.memory_path.search(out["path"]))
    # A read or an edit outside the repository (the agent's own notes, scratch files) is not work on the task.
    if out["cls"] in ("READ", "EDIT") and name not in tools.shell and out["path"] and not in_repo(out["path"], tools):
        out["cls"] = "OTHER"
    out["paths"] = [p for p in out["paths"] if in_repo(p, tools)] if out["cls"] != "OTHER" or name in tools.shell else out["paths"]
    return out


def text_of(content) -> str:
    """A message's text, whatever shape its content came in. A part whose text is null or missing adds nothing."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") if isinstance(p, dict) else "" if p is None else str(p) for p in content)
    return "" if content is None else str(content)


def _name(value) -> str | None:
    return value if isinstance(value, str) and value else None


def items_from_response(raw: dict, ep: str, send: int, start_seq: int = 1, tools: Tools | None = None) -> list[dict]:
    """One saved reply -> trace items in the order the reply lists them. A tool_call carries its paired output.

    Item types beyond the five seen on 17 Nov 2026 are kept, not dropped: a type ending in `_call` is a tool
    call (classed by its name like any other; its arguments may sit under `input`), one ending in
    `_call_output` is its output, and anything else is listed as a call of class OTHER named after its type,
    so that work a new item type carries is counted and shown. An entry of `output` that is no object is skipped.
    """
    tools = tools or load_tools()
    output = [it for it in raw.get("output") or [] if isinstance(it, dict)] if isinstance(raw.get("output"), list) else []
    outputs = {_name(it.get("call_id")): text_of(it.get("output")) for it in output if schema.is_call_output_item(it)}
    items, seq = [], start_seq
    for it in output:
        kind = it.get("type")
        base = {"seq": seq, "ep": ep, "send": send, "name": None, "call_id": None, "args": None, "path": None, "cls": None, "text": None}
        if kind == "message":
            items.append({**base, "kind": "message", "text": text_of(it.get("content"))})
        elif kind == "reasoning":
            items.append({**base, "kind": "reasoning", "text": text_of(it.get("summary") or it.get("content"))})
        elif schema.is_call_output_item(it):
            items.append({**base, "kind": "tool_output", "call_id": _name(it.get("call_id")), "text": text_of(it.get("output"))})
        elif schema.is_call_item(it):
            name = _name(it.get("name")) or (None if kind == "function_call" else kind)
            args = parse_args(it.get("arguments") if it.get("arguments") is not None else it.get("input"))
            c = classify(name, args, tools)
            items.append({**base, "kind": "tool_call", "name": name, "call_id": _name(it.get("call_id")), "args": args,
                          "path": c["path"], "cls": c["cls"], "paths": c["paths"], "command": c["command"],
                          "self_check": c["self_check"], "revert": c["revert"], "memory": c["memory"], "subagent": c["subagent"],
                          "output": outputs.get(_name(it.get("call_id")))})
        elif kind == "input_request":
            items.append({**base, "kind": "message", "text": text_of(it.get("prompt")), "input_request": True})
        else:                                           # a type nobody has seen yet: kept, classed OTHER
            items.append({**base, "kind": "tool_call", "name": str(kind), "call_id": _name(it.get("call_id")), "args": {},
                          "cls": "OTHER", "paths": [], "command": None, "self_check": False, "revert": False, "memory": False,
                          "subagent": False, "output": None, "unknown_type": True})
        seq += 1
    return items


def send_dirs(session_dir: str | Path) -> list[tuple[str, int, Path]]:
    """(entry point id, send number, directory) for every send of a session, in the order they were sent."""
    found = []
    for d in (Path(session_dir) / "turns").iterdir() if (Path(session_dir) / "turns").is_dir() else []:
        m = SEND_DIR.match(d.name)
        if m and d.is_dir():
            found.append((int(m.group(1)), int(m.group(3)), m.group(2), d))
    return [(ep, send, d) for _, send, ep, d in sorted(found, key=lambda x: (x[0], x[1]))]


def session_items(session_dir: str | Path, tools: Tools | None = None) -> list[dict]:
    """Every trace item of a session, numbered from 1 across all its sends."""
    tools = tools or load_tools()
    items: list[dict] = []
    for ep, send, d in send_dirs(session_dir):
        reply = d / "response.json"
        if reply.exists():
            items += items_from_response(json.loads(reply.read_text(encoding="utf-8")), ep, send, len(items) + 1, tools)
    return items


def tool_output_visible(items: list[dict]) -> bool:
    """True when at least one tool call came back with its output."""
    return any(i["kind"] == "tool_call" and i.get("output") is not None for i in items)
