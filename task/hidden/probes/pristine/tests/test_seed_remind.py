import shutil
import tempfile
import unittest
from pathlib import Path

from slotbook.remind import compose, due, reminders
from slotbook.spans import Span
from slotbook.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "office.json"


class RemindTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "office.json"
        shutil.copy(FIXTURE, self.path)
        self.store = Store.load(self.path)

    def ids(self, now):
        return [booking.id for booking in due(self.store, now)]

    def test_basic_reminder_two_slots_ahead(self):
        self.assertEqual(self.ids(7), [2])
        self.assertEqual(self.ids(8), [])

    def test_team_reminder_four_slots_ahead(self):
        self.assertEqual(self.ids(6), [4])

    def test_campus_gets_a_second_reminder(self):
        booking = self.store.add("cy", "lab", Span(30, 34))
        self.assertEqual(self.ids(22), [booking.id])
        self.assertEqual(self.ids(29), [booking.id])
        self.assertEqual(self.ids(28), [])

    def test_account_without_a_plan_gets_its_reminder(self):
        self.store.add("dee", "atrium", Span(40, 42))
        emails = reminders(self.store, 38)
        self.assertEqual(len(emails), 1)
        self.assertIn("To: dee\n", emails[0])
        self.assertIn("Plan: basic\n", emails[0])

    def test_email_text(self):
        booking = self.store.bookings("atrium")[1]
        self.assertEqual(
            compose(self.store, booking, 7),
            "To: bob\nSubject: atrium 9-12\nYour booking of atrium for slots 9-12 starts in 2 slots.\nPlan: basic\n",
        )


if __name__ == "__main__":
    unittest.main()
