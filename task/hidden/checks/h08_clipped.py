"""H8: `free_spans` is clipped to lo..hi. Bookings are stored in slot order, so only the clipping is checked."""
import tempfile
import unittest

import rjsupport
from slotbook.book import free_spans


class H8Clipped(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = rjsupport.make_store(
            folder.name, bookings=[("ann", "a", 2, 5), ("bob", "a", 10, 20), ("cy", "a", 40, 50)])

    def free(self, lo, hi):
        return rjsupport.spans(free_spans(self.store, "a", lo, hi))

    def test_window_starts_inside_a_booking(self):
        self.assertEqual(self.free(12, 30), [(20, 30)])

    def test_window_ends_inside_a_booking(self):
        self.assertEqual(self.free(6, 15), [(6, 10)])

    def test_bookings_outside_the_window_do_not_count(self):
        self.assertEqual(self.free(22, 28), [(22, 28)])
        self.assertEqual(self.free(20, 40), [(20, 40)])
        self.assertEqual(self.free(60, 70), [(60, 70)])

    def test_window_inside_one_booking_is_empty(self):
        self.assertEqual(self.free(12, 18), [])

    def test_window_across_all_bookings(self):
        self.assertEqual(self.free(3, 45), [(5, 10), (20, 40)])
