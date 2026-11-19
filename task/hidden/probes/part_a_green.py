"""Probe: the pristine seed tests and tests/test_part_a.py pass.

    python3 -I part_a_green.py WORKSPACE      prints {"ok": bool, "option": null}

The workspace is not touched: it is copied, the protected files are restored on the copy, and the tests run there.
"""
import pathlib
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _rjlib  # noqa: E402

if __name__ == "__main__":
    try:
        ok, failed = _rjlib.green(sys.argv[1], part_b=False)
    except Exception as error:  # noqa: BLE001
        ok, failed = False, [f"probe: {type(error).__name__}: {error}"]
    _rjlib.emit(ok, None, failed[:20])
