"""Tests for what the review turned up."""
import io
import json
import tempfile
import unittest
from pathlib import Path

from slotbook.book import BookingError, book, cancel, enqueue
from slotbook.cli import main
from slotbook.report import render, utilisation
from slotbook.spans import Span
from slotbook.store import Store


class ReviewFixes(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "store.json"
        records = [
            {"id": 1, "account": "ann", "room": "a", "start": 0, "end": 4},
            {"id": 2, "account": "bob", "room": "b", "start": 10, "end": 15},
            {"id": 3, "account": "cy", "room": "c", "start": 3, "end": 10},
        ]
        rooms = [{"name": "a", "closed": False}, {"name": "b", "closed": True}, {"name": "c", "closed": False}]
        data = {"rooms": rooms, "bookings": records, "plans": {}, "next_id": 4}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.store = Store.load(self.path)

    def test_total_counts_the_last_room(self):
        self.assertEqual(utilisation(self.store), ({"a": 4, "b": 5, "c": 7}, 16))
        self.assertEqual(render(self.store).splitlines()[-1], "Total: 16 slots booked")

    def test_utilisation_command_counts_the_last_room(self):
        out = io.StringIO()
        self.assertEqual(main(["--store", str(self.path), "utilisation"], out), 0)
        self.assertEqual(out.getvalue(), "a\t4\nb\t5\nc\t7\ntotal\t16\n")

    def test_closed_room_refuses_bookings_and_requests(self):
        with self.assertRaises(BookingError):
            book(self.store, "ann", "b", Span(30, 35))
        with self.assertRaises(BookingError):
            enqueue(self.store, "ann", "b", Span(10, 15))
        self.assertEqual(len(Store.load(self.path).bookings("b")), 1)

    def test_cancel_in_a_closed_room_frees_the_slots(self):
        self.assertEqual(cancel(self.store, 2), [])
        self.assertEqual(self.store.bookings("b"), [])


if __name__ == "__main__":
    unittest.main()
