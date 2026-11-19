import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from slotbook.cli import main
from slotbook.report import render
from slotbook.store import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "office.json"


class CliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "office.json"
        shutil.copy(FIXTURE, self.path)

    def run_cli(self, *words):
        out = io.StringIO()
        status = main(["--store", str(self.path), *words], out)
        return status, out.getvalue()

    def test_rooms(self):
        self.assertEqual(self.run_cli("rooms"), (0, "atrium\nlab\nannex (closed)\n"))

    def test_list(self):
        self.assertEqual(self.run_cli("list", "atrium"), (0, "1 2-5 ann\n2 9-12 bob\n"))

    def test_utilisation(self):
        self.assertEqual(self.run_cli("utilisation"), (0, "atrium\t6\nlab\t10\nannex\t0\ntotal\t16\n"))

    def test_report_as_a_module(self):
        done = subprocess.run(
            [sys.executable, "-m", "slotbook", "--store", str(self.path), "report"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, render(Store.load(self.path)))


if __name__ == "__main__":
    unittest.main()
