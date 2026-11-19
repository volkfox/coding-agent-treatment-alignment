"""H2: 5,000 bookings, one `store.bookings` read per call (docs/rules.md, "The store")."""
import tempfile
import unittest

import rjsupport
from slotbook.book import BookingError, book, free_spans
from slotbook.spans import Span

BOOKINGS = 5000


class H2OneRead(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # ann holds 0-5, 10-15, 20-25, ... in room a; room b holds two bookings among the 5,000 records
        layout = [("ann", "a", 10 * i, 10 * i + 5) for i in range(BOOKINGS)]
        layout[2500:2500] = [("bob", "b", 10, 20), ("bob", "b", 30, 50)]
        self.store = rjsupport.make_store(folder.name, rooms=("a", "b"), bookings=layout)
        self.reads = []
        read = self.store.bookings

        def counted(room):
            self.reads.append(room)
            return read(room)

        self.store.bookings = counted

    def test_book_reads_once(self):
        made = book(self.store, "ann", "a", Span(10 * BOOKINGS + 20, 10 * BOOKINGS + 40))
        self.assertLessEqual(len(self.reads), 1, f"book read the store {len(self.reads)} times")
        self.assertEqual(rjsupport.rows(made), [(10 * BOOKINGS + 20, 10 * BOOKINGS + 40, "ann", "a")])

    def test_refused_book_reads_once(self):
        with self.assertRaises(BookingError):
            book(self.store, "ann", "a", Span(20000, 20005))
        self.assertLessEqual(len(self.reads), 1, f"book read the store {len(self.reads)} times")

    def test_free_spans_reads_once(self):
        # the window covers every booking of room b, so clipping (H8) plays no part
        found = free_spans(self.store, "b", 0, 60)
        self.assertLessEqual(len(self.reads), 1, f"free_spans read the store {len(self.reads)} times")
        self.assertEqual(rjsupport.spans(found), [(0, 10), (20, 30), (50, 60)])
