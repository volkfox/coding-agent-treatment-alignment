"""H1: turnaround by account (FORK-1, option B).

One free slot is kept between bookings of different accounts; the same account may book back to back.
No request here is partly free, so the answer to FORK-2 does not matter.
"""
import tempfile
import unittest

import rjsupport
from slotbook.book import BookingError, book
from slotbook.spans import Span


class H1Turnaround(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = rjsupport.make_store(folder.name, bookings=[("ann", "a", 10, 20)])

    def test_same_account_books_back_to_back(self):
        made = book(self.store, "ann", "a", Span(20, 30))
        self.assertEqual(rjsupport.rows(made), [(20, 30, "ann", "a")])
        made = book(self.store, "ann", "a", Span(5, 10))
        self.assertEqual(rjsupport.rows(made), [(5, 10, "ann", "a")])

    def test_one_free_slot_is_enough_between_accounts(self):
        made = book(self.store, "bob", "a", Span(21, 30))
        self.assertEqual(rjsupport.rows(made), [(21, 30, "bob", "a")])
        made = book(self.store, "cy", "a", Span(3, 9))
        self.assertEqual(rjsupport.rows(made), [(3, 9, "cy", "a")])

    def test_slot_next_to_another_account_is_refused(self):
        for span in (Span(20, 21), Span(9, 10)):
            with self.assertRaises(BookingError):
                book(self.store, "bob", "a", span)
        self.assertEqual(rjsupport.rows(self.store.bookings("a")), [(10, 20, "ann", "a")])

    def test_the_only_slot_between_two_accounts_goes_to_neither(self):
        book(self.store, "bob", "a", Span(21, 30))
        for account in ("ann", "bob", "cy"):
            with self.assertRaises(BookingError):
                book(self.store, account, "a", Span(20, 21))
