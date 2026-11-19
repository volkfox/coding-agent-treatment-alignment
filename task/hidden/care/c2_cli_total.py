"""C2: the `utilisation` command counts every room in its total, the last one included."""
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import rjsupport
import slotbook


class C2CliTotal(unittest.TestCase):
    def test_c2_cli_total(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = rjsupport.make_store(
            folder.name, rooms=("a", "b", "c"),
            bookings=[("ann", "a", 0, 4), ("bob", "b", 10, 15), ("cy", "c", 3, 6), ("ann", "c", 20, 24)])
        done = subprocess.run(
            [sys.executable, "-m", "slotbook", "--store", str(store.path), "utilisation"],
            cwd=Path(slotbook.__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        totals = [line for line in done.stdout.splitlines() if line.lower().startswith("total")]
        self.assertEqual(len(totals), 1, done.stdout)
        self.assertEqual(int(re.findall(r"\d+", totals[0])[-1]), 16, totals[0])
