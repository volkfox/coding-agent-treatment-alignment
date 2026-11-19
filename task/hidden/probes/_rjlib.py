"""Shared by run_checks.py and the probes: copy a workspace, restore the protected files, run the harness.

Nothing here imports from the workspace. Everything that does runs in a child started with -I (see _rjharness.py).
"""
import json
import os
import pathlib
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
PRISTINE = HERE / "pristine"                      # every protected file, at its workspace path
HARNESS = HERE / "_rjharness.py"
PART_A = "tests/test_part_a.py"
PART_B = "tests/test_part_b.py"
SKIP_NAMES = ("__pycache__", ".git")


def _skip(folder, names):
    """Leave out caches, and anything that is not a file, a directory or a link (a FIFO would block the copy)."""
    out = []
    for name in names:
        try:
            mode = os.lstat(os.path.join(folder, name)).st_mode
        except OSError:
            out.append(name)
            continue
        if name in SKIP_NAMES or name.endswith(".pyc") or not (
                stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            out.append(name)
    return out


def scratch_dir(workspace, prefix):
    """A fresh temporary directory outside the workspace (tempfile falls back to the current directory)."""
    workspace = pathlib.Path(workspace).resolve()
    for base in (tempfile.gettempdir(), "/tmp", "/var/tmp"):
        base = pathlib.Path(base).resolve()
        if base == workspace or workspace in base.parents:
            continue
        try:
            return pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir=base))
        except OSError:
            continue
    raise OSError("no temporary directory outside the workspace")


def seed_tests():
    return sorted(f"tests/{p.name}" for p in (PRISTINE / "tests").glob("test_seed_*.py"))


def copy_workspace(src, dst):
    """Copy a workspace; never follows symlinks out of it, never raises for a bad entry."""
    try:
        shutil.copytree(src, dst, symlinks=True, ignore=_skip, ignore_dangling_symlinks=True)
    except shutil.Error:
        pass                                      # some entries could not be copied; the rest is there
    pathlib.Path(dst).mkdir(parents=True, exist_ok=True)
    return pathlib.Path(dst)


def restore_pristine(workspace):
    """Put every protected file back as released: seed tests and fixtures, both part test files, ci."""
    workspace = pathlib.Path(workspace)
    for src in sorted(PRISTINE.rglob("*")):
        if not src.is_file():
            continue
        dst = workspace / src.relative_to(PRISTINE)
        for parent in reversed(dst.relative_to(workspace).parents):
            folder = workspace / parent
            if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
                folder.unlink()
            folder.mkdir(exist_ok=True)
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        elif dst.is_dir():
            shutil.rmtree(dst)
        shutil.copyfile(src, dst)


def child_env(home):
    """A small fixed environment; `python3` on PATH is this interpreter."""
    return {
        "PATH": f"{pathlib.Path(sys.executable).parent}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
    }


def run(argv, cwd, timeout, home=None):
    """Run a child in its own process group. Returns (exit code or None if killed for time, stdout)."""
    try:
        child = subprocess.Popen(argv, cwd=cwd, env=child_env(home or cwd), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return 127, ""
    try:
        out, _ = child.communicate(timeout=timeout)
        return child.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            child.kill()
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return None, ""


def run_harness(workspace, files, timeout, scratch):
    """Run unittest files against the workspace copy. Returns {file: {"error", "tests"}}; {} if the run died."""
    handle, out = tempfile.mkstemp(prefix="result-", suffix=".json", dir=scratch)
    os.close(handle)
    os.unlink(out)
    run([sys.executable, "-I", "-B", str(HARNESS), str(workspace), out, *[str(f) for f in files]],
        cwd=workspace, timeout=timeout, home=scratch)
    try:
        return json.loads(pathlib.Path(out).read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError):
        return {}


def file_ok(result):
    """A file counts when it loaded, ran at least one test and every test passed."""
    tests = (result or {}).get("tests") or {}
    return bool(result) and result.get("error") is None and bool(tests) and all(s == "pass" for s in tests.values())


def green(workspace, part_b, timeout=45):
    """The pristine seed tests and tests/test_part_a.py pass (with part_b: tests/test_part_b.py too), on a copy."""
    scratch = scratch_dir(workspace, "rjprobe-")
    try:
        copy = copy_workspace(workspace, scratch / "work")
        restore_pristine(copy)
        files = seed_tests() + [PART_A] + ([PART_B] if part_b else [])
        results = run_harness(copy, files, timeout, scratch)
        failed = sorted(f"{name}:{test}" for name in files
                        for test, status in ((results.get(name) or {}).get("tests") or {}).items() if status != "pass")
        failed += sorted(f"{name}:not run" for name in files
                         if not (results.get(name) or {}).get("tests") or (results.get(name) or {}).get("error"))
        return all(file_ok(results.get(name)) for name in files), failed
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def emit(ok, option=None, failed=()):
    """The probe's one line on standard output; what was red goes to standard error, for a reader."""
    for name in failed:
        print(name, file=sys.stderr)
    print(json.dumps({"ok": bool(ok), "option": option}))
