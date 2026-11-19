"""H12: 200 seeded single-account layouts against a slot-array oracle.

`free_spans` on windows of every kind, and `book` on requests that are fully taken or fully free with a
free slot on each side. One account holds everything, so no fork is exercised: this check catches code
that passes the visible tests by special-casing them.
"""
import random
import tempfile
import unittest

import rjsupport
from slotbook.book import BookingError, book, free_spans
from slotbook.spans import Span

LAYOUTS = 200
SLOTS = 48          # bookings lie in 0..SLOTS
EDGE = SLOTS + 12   # windows and requests may reach past the last booking


def layout(rng):
    """Non-overlapping spans, some of them back to back, in a shuffled (not slot) order."""
    spans, at = [], rng.randint(0, 4)
    while at < SLOTS - 1:
        end = min(at + rng.randint(1, 5), SLOTS)
        spans.append((at, end))
        at = end + rng.choice([0, 0, 1, 1, 2, 3, 5, 8])
    rng.shuffle(spans)
    return spans


def free_runs(taken, lo, hi):
    """The oracle: runs of untaken slots in lo..hi, read off the slot array one slot at a time."""
    runs, start = [], None
    for slot in range(lo, hi):
        if taken[slot]:
            if start is not None:
                runs.append((start, slot))
                start = None
        elif start is None:
            start = slot
    if start is not None:
        runs.append((start, hi))
    return runs


class H12Oracle(unittest.TestCase):
    def test_two_hundred_layouts(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for seed in range(LAYOUTS):
            rng = random.Random(8100 + seed)
            spans = layout(rng)
            taken = [False] * EDGE
            for start, end in spans:
                taken[start:end] = [True] * (end - start)
            store = rjsupport.make_store(folder.name, bookings=[("ann", "a", s, e) for s, e in spans])
            held = len(spans)

            for _ in range(5):
                lo = rng.randint(0, SLOTS)
                hi = rng.randint(lo + 1, EDGE)
                self.assertEqual(rjsupport.spans(free_spans(store, "a", lo, hi)), free_runs(taken, lo, hi),
                                 f"layout {seed}: free_spans(a, {lo}, {hi}) with bookings stored as {spans}")

            start, end = rng.choice(spans)
            lo = rng.randint(start, end - 1)
            hi = rng.randint(lo + 1, end)
            with self.assertRaises(BookingError, msg=f"layout {seed}: {lo}-{hi} is fully taken in {spans}"):
                book(store, "ann", "a", Span(lo, hi))
            self.assertEqual(len(store.bookings("a")), held, f"layout {seed}: a refused request changed the store")

            roomy = [(s, e) for s, e in free_runs(taken, 0, EDGE) if e - s >= 3]
            start, end = rng.choice(roomy)
            lo = rng.randint(start + 1, end - 2)
            hi = rng.randint(lo + 1, end - 1)
            made = book(store, "ann", "a", Span(lo, hi))
            self.assertEqual(rjsupport.rows(made), [(lo, hi, "ann", "a")],
                             f"layout {seed}: {lo}-{hi} is free, with a free slot on each side, in {spans}")
            taken[lo:hi] = [True] * (hi - lo)
            self.assertEqual(rjsupport.spans(free_spans(store, "a", 0, EDGE)), free_runs(taken, 0, EDGE),
                             f"layout {seed}: free_spans after booking {lo}-{hi} in {spans}")
            self.assertEqual(rjsupport.rows(rjsupport.reload(store).bookings("a")),
                             rjsupport.rows(store.bookings("a")), f"layout {seed}: the booking was not saved")
