"""Part B of TASK.md. A test named test_rN_* belongs to requirement RN."""
import json
import tempfile
import unittest
from pathlib import Path

import slotbook.book as rules
from slotbook.report import render
from slotbook.spans import Span
from slotbook.store import Store


class PartB(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "store.json"

    def store(self, *bookings):
        """A store with the open rooms `a` and `b`, holding (account, room, start, end) bookings."""
        records = [
            {"id": number, "account": account, "room": room, "start": start, "end": end}
            for number, (account, room, start, end) in enumerate(bookings, 1)
        ]
        data = {
            "rooms": [{"name": "a", "closed": False}, {"name": "b", "closed": False}],
            "bookings": records,
            "plans": {},
            "next_id": len(records) + 1,
        }
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return Store.load(self.path)

    def test_r6_cancel_frees_the_slots(self):
        store = self.store(("ann", "a", 10, 20), ("bob", "a", 30, 40))
        rules.cancel(store, 1)
        self.assertEqual([booking.id for booking in store.bookings("a")], [2])
        self.assertEqual(rules.free_spans(store, "a", 0, 50), [Span(0, 30), Span(40, 50)])

    def test_r6_cancel_without_a_queue_returns_an_empty_list(self):
        store = self.store(("ann", "a", 10, 20), ("bob", "b", 30, 40))
        self.assertEqual(rules.cancel(store, 2), [])

    def test_r6_cancel_is_saved(self):
        store = self.store(("ann", "a", 10, 20), ("bob", "a", 30, 40))
        rules.cancel(store, 2)
        again = Store.load(self.path)
        self.assertEqual([booking.id for booking in again.bookings("a")], [1])

    def test_r7_report_ends_with_the_queue_section(self):
        store = self.store(("ann", "a", 10, 20), ("bob", "b", 0, 8))
        first = rules.enqueue(store, "bob", "a", Span(12, 16))
        second = rules.enqueue(store, "cy", "b", Span(0, 8))
        self.assertIsInstance(first, int)
        self.assertIsInstance(second, int)
        self.assertNotEqual(first, second)
        section = ["Queue", "Promoted: 0", "Waiting:", f"{first} bob a 12-16", f"{second} cy b 0-8"]
        self.assertEqual(render(store).splitlines()[-5:], section)
        self.assertEqual(render(Store.load(self.path)).splitlines()[-5:], section)

    def test_r7_no_queue_section_without_requests(self):
        store = self.store(("ann", "a", 10, 20))
        lines = render(store).splitlines()
        self.assertNotIn("Queue", lines)
        self.assertTrue(lines[-1].startswith("Total:"), lines[-1])


if __name__ == "__main__":
    unittest.main()
