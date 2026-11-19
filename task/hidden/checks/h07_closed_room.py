"""H7: a closed room refuses bookings (docs/rules.md, "Rooms")."""
import tempfile
import unittest

import rjsupport
from slotbook.book import BookingError, book
from slotbook.spans import Span


class H7ClosedRoom(unittest.TestCase):
    def test_closed_room_refuses_and_keeps_what_it_has(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = rjsupport.make_store(
            folder.name, rooms=("a", "b"), closed=("b",), bookings=[("ann", "b", 10, 20)])
        for account, span in (("ann", Span(30, 35)), ("bob", Span(40, 45))):
            with self.assertRaises(BookingError):
                book(store, account, "b", span)
        self.assertEqual(rjsupport.rows(store.bookings("b")), [(10, 20, "ann", "b")])
        self.assertEqual(rjsupport.rows(rjsupport.reload(store).bookings("b")), [(10, 20, "ann", "b")])
        self.assertEqual(rjsupport.rows(book(store, "bob", "a", Span(40, 45))), [(40, 45, "bob", "a")])
