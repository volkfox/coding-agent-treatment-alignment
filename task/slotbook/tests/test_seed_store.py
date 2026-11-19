import shutil
import tempfile
import unittest
from pathlib import Path

from slotbook.spans import Span
from slotbook.store import Booking, Room, Store

FIXTURE = Path(__file__).parent / "fixtures" / "office.json"


class StoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "office.json"
        shutil.copy(FIXTURE, self.path)
        self.store = Store.load(self.path)

    def test_load_reads_the_rooms(self):
        self.assertEqual(
            self.store.rooms(), [Room("atrium", False), Room("lab", False), Room("annex", True)]
        )

    def test_room_by_name(self):
        self.assertFalse(self.store.room("lab").closed)
        self.assertTrue(self.store.room("annex").closed)

    def test_unknown_room_is_a_key_error(self):
        with self.assertRaises(KeyError):
            self.store.room("cellar")
        with self.assertRaises(KeyError):
            self.store.bookings("cellar")

    def test_bookings_of_a_room(self):
        self.assertEqual(
            self.store.bookings("atrium"),
            [Booking(1, "ann", "atrium", Span(2, 5)), Booking(2, "bob", "atrium", Span(9, 12))],
        )
        self.assertEqual(self.store.bookings("annex"), [])

    def test_add_gives_the_next_id(self):
        first = self.store.add("bob", "lab", Span(20, 22))
        second = self.store.add("cy", "atrium", Span(30, 31))
        self.assertEqual(first, Booking(5, "bob", "lab", Span(20, 22)))
        self.assertEqual(second.id, 6)
        self.assertEqual(self.store.bookings("lab")[-1], first)

    def test_add_to_an_unknown_room_is_a_key_error(self):
        with self.assertRaises(KeyError):
            self.store.add("bob", "cellar", Span(1, 2))

    def test_remove(self):
        self.store.remove(1)
        self.assertEqual([booking.id for booking in self.store.bookings("atrium")], [2])
        with self.assertRaises(KeyError):
            self.store.remove(1)

    def test_save_and_load_again(self):
        self.store.add("bob", "lab", Span(20, 22))
        self.store.remove(3)
        self.store.save()
        again = Store.load(self.path)
        self.assertEqual(again.rooms(), self.store.rooms())
        self.assertEqual(again.bookings("lab"), self.store.bookings("lab"))
        self.assertEqual(again.add("cy", "lab", Span(40, 41)).id, 6)
        self.assertEqual(Store.load(Path(self.path.parent) / "new.json").rooms(), [])


if __name__ == "__main__":
    unittest.main()
