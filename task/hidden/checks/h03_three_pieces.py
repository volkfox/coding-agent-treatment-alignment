"""H3: a request across two bookings is booked as three pieces, and the turnaround gaps are kept (FORK-2, option C)."""
import tempfile
import unittest

import rjsupport
from slotbook.book import book
from slotbook.spans import Span


class H3ThreePieces(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = rjsupport.make_store(folder.name, bookings=[("ann", "a", 10, 15), ("ann", "a", 20, 25)])

    def test_another_account_gets_three_pieces_with_gaps(self):
        made = book(self.store, "bob", "a", Span(5, 30))
        self.assertEqual(rjsupport.rows(made), [(5, 9, "bob", "a"), (16, 19, "bob", "a"), (26, 30, "bob", "a")])
        self.assertEqual(len(self.store.bookings("a")), 5)
        self.assertEqual(len(rjsupport.reload(self.store).bookings("a")), 5)

    def test_the_same_account_gets_three_pieces_back_to_back(self):
        made = book(self.store, "ann", "a", Span(5, 30))
        self.assertEqual(rjsupport.rows(made), [(5, 10, "ann", "a"), (15, 20, "ann", "a"), (25, 30, "ann", "a")])
