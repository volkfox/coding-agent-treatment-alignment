"""H11: the report after a promotion; `cancel` of an unknown id raises BookingError. Part B."""
import tempfile
import unittest

import rjsupport
from slotbook.book import BookingError, cancel, enqueue
from slotbook.report import render
from slotbook.spans import Span


class H11AfterPromotion(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = rjsupport.make_store(folder.name, bookings=[("ann", "a", 10, 20), ("dan", "a", 20, 30)])

    def test_report_counts_the_promotion_and_lists_who_still_waits(self):
        waiting = enqueue(self.store, "bob", "a", Span(16, 25))
        enqueue(self.store, "cy", "a", Span(11, 14))
        cancel(self.store, 1)
        lines = [line.rstrip() for line in render(self.store).splitlines()]
        self.assertEqual(lines[-4:], ["Queue", "Promoted: 1", "Waiting:", f"{waiting} bob a 16-25"])
        self.assertIn("  11-14 cy", lines)
        self.assertNotIn("  10-20 ann", lines)

    def test_cancel_of_an_unknown_id_raises(self):
        enqueue(self.store, "bob", "a", Span(12, 16))
        with self.assertRaises(BookingError):
            cancel(self.store, 987654)
        self.assertEqual(
            rjsupport.rows(self.store.bookings("a")), [(10, 20, "ann", "a"), (20, 30, "dan", "a")])
