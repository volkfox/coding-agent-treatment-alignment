"""Run untrusted code (an agent's workspace, a hidden check, a probe) with no network and a time limit.

Plan sections 5 and 10. Two modes:
  docker      `python:3.12-slim --network none`, the workspace mounted at /work  (preferred)
  subprocess  the same command on the host with a stripped environment           (fallback, no isolation)
`RJ_SANDBOX=docker|subprocess|auto` picks one; auto uses Docker when its daemon and the image are there.

Commands name their mounts with placeholders, so one argv works in both modes:
    run(["python3", "-I", "{probes}/part_a_green.py", "{work}"], workdir, ro_mounts={"probes": probes_dir})
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

IMAGE = "python:3.12-slim"
PROBE_TIMEOUT = 60


@dataclass
class RunResult:
    returncode: int | None      # None when the run was killed for time
    stdout: str
    stderr: str
    timed_out: bool
    mode: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@lru_cache(maxsize=1)
def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        up = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=20)
        return up.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pick_mode(mode: str | None = None) -> str:
    mode = mode or os.environ.get("RJ_SANDBOX", "auto")
    if mode == "auto":
        return "docker" if docker_available() else "subprocess"
    if mode not in ("docker", "subprocess"):
        raise ValueError(f"unknown sandbox mode {mode!r}")
    return mode


def run(argv: list[str], workdir: str | Path, *, timeout: float = 120, mode: str | None = None,
        ro_mounts: dict[str, str | Path] | None = None, env: dict[str, str] | None = None) -> RunResult:
    """Run argv with the workspace as the working directory. Never raises for a failing or slow command."""
    workdir = Path(workdir).resolve()
    ro_mounts = {k: Path(v).resolve() for k, v in (ro_mounts or {}).items()}
    mode = pick_mode(mode)
    started = time.monotonic()
    if mode == "docker":
        result = _run_docker(argv, workdir, timeout, ro_mounts, env or {})
    else:
        result = _run_subprocess(argv, workdir, timeout, ro_mounts, env or {})
    result.seconds = round(time.monotonic() - started, 3)
    return result


def _fill(argv: list[str], places: dict[str, str]) -> list[str]:
    out = []
    for a in argv:
        for name, value in places.items():
            a = a.replace("{" + name + "}", value)
        out.append(a)
    return out


def _run_docker(argv, workdir, timeout, ro_mounts, env) -> RunResult:
    name = f"rj-sbx-{uuid.uuid4().hex[:12]}"
    places = {"work": "/work", **{k: f"/mnt/{k}" for k in ro_mounts}}
    cmd = ["docker", "run", "--rm", "--name", name, "--network", "none", "--memory", "1g", "--cpus", "1",
           "--pids-limit", "256", "--tmpfs", "/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp",
           "-v", f"{workdir}:/work", "-w", "/work"]
    for k, p in ro_mounts.items():
        cmd += ["-v", f"{p}:/mnt/{k}:ro"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [IMAGE, *_fill(argv, places)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        return RunResult(p.returncode, p.stdout, p.stderr, False, "docker", 0.0)
    except subprocess.TimeoutExpired as e:
        subprocess.run(["docker", "kill", name], capture_output=True)
        return RunResult(None, _text(e.stdout), _text(e.stderr), True, "docker", 0.0)


def _run_subprocess(argv, workdir, timeout, ro_mounts, env) -> RunResult:
    places = {"work": str(workdir), **{k: str(p) for k, p in ro_mounts.items()}}
    # `python3` must be this interpreter (3.12), not whatever the host puts first on PATH.
    clean = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
             "HOME": str(workdir), "LANG": "C.UTF-8", **env}
    try:
        p = subprocess.Popen(_fill(argv, places), cwd=workdir, env=clean, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True)
    except OSError as e:
        return RunResult(127, "", str(e), False, "subprocess", 0.0)
    try:
        out, err = p.communicate(timeout=timeout)
        return RunResult(p.returncode, out, err, False, "subprocess", 0.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = p.communicate()
        return RunResult(None, out, err, True, "subprocess", 0.0)


def _text(b) -> str:
    if b is None:
        return ""
    return b if isinstance(b, str) else b.decode("utf-8", "replace")


def run_probe(name: str, workdir: str | Path, probes_dir: str | Path, *, timeout: float = PROBE_TIMEOUT,
              mode: str | None = None) -> dict:
    """Run task/hidden/probes/<name>.py on a workspace. Returns {"ok": bool, "option": "A"|"B"|"C"|"other"|None}.

    A timeout, a crash or unreadable output is `ok: false`; the reason is kept under "error".
    """
    r = run(["python3", "-I", "{probes}/" + name + ".py", "{work}"], workdir, timeout=timeout, mode=mode,
            ro_mounts={"probes": probes_dir})
    if r.timed_out:
        return {"ok": False, "option": None, "error": "timeout"}
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict) and "ok" in got:
            return {"ok": bool(got["ok"]), "option": got.get("option")}
    return {"ok": False, "option": None, "error": (r.stderr or r.stdout).strip()[-300:] or f"exit {r.returncode}"}
