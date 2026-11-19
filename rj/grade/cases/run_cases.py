"""Score one workspace case by case: what task/hidden/run_checks.py scores, with every case kept.

    python3 -I run_cases.py HIDDEN WORKSPACE [--part-b]
    python3 -I run_cases.py HIDDEN --list

HIDDEN is task/hidden. It is frozen: its files are imported and read here, never changed. With a workspace
this prints ONE JSON line and exits 0, whatever state the workspace is in:

    {"musts": {"R1": 0|1, ..., "R7": 0|1},
     "hidden": {"H1": {"score": 0|1|null, "cases": int, "passed": int|null, "failed": ["<case>", ...]}, ...},
     "care": {"C1": {"score": 0|1, "cases": int, "passed": int, "failed": [...]}, "C2": {...}},
     "notes": ["..."]}

`score` is run_checks.py's figure by run_checks.py's rule: the check's file runs in a child of its own through
the frozen harness (probes/_rjharness.py), on a copy with every protected file restored, and scores 1 when it
loaded, ran a test and every test passed. Without --part-b, R6 and R7 are 0 and H5, H6 and H11 are null, with
`passed` null. `sh ci`, the seed tests and the fork probes are not run here: nothing below is read from them.

A case is one test method of the check's file. The harness runs every method whatever the others did, so its
record holds the cases of every check but one. H12 is one method that walks 200 layouts and stops at the first
that fails. Its cases are the layouts: loop_cases.py runs that same frozen method once per layout, with the
`range` of the check's module standing in so that the loop yields one layout. `failed` names what was red, the
first 20 of it. A file that did not load or was killed for time has no case passed.

--list prints {"H1": {"file": "h01_turnaround.py", "cases": [...], "loop": null}, ...} for the hidden checks and
the care tests: the cases as they are read from the files' text, with no workspace.
"""
import sys

sys.dont_write_bytecode = True

import ast  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
LOOP_RUNNER = HERE / "loop_cases.py"
# a check that is one method walking its cases: (the method, the module constant that counts them, what a case is called)
LOOPS = {"H12": ("H12Oracle.test_two_hundred_layouts", "LAYOUTS", "layout")}
LOOP_SECONDS = 30       # for all the cases of one looped check; run_checks.py gives the whole file CHECK_SECONDS
NAMED = 20


def frozen(hidden):
    """task/hidden's own modules: (probes/_rjlib.py, run_checks.py)."""
    for folder in (hidden, hidden / "probes"):
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    import _rjlib
    import run_checks
    return _rjlib, run_checks


def cases_of(path):
    """The test methods of a check file as the harness names them, read from its text: a file that cannot be
    imported against a workspace still has its cases."""
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    return [f"{cls.name}.{fn.name}" for cls in tree.body if isinstance(cls, ast.ClassDef)
            and any(getattr(base, "attr", getattr(base, "id", None)) == "TestCase" for base in cls.bases)
            for fn in cls.body if isinstance(fn, ast.FunctionDef) and fn.name.startswith("test")]


