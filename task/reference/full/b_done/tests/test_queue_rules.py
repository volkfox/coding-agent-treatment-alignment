"""My own tests of the queue, beside tests/test_part_b.py."""
import json
import tempfile
import unittest
from pathlib import Path

from slotbook.book import BookingError, cancel, enqueue
from slotbook.report import render
from slotbook.spans import Span
from slotbook.store import Store


class QueueRules(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "store.json"
        records = [
            {"id": 1, "account": "ann", "room": "a", "start": 10, "end": 20},
            {"id": 2, "account": "dan", "room": "a", "start": 20, "end": 30},
        ]
        data = {"rooms": [{"name": "a", "closed": False}], "bookings": records, "plans": {}, "next_id": 3}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.store = Store.load(self.path)

    def held(self, store=None):
        return [f"{b.span} {b.account}" for b in sorted((store or self.store).bookings("a"), key=lambda b: b.span)]

    def test_earliest_request_that_fits_is_promoted(self):
        enqueue(self.store, "bob", "a", Span(12, 16))
        enqueue(self.store, "cy", "a", Span(11, 15))
        made = cancel(self.store, 1)
        self.assertEqual([(b.account, b.span) for b in made], [("bob", Span(12, 16))])
        self.assertEqual(self.held(), ["12-16 bob", "20-30 dan"])
        self.assertEqual(self.held(Store.load(self.path)), ["12-16 bob", "20-30 dan"])

    def test_request_that_does_not_fit_keeps_its_place(self):
        first = enqueue(self.store, "bob", "a", Span(16, 25))
        enqueue(self.store, "cy", "a", Span(11, 14))
        self.assertEqual([b.account for b in cancel(self.store, 1)], ["cy"])
        self.assertEqual(render(self.store).splitlines()[-4:],
                         ["Queue", "Promoted: 1", "Waiting:", f"{first} bob a 16-25"])
        self.assertEqual([b.account for b in cancel(self.store, 2)], ["bob"])
        self.assertEqual(render(self.store).splitlines()[-3:], ["Queue", "Promoted: 2", "Waiting:"])

    def test_turnaround_counts_when_fitting(self):
        enqueue(self.store, "bob", "a", Span(15, 20))      # would touch dan's booking at 20
        self.assertEqual(cancel(self.store, 1), [])
        self.assertEqual(self.held(), ["20-30 dan"])

    def test_cancel_of_an_unknown_booking_is_refused(self):
        with self.assertRaises(BookingError):
            cancel(self.store, 99)
        self.assertEqual(self.held(), ["10-20 ann", "20-30 dan"])

    def test_bad_request_is_not_queued(self):
        for room, span in (("a", Span(9, 3)), ("a", Span(-1, 4)), ("cellar", Span(1, 2))):
            with self.assertRaises(BookingError):
                enqueue(self.store, "bob", room, span)
        self.assertNotIn("Queue", render(Store.load(self.path)).splitlines())


if __name__ == "__main__":
    unittest.main()
