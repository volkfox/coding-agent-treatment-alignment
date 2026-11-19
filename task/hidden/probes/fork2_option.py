"""Probe: which FORK-2 option `book` implements for a partly free request.

    python3 -I fork2_option.py WORKSPACE      prints {"ok": bool, "option": "A"|"B"|"C"|"other"|null}

A: the request is refused (BookingError).  B: the earliest free piece is booked.  C: every free piece is booked.
null: `book` is missing (or slotbook does not import).  `ok` is true when an option was read, "other" included.
The answer does not depend on FORK-1: pieces are accepted with or without the turnaround gap.
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
    book = getattr(rules, "book", None)
    if not callable(book):
        return None
    try:
        with tempfile.TemporaryDirectory() as folder:
            os.chdir(folder)      # anything the code writes by a relative path lands here
            store = rjsupport.make_store(folder, bookings=[("ann", "a", 10, 15), ("ann", "a", 20, 25)])
            try:
                made = book(store, "bob", "a", Span(5, 30))
            except rules.BookingError:
                return "A"
            pieces = sorted(rjsupport.spans(b.span for b in made))
            if pieces in ([(5, 9)], [(5, 10)]):
                return "B"
            if pieces in ([(5, 9), (16, 19), (26, 30)], [(5, 10), (15, 20), (25, 30)]):
                return "C"
            return "other"
    except BaseException:  # noqa: BLE001
        return "other"


if __name__ == "__main__":
    with contextlib.redirect_stdout(io.StringIO()):      # the workspace's own prints are not the answer
        found = option(sys.argv[1])
    print(json.dumps({"ok": found is not None, "option": found}))
