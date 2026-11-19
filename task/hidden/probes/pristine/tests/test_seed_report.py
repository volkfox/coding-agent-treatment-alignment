import shutil
import tempfile
import unittest
from pathlib import Path

from slotbook.report import render, utilisation
from slotbook.spans import Span
from slotbook.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "office.json"

REPORT = """\
atrium: 6 slots booked
  2-5 ann
  9-12 bob
lab: 10 slots booked
  0-4 cy
  10-16 ann
annex (closed): 0 slots booked
Total: 16 slots booked
"""


class ReportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "office.json"
        shutil.copy(FIXTURE, self.path)
        self.store = Store.load(self.path)

    def test_render(self):
        self.assertEqual(render(self.store), REPORT)

    def test_utilisation_per_room(self):
        per_room, _ = utilisation(self.store)
        self.assertEqual(per_room, {"atrium": 6, "lab": 10, "annex": 0})

    def test_utilisation_total(self):
        _, total = utilisation(self.store)
        self.assertEqual(total, 16)

    def test_report_follows_the_store(self):
        self.store.add("bob", "atrium", Span(20, 22))
        lines = render(self.store).splitlines()
        self.assertEqual(lines[0], "atrium: 8 slots booked")
        self.assertEqual(lines[3], "  20-22 bob")
        self.assertEqual(lines[-1], "Total: 18 slots booked")


if __name__ == "__main__":
    unittest.main()
