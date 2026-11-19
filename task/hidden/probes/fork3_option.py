"""Probe: which FORK-3 option `cancel` implements for queued requests.

    python3 -I fork3_option.py WORKSPACE      prints {"ok": bool, "option": "A"|"B"|"C"|"other"|null}

The room holds ann 10-20 and dan 20-30. Queued, in this order: bob 15-25 (still does not fit once ann's
booking goes) and cy 12-16 (fits). ann's booking is cancelled.
A: cy's request is booked whole and returned.  B: bob's request is booked, cut to fit, and returned.
C: nobody is booked and `cancel` returns an empty list; code that ignores FORK-3, or has no working
`enqueue` yet, reads as C.  null: `cancel` is missing (or slotbook does not import).
`ok` is true when an option was read, "other" included.
The workspace is only read: stores are built in a temporary directory and no bytecode is written.
"""
import pathlib
import sys

sys.dont_write_bytecode = True

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402

sys.path.append(str(pathlib.Path(__file__).resolve().parent))


def option(workspace):
    sys.path.append(str(pathlib.Path(workspace).resolve()))
    try:
        import slotbook.book as rules
        from slotbook.spans import Span
        import rjsupport
    except BaseException:  # noqa: BLE001
        return None
    cancel = getattr(rules, "cancel", None)
    if not callable(cancel):
        return None
    try:
        with tempfile.TemporaryDirectory() as folder:
            os.chdir(folder)      # anything the code writes by a relative path lands here
            store = rjsupport.make_store(folder, bookings=[("ann", "a", 10, 20), ("dan", "a", 20, 30)])
            for account, start, end in (("bob", 15, 25), ("cy", 12, 16)):
                try:
                    rules.enqueue(store, account, "a", Span(start, end))
                except Exception:  # noqa: BLE001  (no queue yet: nobody can be promoted)
                    pass
            made = [(b.account, b.span.start, b.span.end) for b in cancel(store, 1)]
            if not made:
                return "C"
            if made == [("cy", 12, 16)]:
                return "A"
            if all(account == "bob" and 15 <= start < end <= 25 for account, start, end in made):
                return "B"
            return "other"
    except BaseException:  # noqa: BLE001
        return "other"


if __name__ == "__main__":
    with contextlib.redirect_stdout(io.StringIO()):      # the workspace's own prints are not the answer
        found = option(sys.argv[1])
    print(json.dumps({"ok": found is not None, "option": found}))
