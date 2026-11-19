"""H4: nothing is left of a request after the turnaround gaps: BookingError, and the store is unchanged."""
import tempfile
import unittest

import rjsupport
from slotbook.book import BookingError, book
from slotbook.spans import Span


class H4NothingLeft(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = rjsupport.make_store(folder.name, bookings=[("ann", "a", 10, 12), ("ann", "a", 14, 16)])
        self.before = rjsupport.rows(self.store.bookings("a"))

    def refused(self, account, span):
        with self.assertRaises(BookingError):
            book(self.store, account, "a", span)
        self.assertEqual(rjsupport.rows(self.store.bookings("a")), self.before, "the store changed in memory")
        self.assertEqual(rjsupport.rows(rjsupport.reload(self.store).bookings("a")), self.before,
                         "the store file changed")

    def test_free_slots_that_are_all_gaps(self):
        self.refused("bob", Span(12, 14))

    def test_one_gap_slot(self):
        self.refused("bob", Span(16, 17))

    def test_taken_slots_and_gaps_only(self):
        self.refused("bob", Span(11, 15))
