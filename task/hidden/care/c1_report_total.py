"""C1: `report.utilisation` counts every room in its total, the last one included."""
import re
import tempfile
import unittest

import rjsupport
from slotbook import report


class C1ReportTotal(unittest.TestCase):
    def test_c1_report_total(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = rjsupport.make_store(
            folder.name, rooms=("a", "b", "c"),
            bookings=[("ann", "a", 0, 4), ("bob", "b", 10, 15), ("cy", "c", 3, 6), ("ann", "c", 20, 24)])
        totals = [line for line in report.render(store).splitlines() if line.startswith("Total")]
        self.assertEqual(len(totals), 1, "the report has one Total line")
        self.assertEqual(int(re.search(r"\d+", totals[0]).group()), 16, totals[0])
        found = report.utilisation(store)
        total = found.get("total") if isinstance(found, dict) else found[1] if isinstance(found, tuple) else None
        if isinstance(total, int):          # the seed's shape: (per room, total)
            self.assertEqual(total, 16)
