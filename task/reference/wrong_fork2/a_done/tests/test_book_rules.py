"""My own tests of the booking rules, beside tests/test_part_a.py."""
import json
import tempfile
import unittest
from pathlib import Path

from slotbook.book import BookingError, book, free_spans
from slotbook.report import render
from slotbook.spans import Span
from slotbook.store import Store


class BookRules(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "store.json"

    def store(self, *bookings):
        records = [
            {"id": number, "account": account, "room": "a", "start": start, "end": end}
            for number, (account, start, end) in enumerate(bookings, 1)
        ]
        data = {"rooms": [{"name": "a", "closed": False}], "bookings": records, "plans": {},
                "next_id": len(records) + 1}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return Store.load(self.path)

    def spans(self, bookings):
        return sorted(str(booking.span) for booking in bookings)

    def test_same_account_books_back_to_back(self):
        store = self.store(("ann", 10, 20))
        self.assertEqual(self.spans(book(store, "ann", "a", Span(20, 25))), ["20-25"])

    def test_other_account_keeps_one_free_slot(self):
        store = self.store(("ann", 10, 20))
        self.assertEqual(self.spans(book(store, "bob", "a", Span(20, 25))), ["21-25"])
        self.assertEqual(self.spans(book(store, "bob", "a", Span(5, 10))), ["5-9"])

    def test_partly_free_request_books_the_earliest_free_piece(self):
        store = self.store(("ann", 10, 15), ("ann", 20, 25))
        self.assertEqual(self.spans(book(store, "bob", "a", Span(5, 30))), ["5-9"])
        self.assertEqual(len(Store.load(self.path).bookings("a")), 3)

    def test_request_with_no_free_slot_changes_nothing(self):
        store = self.store(("ann", 10, 12), ("ann", 14, 16))
        with self.assertRaises(BookingError):
            book(store, "bob", "a", Span(12, 14))
        self.assertEqual(len(store.bookings("a")), 2)
        self.assertEqual(len(Store.load(self.path).bookings("a")), 2)

    def test_bad_input_is_refused_and_nothing_is_saved(self):
        store = self.store(("ann", 10, 20))
        for room, span in (("a", Span(8, 5)), ("a", Span(-2, 3)), ("a", Span(4, 4)), ("cellar", Span(1, 2))):
            with self.assertRaises(BookingError):
                book(store, "bob", room, span)
        self.assertEqual(len(Store.load(self.path).bookings("a")), 1)

    def test_free_spans_is_sorted_and_clipped(self):
        store = self.store(("ann", 40, 50), ("bob", 10, 20), ("cy", 2, 5))
        self.assertEqual([str(span) for span in free_spans(store, "a", 3, 45)], ["5-10", "20-40"])
        self.assertEqual(free_spans(store, "a", 12, 18), [])

    def test_one_store_read_per_call(self):
        store = self.store(("ann", 10, 20), ("bob", 30, 40))
        reads = []
        read = store.bookings
        store.bookings = lambda room: reads.append(room) or read(room)
        book(store, "cy", "a", Span(22, 28))
        free_spans(store, "a", 0, 50)
        self.assertEqual(reads, ["a", "a"])

    def test_report_lists_bookings_in_slot_order(self):
        store = self.store()
        for account, start in (("bob", 20), ("ann", 2), ("cy", 10)):
            book(store, account, "a", Span(start, start + 3))
        self.assertEqual(render(store).splitlines()[1:4], ["  2-5 ann", "  10-13 cy", "  20-23 bob"])


if __name__ == "__main__":
    unittest.main()
