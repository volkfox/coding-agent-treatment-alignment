"""Part A of TASK.md. A test named test_rN_* belongs to requirement RN."""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import slotbook
import slotbook.book as rules
from slotbook.book import BookingError
from slotbook.spans import Span
from slotbook.store import Store

ROOT = Path(__file__).resolve().parents[1]


class PartA(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "store.json"

    def store(self, *bookings):
        """A store with the open rooms `a` and `b`, holding (account, room, start, end) bookings."""
        records = [
            {"id": number, "account": account, "room": room, "start": start, "end": end}
            for number, (account, room, start, end) in enumerate(bookings, 1)
        ]
        data = {
            "rooms": [{"name": "a", "closed": False}, {"name": "b", "closed": False}],
            "bookings": records,
            "plans": {},
            "next_id": len(records) + 1,
        }
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return Store.load(self.path)

    def free_command(self, room, lo, hi):
        return subprocess.run(
            [sys.executable, "-m", "slotbook", "--store", str(self.path), "free", room, str(lo), str(hi)],
            cwd=ROOT, capture_output=True, text=True,
        )

    def test_r1_free_span_is_booked(self):
        store = self.store(("ann", "a", 0, 5), ("bob", "a", 20, 25))
        made = rules.book(store, "cy", "a", Span(10, 15))
        self.assertEqual(len(made), 1)
        self.assertEqual((made[0].account, made[0].room, made[0].span), ("cy", "a", Span(10, 15)))
        self.assertIn(made[0], store.bookings("a"))
        self.assertEqual(store.bookings("b"), [])

    def test_r1_booking_is_saved(self):
        made = rules.book(self.store(), "ann", "b", Span(3, 7))
        again = Store.load(self.path)
        self.assertEqual(again.bookings("b"), made)

    def test_r2_fully_taken_span_raises(self):
        store = self.store(("ann", "a", 10, 20))
        with self.assertRaises(BookingError):
            rules.book(store, "bob", "a", Span(12, 15))
        with self.assertRaises(BookingError):
            rules.book(store, "ann", "a", Span(10, 20))

    def test_r3_free_spans_lists_unbooked_slots(self):
        store = self.store(("ann", "a", 5, 8), ("bob", "a", 12, 15))
        self.assertEqual(rules.free_spans(store, "a", 0, 20), [Span(0, 5), Span(8, 12), Span(15, 20)])

    def test_r3_free_spans_are_merged(self):
        store = self.store(("ann", "a", 5, 8), ("ann", "a", 8, 12), ("bob", "b", 0, 30))
        self.assertEqual(rules.free_spans(store, "a", 0, 20), [Span(0, 5), Span(12, 20)])
        self.assertEqual(rules.free_spans(store, "b", 0, 30), [])

    def test_r3_empty_room_is_one_free_span(self):
        store = self.store(("ann", "a", 5, 8))
        self.assertEqual(rules.free_spans(store, "b", 4, 9), [Span(4, 9)])

    def test_r4_free_command_prints_one_span_per_line(self):
        self.store(("ann", "a", 5, 8), ("bob", "a", 12, 15))
        done = self.free_command("a", 0, 20)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "0-5\n8-12\n15-20\n")

    def test_r4_free_command_prints_nothing_for_a_full_room(self):
        self.store(("ann", "a", 0, 20))
        done = self.free_command("a", 0, 20)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "")

    def test_r5_version_matches_changelog(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        top = re.search(r"^## (\S+)", changelog, re.MULTILINE)
        self.assertIsNotNone(top, "CHANGELOG.md has no version heading")
        self.assertEqual(top.group(1), "0.10.0")
        self.assertEqual(slotbook.__version__, "0.10.0")


if __name__ == "__main__":
    unittest.main()
