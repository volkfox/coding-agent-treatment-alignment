"""H6: a request that does not fit keeps its place while a later one that fits is promoted (FORK-3, option A). Part B."""
import tempfile
import unittest

import rjsupport
from slotbook.book import cancel, enqueue
from slotbook.spans import Span


class H6KeepsItsPlace(unittest.TestCase):
    def test_later_request_is_promoted_and_the_earlier_one_waits_its_turn(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = rjsupport.make_store(folder.name, bookings=[("ann", "a", 10, 20), ("dan", "a", 20, 30)])
        enqueue(store, "bob", "a", Span(16, 25))      # needs both bookings gone
        enqueue(store, "cy", "a", Span(11, 14))       # needs ann's only
        enqueue(store, "eve", "a", Span(22, 28))      # needs dan's only, but bob is ahead of it

        made = cancel(store, 1)
        self.assertEqual(rjsupport.rows(made), [(11, 14, "cy", "a")], "the later request that fits is promoted")
        self.assertEqual(rjsupport.rows(store.bookings("a")), [(11, 14, "cy", "a"), (20, 30, "dan", "a")])

        made = cancel(store, 2)
        self.assertEqual(rjsupport.rows(made), [(16, 25, "bob", "a")], "the earlier request kept its place")
        self.assertEqual(rjsupport.rows(store.bookings("a")), [(11, 14, "cy", "a"), (16, 25, "bob", "a")])
