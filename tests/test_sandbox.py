import tempfile
import unittest
from pathlib import Path

from rj import sandbox


class SandboxCase:
    mode = "subprocess"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)   # under the repo: Docker can mount it
        self.work = Path(self._tmp.name) / "work"
        self.probes = Path(self._tmp.name) / "probes"
        self.work.mkdir()
        self.probes.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_runs_python_312_in_the_workspace(self):
        (self.work / "hello.py").write_text("import sys\nprint(sys.version_info[:2])\nopen('made.txt','w').write('x')\n")
        r = sandbox.run(["python3", "-I", "hello.py"], self.work, mode=self.mode)
        self.assertTrue(r.ok, r.stderr)
        self.assertIn("(3, 12)", r.stdout)
        self.assertEqual(r.mode, self.mode)
        self.assertTrue((self.work / "made.txt").exists(), "writes in the workspace reach the host")

    def test_time_limit_kills_and_reports(self):
        r = sandbox.run(["python3", "-c", "import time; time.sleep(30)"], self.work, timeout=3, mode=self.mode)
        self.assertTrue(r.timed_out)
        self.assertIsNone(r.returncode)
        self.assertLess(r.seconds, 15)

    def test_failing_command_is_a_result_not_an_exception(self):
        r = sandbox.run(["python3", "-c", "raise SystemExit(3)"], self.work, mode=self.mode)
        self.assertEqual(r.returncode, 3)
        self.assertFalse(r.ok)

    def test_probe_contract(self):
        (self.probes / "good.py").write_text(
            "import json, sys, pathlib\n"
            "print('noise')\n"
            "print(json.dumps({'ok': pathlib.Path(sys.argv[1], 'flag').exists(), 'option': 'B'}))\n")
        (self.probes / "crash.py").write_text("raise RuntimeError('boom')\n")
        (self.probes / "slow.py").write_text("import time; time.sleep(30)\n")
        self.assertEqual(sandbox.run_probe("good", self.work, self.probes, mode=self.mode), {"ok": False, "option": "B"})
        (self.work / "flag").write_text("")
        self.assertEqual(sandbox.run_probe("good", self.work, self.probes, mode=self.mode), {"ok": True, "option": "B"})
        crashed = sandbox.run_probe("crash", self.work, self.probes, mode=self.mode)
        self.assertEqual((crashed["ok"], crashed["option"]), (False, None))
        self.assertIn("boom", crashed["error"])
        slow = sandbox.run_probe("slow", self.work, self.probes, timeout=3, mode=self.mode)
        self.assertEqual(slow, {"ok": False, "option": None, "error": "timeout"})


class SubprocessSandbox(SandboxCase, unittest.TestCase):
    mode = "subprocess"


@unittest.skipUnless(sandbox.docker_available(), "Docker or the python:3.12-slim image is not available")
class DockerSandbox(SandboxCase, unittest.TestCase):
    mode = "docker"

    def test_network_is_blocked(self):
        code = ("import socket\n"
                "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=3); print('OPEN')\n"
                "except OSError:\n    print('BLOCKED')\n")
        r = sandbox.run(["python3", "-c", code], self.work, mode="docker")
        self.assertIn("BLOCKED", r.stdout)

    def test_probe_mount_is_read_only(self):
        (self.probes / "write.py").write_text(
            "import json, pathlib\n"
            "try:\n    pathlib.Path(__file__).with_name('x').write_text('1'); ok = True\n"
            "except OSError:\n    ok = False\n"
            "print(json.dumps({'ok': ok, 'option': None}))\n")
        self.assertEqual(sandbox.run_probe("write", self.work, self.probes, mode="docker"), {"ok": False, "option": None})


if __name__ == "__main__":
    unittest.main()
