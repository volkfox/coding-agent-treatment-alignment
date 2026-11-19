"""Run a hidden check that is one method walking its cases, one case at a time, without trusting the workspace.

    python3 -I -B loop_cases.py PROBES WORKSPACE OUT FILE CLASS.METHOD COUNT_NAME

FILE is a frozen check such as task/hidden/checks/h12_oracle.py, whose one test method is
`for seed in range(LAYOUTS): ...` and ends at the first layout that fails. Nothing of the check is copied
here: for each case the frozen method itself runs, on a fresh instance, while the name `range` in the check's
module is a stand-in that yields that one case for `range(COUNT)` and is the real range for every other call.
So a case is judged by the check's own lines, with the seed and the data the check gives it.

The import path is _rjharness.py's: started with -I, the standard library first, PROBES (for rjsupport) and
then the workspace appended to the END of sys.path. OUT gets one line per case as it ends, "<index> pass" or
"<index> fail", so a kill for time keeps what was done; a check that does not load leaves "error <why>".
A case that runs longer than CASE_SECONDS fails and the next one starts.
"""
import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402,F401  (the standard library the checks use, loaded before the workspace)
import contextlib  # noqa: E402
import dataclasses  # noqa: E402,F401
import importlib.util  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402,F401
import os  # noqa: E402
import pathlib  # noqa: E402
import random  # noqa: E402,F401
import re  # noqa: E402,F401
import shutil  # noqa: E402,F401
import signal  # noqa: E402
import subprocess  # noqa: E402,F401
import tempfile  # noqa: E402,F401
import unittest  # noqa: E402

CASE_SECONDS = 3


class TooSlow(BaseException):
    pass


def only(index, count):
    """The stand-in for `range` in the check's module: the loop over its cases yields this one case."""
    def stand_in(*args):
        return iter((index,)) if args == (count,) else range(*args)
    return stand_in


def _too_slow(signum, frame):
    raise TooSlow()


def run_case(case, method):
    """True when the method ran once on a fresh instance and passed."""
    result = unittest.TestResult()
    timed = hasattr(signal, "setitimer")
    if timed:
        signal.signal(signal.SIGALRM, _too_slow)
        signal.setitimer(signal.ITIMER_REAL, CASE_SECONDS)
    try:
        case(method).run(result)
    except BaseException:  # noqa: BLE001  (SystemExit and friends are a failed case too)
        return False
    finally:
        if timed:
            signal.setitimer(signal.ITIMER_REAL, 0)
    return (result.testsRun == 1 and result.wasSuccessful() and not result.skipped
            and not result.expectedFailures)


def main(argv):
    probes, workspace, out, path, name, count_name = argv
    workspace = pathlib.Path(workspace).resolve()
    sys.path.append(str(pathlib.Path(probes).resolve()))
    sys.path.append(str(workspace))
    os.chdir(workspace)
    class_name, method = name.split(".")
    sink = io.StringIO()
    with open(out, "w", encoding="utf-8") as record, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            spec = importlib.util.spec_from_file_location("rjloop_check", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules["rjloop_check"] = module
            spec.loader.exec_module(module)
            count, case = int(getattr(module, count_name)), getattr(module, class_name)
        except BaseException as error:  # noqa: BLE001
            record.write(f"error {type(error).__name__}: {error}"[:300].replace("\n", " ") + "\n")
            return 0
        for index in range(count):
            module.range = only(index, count)
            record.write(f"{index} {'pass' if run_case(case, method) else 'fail'}\n")
            record.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
