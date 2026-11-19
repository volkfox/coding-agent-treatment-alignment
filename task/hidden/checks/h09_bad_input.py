"""H9: start above end, a negative slot, an unknown room: BookingError, and nothing is saved (TASK.md, "Should")."""
import tempfile
import unittest

import rjsupport
from slotbook.book import BookingError, book
from slotbook.spans import Span


class H9BadInput(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = rjsupport.make_store(folder.name, rooms=("a", "b"), bookings=[("ann", "a", 10, 20)])

    def refused(self, room, start, end):
        with self.assertRaises(BookingError):
            book(self.store, "bob", room, Span(start, end))      # a Span that refuses to exist counts too
        for store in (self.store, rjsupport.reload(self.store)):
            self.assertEqual(rjsupport.rows(store.bookings("a")), [(10, 20, "ann", "a")])
            self.assertEqual(rjsupport.rows(store.bookings("b")), [])

    def test_start_above_end(self):
        self.refused("b", 8, 5)
        self.refused("a", 40, 30)

    def test_negative_slot(self):
        self.refused("b", -3, 2)
        self.refused("b", -5, -2)

    def test_unknown_room(self):
        self.refused("nosuch", 1, 4)
