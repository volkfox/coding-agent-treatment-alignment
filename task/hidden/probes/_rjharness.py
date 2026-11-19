"""Run unittest files against a workspace without trusting what the workspace holds.

    python3 -I -B _rjharness.py WORKSPACE OUT.json FILE [FILE ...]

FILE is absolute, or relative to WORKSPACE. The process is started with -I, so neither the current directory
nor any PYTHON* variable shapes the import path. The standard library is imported first; this directory
(for rjsupport) and then the workspace are appended to the END of sys.path, so a `unittest.py` or `json.py`
planted in the workspace root is never imported. The result goes to OUT.json:

    {"files": {FILE: {"error": null | "...", "tests": {"Class.test_name": "pass" | "fail" | "error" | "skip"}}}}

Anything else (a crash, a kill for time, no OUT.json) is read by the caller as "nothing passed".
"""
import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402,F401  (the standard library the tests and checks use, loaded before the workspace)
import contextlib  # noqa: E402
import dataclasses  # noqa: E402,F401
import importlib.util  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import random  # noqa: E402,F401
import re  # noqa: E402,F401
import shutil  # noqa: E402,F401
import subprocess  # noqa: E402,F401
import tempfile  # noqa: E402,F401
import unittest  # noqa: E402


class Recorder(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.status = {}

    @staticmethod
    def _name(test):
        method = getattr(test, "_testMethodName", None)
        return f"{type(test).__name__}.{method}" if method else str(test)

    def addSuccess(self, test):
        self.status.setdefault(self._name(test), "pass")

    def addFailure(self, test, err):
        self.status[self._name(test)] = "fail"

    def addError(self, test, err):
        self.status[self._name(test)] = "error"

    def addSkip(self, test, reason):
        self.status[self._name(test)] = "skip"

    def addExpectedFailure(self, test, err):
        self.status[self._name(test)] = "fail"

    def addUnexpectedSuccess(self, test):
        self.status[self._name(test)] = "fail"

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.status[self._name(test)] = "fail"


def run_file(index, path):
    name = f"rjcheck_{index}_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        suite = unittest.TestLoader().loadTestsFromModule(module)
    except BaseException as error:  # noqa: BLE001  (SystemExit and friends are a failed load too)
        return {"error": f"{type(error).__name__}: {error}"[:300], "tests": {}}
    recorder = Recorder()
    try:
        suite.run(recorder)
    except BaseException as error:  # noqa: BLE001
        return {"error": f"{type(error).__name__}: {error}"[:300], "tests": recorder.status}
    return {"error": None, "tests": recorder.status}


def main(argv):
    workspace = pathlib.Path(argv[0]).resolve()
    out = pathlib.Path(argv[1])
    sys.path.append(str(pathlib.Path(__file__).resolve().parent))
    sys.path.append(str(workspace))
    os.chdir(workspace)
    files = {}
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        for index, given in enumerate(argv[2:]):
            path = pathlib.Path(given)
            files[given] = run_file(index, path if path.is_absolute() else workspace / path)
    scratch = out.with_name(out.name + ".part")
    scratch.write_text(json.dumps({"files": files}), encoding="utf-8")
    os.replace(scratch, out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