def constant(path, name):
    """A module-level `NAME = <literal>` of a check file; None when it has none."""
    for node in ast.parse(pathlib.Path(path).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return None
    return None


def loop_of(label, path):
    """(method, constant, word, count) when the check is one looped method, else None."""
    if label not in LOOPS:
        return None
    method, name, word = LOOPS[label]
    count = constant(path, name)
    if cases_of(path) != [method] or not isinstance(count, int) or count < 1:
        return None
    return method, name, word, count


def listing(run_checks):
    """Every hidden check and care test: {"H1": {"file", "cases", "loop"}, ...}."""
    out = {}
    for label, path in {**run_checks.check_files(), **run_checks.care_files()}.items():
        loop = loop_of(label, path)
        cases = [f"{loop[2]} {i}" for i in range(loop[3])] if loop else cases_of(path)
        out[label] = {"file": path.name, "cases": cases, "loop": loop[0] if loop else None}
    return out


def tally(found, cases):
    """Cases and how many of them passed, from one file's harness record."""
    tests = (found or {}).get("tests") or {}
    failed = [case for case in cases if tests.get(case) != "pass"]
    return {"cases": len(cases), "passed": len(cases) - len(failed), "failed": failed[:NAMED]}


def read_loop(record, word, count):
    """What loop_cases.py wrote: one "<index> pass|fail" line per case that ended. Returns (tally, note or None)."""
    try:
        lines = pathlib.Path(record).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    status, note = {}, None
    for line in lines:
        index, _, rest = line.partition(" ")
        if index.isdigit() and rest in ("pass", "fail"):
            status[int(index)] = rest
        elif index == "error":
            note = f"did not load: {rest.split(' (/')[0]}"[:200]      # without the scratch path, as run_checks.py cuts it
    if note is None and len(status) < count:
        note = f"stopped for time after {len(status)} of {count} cases"
    failed = [f"{word} {i}" for i in range(count) if status.get(i) != "pass"]
    return {"cases": count, "passed": count - len(failed), "failed": failed[:NAMED]}, note


def blank(run_checks, part_b):
    hidden = {}
    for label, entry in listing(run_checks).items():
        waiting = label in run_checks.HIDDEN_PART_B and not part_b
        hidden[label] = {"score": None if waiting else 0, "cases": len(entry["cases"]),
                         "passed": None if waiting else 0, "failed": []}
    care = {label: hidden.pop(label) for label in list(hidden) if label.startswith("C")}
    return {"musts": {r: 0 for r in run_checks.MUSTS}, "hidden": hidden, "care": care, "notes": []}


def score(_rjlib, run_checks, workspace, part_b, result, scratch):
    deadline = time.monotonic() + run_checks.BUDGET_SECONDS
    clean = _rjlib.copy_workspace(workspace, scratch / "clean")
    _rjlib.restore_pristine(clean)

    def harness(files):
        limit = min(run_checks.CHECK_SECONDS, deadline - time.monotonic())
        return _rjlib.run_harness(clean, files, limit, scratch) if limit >= 1 else {}

    def loop(label, path, spec):
        method, name, word, count = spec
        record = scratch / f"loop-{label}.txt"
        _rjlib.run([sys.executable, "-I", "-B", str(LOOP_RUNNER), str(_rjlib.HERE), str(clean), str(record), str(path),
                    method, name], cwd=clean, timeout=LOOP_SECONDS, home=scratch)
        return read_loop(record, word, count)

    checks = {h: p for h, p in run_checks.check_files().items() if result["hidden"][h]["score"] is not None}
    cares = run_checks.care_files()
    loops = {label: spec for label, spec in ((label, loop_of(label, path)) for label, path in checks.items()) if spec}
    # Submitted in run_checks.py's order; the looped cases come last and have their own time.
    with ThreadPoolExecutor(max_workers=max(1, min(run_checks.MAX_WORKERS, run_checks.cpus()))) as pool:
        part_a = pool.submit(harness, [_rjlib.PART_A])
        part_b_run = pool.submit(harness, [_rjlib.PART_B]) if part_b else None
        hidden = {h: pool.submit(harness, [p]) for h, p in checks.items()}
        care = {c: pool.submit(harness, [p]) for c, p in cares.items()}
        looped = {label: pool.submit(loop, label, checks[label], spec) for label, spec in loops.items()}

    def tests_of(future, name):
        return ((future.result() or {}).get(str(name)) or {})

    by_must = {}
    visible = [(part_a, _rjlib.PART_A)] + ([(part_b_run, _rjlib.PART_B)] if part_b else [])
    for future, name in visible:
        for test, status in (tests_of(future, name).get("tests") or {}).items():
            match = re.match(r"test_r(\d+)_", test.rsplit(".", 1)[-1])
            if match:
                by_must.setdefault(f"R{match.group(1)}", []).append(status == "pass")
    for must in run_checks.MUSTS:
        if must in run_checks.MUSTS_PART_B and not part_b:
            continue
        result["musts"][must] = int(bool(by_must.get(must)) and all(by_must[must]))

    for group, futures, paths in (("hidden", hidden, checks), ("care", care, cares)):
        for label, future in futures.items():
            found = tests_of(future, paths[label])
            entry = {"score": int(_rjlib.file_ok(found)), **tally(found, cases_of(paths[label]))}
            if label in looped:
                counted, note = looped[label].result()
                entry.update(counted)
                if note:
                    result["notes"].append(f"{label}: {note}")
            result[group][label] = entry


def main(argv):
    hidden = pathlib.Path(argv[0]).resolve()
    _rjlib, run_checks = frozen(hidden)
    if "--list" in argv:
        print(json.dumps(listing(run_checks)))
        return 0
    part_b = "--part-b" in argv
    paths = [a for a in argv[1:] if not a.startswith("--")]
    result = blank(run_checks, part_b)
    scratch = None
    try:
        scratch = _rjlib.scratch_dir(paths[0], "rjcases-")
        score(_rjlib, run_checks, pathlib.Path(paths[0]), part_b, result, scratch)
    except BaseException as error:  # noqa: BLE001  (the line is printed whatever happens)
        result["notes"].append(f"run_cases:{type(error).__name__}: {str(error).split(' (/')[0]}"[:200])
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
