"""H5: a cancel promotes the earliest queued request that fits (FORK-3, option A). Part B."""
import tempfile
import unittest

import rjsupport
from slotbook.book import cancel, enqueue
from slotbook.spans import Span


class H5EarliestFitting(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = rjsupport.make_store(
            folder.name, rooms=("a", "b"), bookings=[("ann", "a", 10, 20), ("dan", "b", 10, 20)])

    def test_the_earliest_of_two_that_fit_is_booked_and_returned(self):
        enqueue(self.store, "bob", "a", Span(12, 16))
        enqueue(self.store, "cy", "a", Span(11, 15))
        made = cancel(self.store, 1)
        self.assertEqual(rjsupport.rows(made), [(12, 16, "bob", "a")])
        self.assertEqual(rjsupport.rows(self.store.bookings("a")), [(12, 16, "bob", "a")])
        self.assertEqual(rjsupport.rows(rjsupport.reload(self.store).bookings("a")), [(12, 16, "bob", "a")])

    def test_at_most_one_request_is_promoted_per_cancel(self):
        enqueue(self.store, "bob", "a", Span(11, 13))
        enqueue(self.store, "cy", "a", Span(16, 19))
        made = cancel(self.store, 1)
        self.assertEqual(rjsupport.rows(made), [(11, 13, "bob", "a")])
        self.assertEqual(rjsupport.rows(self.store.bookings("a")), [(11, 13, "bob", "a")])

    def test_a_request_for_another_room_is_left_waiting(self):
        enqueue(self.store, "bob", "b", Span(12, 16))
        enqueue(self.store, "cy", "a", Span(12, 16))
        made = cancel(self.store, 1)
        self.assertEqual(rjsupport.rows(made), [(12, 16, "cy", "a")])
        self.assertEqual(rjsupport.rows(self.store.bookings("b")), [(10, 20, "dan", "b")])
