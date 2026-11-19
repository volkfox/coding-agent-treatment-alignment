"""W9: the command line (`rj ...`, rj/cli.py) against the local stub and httpx.MockTransport. Plan sections 3, 6, 10, 12, 13.

No test here reaches another machine, and none reads the repository's .env: RJ_ENV_FILE names a file under
the tests' temporary directory for the whole module. Every command works on a temporary copy of a matrix file
and in a temporary runs directory; the module checks that the repository's config files, its top level and
its runs/ are as they were.
"""
import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import time
import unittest
import unittest.mock
import zipfile
from pathlib import Path

import httpx

from rj import cli, preflight, sandbox, schema, script
from rj.hr import HRClient, HRError
from rj.runner import budget as budgets
from rj.runner import machine, manifest
from rj.stub.server import serve

ROOT = schema.ROOT
CONFIG = ("matrix.yaml", "matrix.stub.yaml", "profiles.yaml", "prices.yaml", "grader.yaml")
PLANTED = "rjplanted-key-0123456789abcdef"          # nobody's key

SERVER = None
TMP: Path = None
_env = None
_before = None


def snapshot() -> dict:
    runs = ROOT / "runs"
    return {"top": sorted(p.name for p in ROOT.iterdir() if p.name != "__pycache__"),
            "config": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in CONFIG},
            "runs": {p.relative_to(ROOT).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(runs.rglob("*")) if p.is_file()}}


def setUpModule():
    global SERVER, TMP, _env, _before
    _before = snapshot()
    TMP = Path(tempfile.mkdtemp(prefix="rj-cli-test-"))
    SERVER = serve(port=0, turn_seconds=0.02, keepalive=0.2)
    _env = unittest.mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
                                                 "HR_BASE_URL": SERVER.url, "TR_BASE_URL": SERVER.tr_url, "RJ_SANDBOX": "subprocess",
                                                 cli.ENV_NAME: str(TMP / "no-such.env")})
    _env.start()
    for name in [n for n in os.environ if n.endswith("_KEY")]:
        os.environ.pop(name)


def tearDownModule():
    SERVER.shutdown()
    _env.stop()
    shutil.rmtree(TMP, ignore_errors=True)
    assert snapshot() == _before, "the tests wrote into the repository"


def new_dir(prefix: str = "work-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir=TMP))


def rj(*argv) -> tuple[int, str]:
    """One command line, in this process. Returns (exit code, everything it printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = cli.main([str(a) for a in argv])
        except SystemExit as e:
            code = e.code
    return code, out.getvalue()


def load(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()] if Path(path).exists() else []


def posts(server=None) -> int:
    stub = (server or SERVER).stub
    with stub.lock:
        return sum(1 for method, path in stub.log if (method, path) == ("POST", "/v1/responses"))


def workspace(matrix: str = "matrix.stub.yaml", profile: str = "offline") -> tuple[Path, Path, list]:
    """A temporary copy of a matrix file, a temporary runs directory, and the options that name both."""
    work = new_dir()
    copy = work / matrix
    shutil.copyfile(ROOT / matrix, copy)
    return copy, work / "runs", ["--profile", profile, "--matrix", copy, "--runs-dir", work / "runs"]


def service(bases: dict, model_map: dict) -> httpx.MockTransport:
    """GET /v1/bases and /v1/integrations as given: {base: {model: available}} and {model: connection}."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/bases":
            return httpx.Response(200, json={"bases": [{"id": b, "models": [{"id": m, "available": ok} for m, ok in models.items()]}
                                                       for b, models in bases.items()]})
        if request.method == "GET" and request.url.path == "/v1/integrations":
            return httpx.Response(200, json={"integrations": [{"name": "default", "provider": "tokenrouter"},
                                                              {"name": "anthropic", "provider": "anthropic"}], "model_map": model_map})
        return httpx.Response(404, json={"error": f"not in the test's service: {request.method} {request.url.path}"})
    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- add-row

class AddRow(unittest.TestCase):
    def test_the_default_id_drops_every_character_outside_a_z_0_9_from_the_model(self):
        self.assertEqual(cli.default_row_id("qwen", "claude-haiku-4.5"), "qwen-claudehaiku45")
        self.assertEqual(cli.default_row_id("stub", "persona:asker"), "stub-personaasker")
        self.assertEqual(cli.default_row_id("claude-code", "Claude_Sonnet 5.5"), "claude-code-laudeonnet55", "capitals are outside a-z too")

    def test_one_line_is_appended_and_nothing_else_changes(self):
        matrix, _, common = workspace()
        before = matrix.read_bytes()
        code, out = rj("add-row", "stub", "persona:asker", *common)
        self.assertEqual(code, 0, out)
        after = matrix.read_bytes()
        self.assertTrue(after.startswith(before), "the file is only added to")
        self.assertEqual(after[len(before):].decode(), "- {id: stub-personaasker, base: stub, model: 'persona:asker', role: optional}\n")
        rows = schema.load_matrix(matrix)
        self.assertEqual(rows[-1], {"id": "stub-personaasker", "base": "stub", "model": "persona:asker", "role": "optional"})
        self.assertEqual(len(rows), 7)
        self.assertIn("rj row stub-personaasker --live --profile offline", out)

    def test_refusals_on_the_stub(self):
        matrix, _, common = workspace()
        before = matrix.read_bytes()
        for argv, why in ((["stub", "persona:asker", "--id", "s-asker"], "row id 's-asker' is already in"),
                          (["nosuch", "persona:asker"], "unknown base 'nosuch': the service lists stub"),
                          (["stub", "persona:nobody"], "base 'stub' does not list model 'persona:nobody'"),
                          (["stub", "persona:asker", "--id", "Not/An:Id"], "cannot be a row id"),
                          (["stub", "persona:asker", "--id", "s-new", "--next", "s-missing"], "--next 's-missing' is no row")):
            code, out = rj("add-row", *argv, *common)
            self.assertEqual(code, 2, out)
            self.assertIn(why, out)
            self.assertIn("refused:", out)
        self.assertEqual(matrix.read_bytes(), before, "a refusal leaves the file as it was")
        code, out = rj("add-row", "stub", "persona:asker", "--id", "s-new", "--next", "s-asker", "--side", "extra", *common)
        self.assertEqual(code, 0, out)
        self.assertEqual(schema.load_matrix(matrix)[-1], {"id": "s-new", "base": "stub", "model": "persona:asker", "side": "extra",
                                                         "role": "optional", "next": "s-asker"})
        code, out = rj("add-row", "stub", "persona:asker", "--id", "s-new", *common)
        self.assertEqual(code, 2, "the id just added is a duplicate now")

    def test_what_the_service_says_decides(self):
        matrix, _, common = workspace("matrix.yaml", "demo")
        before = matrix.read_bytes()
        transport = service({"qwen": {"claude-haiku-4.5": True, "qwen-off": False, "orphan": True}, "kimi": {"kimi-k2.7-code": True}},
                            {"claude-haiku-4.5": "anthropic", "qwen-off": "default"})
        with unittest.mock.patch.object(cli, "TRANSPORT", transport):
            for argv, why in ((["qwen", "qwen-off"], "base 'qwen' lists model 'qwen-off' as not available"),
                              (["qwen", "orphan"], "model 'orphan' maps to no connection"),
                              (["qwen", "kimi-k2.7-code"], "base 'qwen' does not list model 'kimi-k2.7-code'"),
                              (["codex", "claude-haiku-4.5"], "unknown base 'codex'")):
                code, out = rj("add-row", *argv, *common)
                self.assertEqual(code, 2, out)
                self.assertIn(why, out)
            self.assertEqual(matrix.read_bytes(), before)
            code, out = rj("add-row", "qwen", "claude-haiku-4.5", "--id", "qwen-cross", "--side", "cross", *common)
        self.assertEqual(code, 0, out)
        self.assertEqual(matrix.read_bytes()[len(before):].decode(),
                         "- {id: qwen-cross, base: qwen, model: claude-haiku-4.5, side: cross, role: optional}\n")
        self.assertIn("paid by connection 'anthropic'", out)
        self.assertNotIn("prices.yaml has no entry", out, "the model is priced")
        self.assertEqual(len(schema.load_matrix(matrix)), len(schema.load_matrix("matrix.yaml")) + 1)
        # the demo profile's cross stage can now be planned, on the row just added
        cfg = {**schema.load_profile("demo"), "matrix": str(matrix)}
        cross = manifest.plan(cfg, only="cross")
        self.assertEqual([e["key"] for e in cross], ["qwen-cross/N/possible/A/r01", "qwen-cross/N/possible/A/r02", "qwen-cross/F/possible/A/r01"])
        self.assertEqual(manifest.rows_of(cross, schema.load_matrix(matrix))[0]["model"], "claude-haiku-4.5")

    def test_a_model_without_a_price_is_added_with_a_warning(self):
        matrix, _, common = workspace("matrix.yaml", "demo")
        with unittest.mock.patch.object(cli, "TRANSPORT", service({"kimi": {"kimi-k9": True}}, {"kimi-k9": "default"})):
            code, out = rj("add-row", "kimi", "kimi-k9", *common)
        self.assertEqual(code, 0, out)
        self.assertEqual(schema.load_matrix(matrix)[-1]["id"], "kimi-kimik9")
        self.assertIn("prices.yaml has no entry for 'kimi-k9'; the cost guard refuses the row until it has one", out)
        self.assertIn("grader.yaml `names` does not hold 'kimi-kimik9'", out)

    def test_a_stub_matrix_never_asks_another_machine(self):
        matrix, _, common = workspace()
        before = matrix.read_bytes()
        with unittest.mock.patch.dict(os.environ, {"HR_BASE_URL": "https://api.harnessrouter.invalid"}):
            code, out = rj("add-row", "stub", "persona:asker", *common)
        self.assertEqual(code, 2, out)
        self.assertIn("is not this machine", out)
        self.assertEqual(matrix.read_bytes(), before)


# --------------------------------------------------------------------------- replace

class Replace(unittest.TestCase):
    def test_replacements_are_written_merged_and_resolved_by_the_stages(self):
        runs = new_dir() / "runs"
        code, out = rj("replace", "cc-haiku45", "cc-sonnet55", "--runs-dir", runs)
        self.assertEqual(code, 0, out)
        self.assertEqual(load(runs / "replacements.json"), {"cc-haiku45": "cc-sonnet55"})
        self.assertIn("rj preflight --paid --rows cc-sonnet55", out)
        code, out = rj("replace", "kimi-k27", "dsh-v4pro", "--runs-dir", runs)
        self.assertEqual(code, 0, out)
        self.assertEqual(load(runs / "replacements.json"), {"cc-haiku45": "cc-sonnet55", "kimi-k27": "dsh-v4pro"})
        planned = manifest.plan(schema.load_profile("demo"), only="baseline", rows=["cc-haiku45", "kimi-k27"],
                                replacements=manifest.load_replacements(runs))
        self.assertEqual(list(dict.fromkeys(e["row"] for e in planned)), ["cc-sonnet55", "dsh-v4pro"])
        code, out = rj("replace", "cc-sonnet55", "cc-sonnet46", "--runs-dir", runs)
        self.assertEqual(code, 0, "a replacement may itself be replaced")
        self.assertEqual(manifest.resolve("cc-haiku45", manifest.load_replacements(runs)), "cc-sonnet46")

    def test_what_is_refused(self):
        runs = new_dir() / "runs"
        self.assertEqual(rj("replace", "cc-haiku45", "cc-sonnet55", "--runs-dir", runs)[0], 0)
        before = (runs / "replacements.json").read_bytes()
        for argv, why in ((["cc-haiku45", "no-such-row"], "no row 'no-such-row'"), (["no-such-row", "cc-haiku45"], "no row 'no-such-row'"),
                          (["kimi-k27", "kimi-k27"], "the same row"), (["cc-sonnet55", "cc-haiku45"], "in a circle")):
            code, out = rj("replace", *argv, "--runs-dir", runs)
            self.assertEqual(code, 2, out)
            self.assertIn(why, out)
        self.assertEqual((runs / "replacements.json").read_bytes(), before)
        self.assertIn("names cc-sonnet55 as its step-up, not codex-54", rj("replace", "cc-haiku45", "codex-54", "--runs-dir", runs)[1])


# --------------------------------------------------------------------------- the .env loader

class EnvLoader(unittest.TestCase):
    SECRET = "sk-rj-loader-0123456789-NEVER-PRINTED"
    TEXT = (f"# keys for the real services\n\nHR_API_KEY={SECRET}\nexport RJ_TEST_QUOTED=\"two words\"\nRJ_TEST_SINGLE='x=y'\n"
            "RJ_TEST_KEPT=from-the-file\nnot a pair\n9BAD=1\nRJ_TEST_EMPTY=\n")
    NAMES = ("HR_API_KEY", "RJ_TEST_QUOTED", "RJ_TEST_SINGLE", "RJ_TEST_KEPT", "RJ_TEST_EMPTY")

    def setUp(self):
        self.file = new_dir() / "test.env"
        self.file.write_text(self.TEXT, encoding="utf-8")
        env = unittest.mock.patch.dict(os.environ, {"RJ_TEST_KEPT": "from-the-shell"})
        env.start()
        self.addCleanup(env.stop)

    def test_pairs_are_loaded_and_no_value_is_printed_or_returned(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            done = cli.load_env(self.file)
        self.assertEqual(out.getvalue(), "", "the loader itself prints nothing")
        self.assertEqual(done, {"set": ["HR_API_KEY", "RJ_TEST_QUOTED", "RJ_TEST_SINGLE", "RJ_TEST_EMPTY"], "kept": ["RJ_TEST_KEPT"],
                                "skipped": [7, 8]}, "a line that is no pair is not passed over in silence")
        self.assertEqual([os.environ[n] for n in self.NAMES], [self.SECRET, "two words", "x=y", "from-the-shell", ""])
        self.assertNotIn("9BAD", os.environ)
        self.assertNotIn(self.SECRET, json.dumps(done))
        self.assertEqual(cli.load_env(self.file.with_name("absent.env")), {"set": [], "kept": [], "skipped": []}, "a missing file loads nothing")

    def test_a_byte_order_mark_a_trailing_comment_and_a_secret_under_another_name(self):
        """What an editor or a hand leaves in a .env: none of it may change a key, or hide one from the key scan."""
        token = "tok-rj-0123456789abcdef-NEVER-PRINTED"
        names = ("RJ_BOM_URL", "RJ_COMMENTED_KEY", "RJ_QUOTED_KEY", "RJ_HASH", "RJ_GATE_TOKEN", "RJ_DB_PASSWORD")
        self.file.write_bytes(("\ufeffRJ_BOM_URL=https://first.example\n"
                               f"RJ_COMMENTED_KEY={self.SECRET}   # the hackathon key\n"
                               f'RJ_QUOTED_KEY="{self.SECRET}"  # quoted, then a comment\n'
                               "RJ_HASH='a # b'#c\n"
                               f"RJ_GATE_TOKEN={token}\n"
                               "RJ_DB_PASSWORD=short\n").encode("utf-8"))
        with unittest.mock.patch.dict(os.environ), unittest.mock.patch.object(preflight, "EXTRA_SECRETS", set()):
            done = cli.load_env(self.file)
            self.assertEqual(done, {"set": list(names), "kept": [], "skipped": []})
            self.assertEqual([os.environ[n] for n in names], ["https://first.example", self.SECRET, self.SECRET, "a # b", token, "short"],
                             "the first line of a file that starts with a byte-order mark is loaded; a comment is no part of a value")
            self.assertEqual(preflight.EXTRA_SECRETS, {"RJ_COMMENTED_KEY", "RJ_QUOTED_KEY", "RJ_GATE_TOKEN", "RJ_DB_PASSWORD"})
            self.assertEqual(cli.key_needles(), {"RJ_COMMENTED_KEY": self.SECRET[:16].encode(), "RJ_QUOTED_KEY": self.SECRET[:16].encode(),
                                                 "RJ_GATE_TOKEN": token[:16].encode()}, "a token kept in .env is looked for like a key")
            self.assertNotIn(token, preflight.scrub(f"Authorization: Bearer {token}"), "and scrubbed from what is printed")
            # the scan opens an archive inside an archive
            run_dir = new_dir()
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as z:        # deflated: the bytes show nowhere but inside
                z.writestr("home/.netrc", f"password {token}\n" * 8)
            with zipfile.ZipFile(run_dir / "archive.zip", "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("backup/home.zip", inner.getvalue())
                z.writestr("README.md", "nothing here\n")
            self.assertEqual(cli.key_scan(run_dir), [f"$RJ_GATE_TOKEN in {run_dir / 'archive.zip'} (member backup/home.zip > home/.netrc)"])
        self.assertEqual(cli._env_value("'unclosed # kept"), "'unclosed", "an unclosed quote: the line is read as it stands, comment off")

    def test_a_line_that_is_skipped_is_said(self):
        runs = new_dir() / "runs"
        with unittest.mock.patch.dict(os.environ, {cli.ENV_NAME: str(self.file)}):
            code, out = rj("replace", "cc-haiku45", "cc-sonnet55", "--runs-dir", runs)
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: line 7, 8 of test.env is no NAME=value pair and was skipped", out)
        self.assertNotIn("not a pair", out, "the line's number, never its text")

    def test_a_real_profile_loads_it_and_says_only_names(self):
        runs = new_dir() / "runs"
        with unittest.mock.patch.dict(os.environ, {cli.ENV_NAME: str(self.file)}):
            code, out = rj("replace", "cc-haiku45", "cc-sonnet55", "--runs-dir", runs)
            self.assertEqual(code, 0, out)
            self.assertEqual(os.environ["HR_API_KEY"], self.SECRET, "in this process's environment")
            self.assertIn("env: HR_API_KEY, RJ_TEST_QUOTED, RJ_TEST_SINGLE, RJ_TEST_EMPTY set from test.env; already set and kept: RJ_TEST_KEPT", out)
            for value in (self.SECRET, self.SECRET[:16], "two words", "from-the-file", "from-the-shell"):
                self.assertNotIn(value, out)
            # an error that quotes the service is scrubbed of the key as well
            with unittest.mock.patch.object(cli, "TRANSPORT", httpx.MockTransport(
                    lambda request: httpx.Response(500, text=f"upstream said: bad key {self.SECRET} (prefix {self.SECRET[:16]}...)"))):
                code, out = rj("add-row", "qwen", "claude-haiku-4.5", "--id", "x", *workspace("matrix.yaml", "demo")[2])
            self.assertEqual(code, 2, out)
            self.assertIn("HTTP 500", out)
            self.assertIn(f"service: {SERVER.url} (cloud); TokenRouter at {SERVER.tr_url}; HR_API_KEY set, TR_API_KEY not set", out)
            self.assertNotIn(self.SECRET[:16], out)

    def test_the_stub_profile_loads_nothing(self):
        runs = new_dir() / "runs"
        with unittest.mock.patch.dict(os.environ, {cli.ENV_NAME: str(self.file)}):
            code, out = rj("replace", "s-asker", "s-guesser", "--profile", "offline", "--runs-dir", runs)
            self.assertEqual(code, 0, out)
            self.assertNotIn("HR_API_KEY", os.environ)
            self.assertNotIn("env:", out)


# --------------------------------------------------------------------------- run, grade, judge, aggregate, site

class Pipeline(unittest.TestCase):
    """One stub session through every command, once; the key scan is tried on what it left."""

    @classmethod
    def setUpClass(cls):
        cls.matrix, cls.runs, cls.common = workspace()
        cls.run_dir = cls.runs / "offline"
        cls.key = "s-asker/N/possible/A/r01"
        cls.sent = posts()
        cls.ran = rj("run", "--rows", "s-asker", "--smoke-only", *cls.common)
        cls.graded = rj("grade", *cls.common)
        cls.judged = rj("judge", *cls.common)
        cls.rolled = rj("aggregate", *cls.common)
        cls.built = rj("site", *cls.common)

    def test_a_run_prints_each_session_start_and_end_the_cost_so_far_and_what_remains(self):
        code, out = self.ran
        self.assertEqual(code, 0, out)
        printed = out.splitlines()
        start = printed.index(f"{self.key}: started (1 in flight)")
        end = next(i for i, line in enumerate(printed) if line.startswith(f"{self.key}: DONE (completed), "))
        self.assertLess(start, end)
        self.assertRegex(printed[end + 1], r"^    so far 0\.\d\d USD spent \(TokenRouter 0\.\d\d by \w+, other payers 0\.00 by the ledger\); "
                                           r"\d+\.\d\d USD remain above the floor; 0 session\(s\) of this selection left$",
                         "each part of the total is said with the figure it comes from")
        turns = [line for line in printed[start:end] if line.startswith(f"{self.key}: ") and " ended " in line]
        self.assertEqual(len(turns), 8, "one line per turn between a session's start and its end, not forty silent minutes")
        self.assertRegex(turns[1], rf"^{self.key}: kickoff\.s0 ended asked_plan after \d+\.\d min, 0\.\d\d USD by the ledger; spent so far 0\.\d\d USD$")
        self.assertEqual(printed[0], "sandbox: subprocess (this machine, no isolation)", "where the agents' code runs is said first")
        self.assertIn("sandbox: subprocess", self.graded[1])
        self.assertIn("  smoke rule, s-asker: passed", printed)
        budget = schema.load_profile("offline")["budget"]      # the owner's to set: no figure of it is written down here
        self.assertRegex(out, r"cost so far: 0\.\d\d USD .*remain above the " + re.escape(f"{budget['floor_usd']:.2f} floor of {budget['total_usd']:.2f}"))
        self.assertIn("skipped (5): smoke-only: not a row's first neutral session", out)
        self.assertEqual(posts() - self.sent >= 8, True)

    def test_a_finished_run_sends_nothing_and_a_redo_gives_a_session_another_life(self):
        _, runs, common = workspace()
        self.assertEqual(rj("run", "--rows", "s-guesser", "--smoke-only", *common)[0], 0)
        sent = posts()
        code, out = rj("run", "--rows", "s-guesser", "--smoke-only", *common)
        self.assertEqual((code, posts()), (0, sent), out)
        self.assertIn("finished before this call", out)
        code, out = rj("run", "--redo", "s-guesser/N/possible/A/r01", *common)
        self.assertEqual((code, posts()), (0, sent), out)
        self.assertTrue((runs / "offline" / "sessions" / "s-guesser/N/possible/A/r01.infra1").is_dir())
        code, out = rj("run", "--rows", "s-guesser", "--smoke-only", *common)
        self.assertEqual((code, posts() - sent), (0, 8), out)

    def test_a_real_profile_must_name_its_stages(self):
        runs = new_dir() / "runs"
        sent = posts()
        code, out = rj("run", "--runs-dir", runs)
        self.assertEqual((code, posts()), (2, sent), out)
        self.assertIn("name the stages to run: --through STAGE or --only STAGE (stages of demo: baseline, matrix, praise, late, forced, once, cross)", out)
        code, out = rj("run", "--only", "no-such-stage", "--runs-dir", runs)
        self.assertEqual(code, 2, out)
        self.assertIn("has no stage 'no-such-stage'", out)
        self.assertFalse(runs.exists(), "nothing was written for either")

    def test_grade_judge_aggregate_and_site_are_thin_calls_that_work(self):
        self.assertEqual([self.graded[0], self.judged[0], self.rolled[0], self.built[0]], [0, 0, 0, 0],
                         "\n".join([self.graded[1], self.judged[1], self.rolled[1], self.built[1]]))
        self.assertIn("graded 1 of 6 sessions; 5 not run or not over yet", self.graded[1], "a session that has not run is no failure")
        session = self.run_dir / "sessions" / self.key
        grade = schema.validate("GRADE", load(session / "grade.json"))
        self.assertEqual((grade["outcome"], grade["effort"]["judge"]["model"]), ("completed", "stub-judge"))
        results = schema.validate("RESULTS", load(self.run_dir / "results.json"))
        self.assertEqual((results["source"], [s["key"] for s in results["sessions"]]), ("stub", [self.key]))
        row = results["rows"]["s-asker"]
        self.assertEqual((row["payer"], row["provider"], row["measured"]["source"], results["cost"]["measured"]),
                         ("default", "tokenrouter", "ledger", False),
                         "who pays is in the results; a cost the ledger summed itself is not a measured one")
        self.assertGreater(results["cost"]["run"]["turns_usd"], 0)
        page = (self.run_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-pay="tokenrouter"', page)
        self.assertIn("estimate", page)
        self.assertIn('class="watermark"', (self.run_dir / "index.html").read_text(encoding="utf-8"))
        self.assertFalse((self.runs / ".grade-tmp").exists(), "the grader's scratch directory is under the runs directory in use, and gone")
        self.assertIn("judge: profile offline, 1 session(s), model stub-judge", self.judged[1])

    def test_grade_also_writes_what_is_computed_after_a_run_and_each_step_takes_up_where_it_stopped(self):
        """`rj grade`: grade.json, then readouts.json and refine.json. `rj readouts` and `rj refine` write one of them alone,
        and touch nothing else; a second pass computes nothing again."""
        code, out = self.graded
        self.assertEqual(code, 0, out)
        said = [line for line in out.splitlines() if " of 6 sessions" in line]
        self.assertEqual(said, ["graded 1 of 6 sessions; 5 not run or not over yet", "readouts for 1 of 6 sessions; 5 not run or not over yet",
                                "refined 1 of 6 sessions; 5 not run or not over yet"], "one line a step, in the order they ran")
        self.assertEqual(out.count(", skipped"), 5, "the sessions that have not run are named once, by the grader")
        session = self.run_dir / "sessions" / self.key
        refined = schema.validate("REFINE", load(session / "refine.json"))
        readout = schema.validate("READOUTS", load(session / "readouts.json"))
        self.assertEqual((refined["score17"], refined["score17_partial"], [h["name"] for h in refined["handbacks"]], refined["defects"]),
                         (17, 17.0, ["part_a", "part_b", "review"], []), "the asker builds the full reference")
        self.assertEqual((readout["workspace"], readout["record_rows_shown"], readout["scratchpad"]["planted"]), ("plain", 3, False))
        listed = load(self.run_dir / "results.json")["sessions"][0]
        self.assertEqual((listed["quality"]["score17_partial"], listed["quality"]["position"], listed["record"]["rows_shown"]),
                         (17.0, "one neutral run", 3), "and the roll-up carries them")

        def stamps() -> dict:
            return {p.relative_to(session).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(session.rglob("*")) if p.is_file()}

        before = stamps()
        code, out = rj("readouts", *self.common)
        self.assertEqual(code, 0, out)
        self.assertIn(f"{self.key}: plain, rows shown 3/3; ", out)
        self.assertIn("readouts for 1 of 6 sessions; 5 not run or not over yet", out)
        code, out = rj("refine", *self.common)
        self.assertEqual(code, 0, out)
        self.assertIn(f"{self.key}: hidden 12 whole, 12 partial; score17 17, partial 17; ", out)
        self.assertIn("kept as it was, nothing run", out, "a refine.json that is current is not computed again")
        self.assertIn("refined 1 of 6 sessions; 5 not run or not over yet", out)
        self.assertEqual(stamps(), before, "neither step wrote a file: what was there is current")
        code, out = rj("refine", "--force", "--session", self.key, *self.common)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"\d+\.\d s in the sandbox")
        self.assertIn("sandbox: subprocess", out)
        self.assertEqual(stamps(), before, "computed again, the same bytes: grade.json and everything else as they were")
        # a run graded before these steps existed: each command fills its file in and leaves grade.json alone
        (session / "refine.json").unlink()
        (session / "readouts.json").unlink()
        self.assertEqual((rj("readouts", "--session", self.key, *self.common)[0], rj("refine", "--session", self.key, *self.common)[0]), (0, 0))
        after = stamps()
        self.assertEqual({name: data for name, (data, _) in after.items()}, {name: data for name, (data, _) in before.items()})
        self.assertEqual(sorted(name for name in after if after[name][1] != before[name][1]), ["readouts.json", "refine.json"])
        self.assertFalse((self.runs / ".grade-tmp").exists())
        # grade.json alone
        (session / "refine.json").unlink()
        code, out = rj("grade", "--grade-only", *self.common)
        self.assertEqual((code, (session / "refine.json").exists()), (0, False), out)
        self.assertNotIn("refined", out)
        self.assertEqual(rj("grade", *self.common)[0], 0)
        self.assertEqual((session / "refine.json").read_bytes(), before["refine.json"][0])
        code, out = rj("refine", "--session", "no/such/session/A/r01", *self.common)
        self.assertEqual(code, 2, out)

    def test_what_a_stub_run_leaves_is_never_read_as_the_real_accounts(self):
        """The stub has a runs directory of its own; and where both are given one directory, the stamps keep them apart."""
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            stubbed = cli.setup(cli.parser().parse_args(["run", "--profile", "offline"]))
            checked = cli.setup(cli.parser().parse_args(["preflight", "--free", "--profile", "offline"]))
            real = cli.setup(cli.parser().parse_args(["run", "--through", "baseline"]))
            named = cli.setup(cli.parser().parse_args(["run", "--profile", "offline", "--runs-dir", "runs"]))
            cli.setup(cli.parser().parse_args(["site", "--profile", "offline"]))
        self.assertEqual((stubbed.runs, checked.runs, real.runs, named.runs), (ROOT / "runs-stub", ROOT / "runs-stub", ROOT / "runs", ROOT / "runs"))
        self.assertEqual(said.getvalue().count(f"runs directory: {ROOT / 'runs-stub'} (stub rows keep their runs apart"), 2,
                         "said by the commands that write there")
        self.assertFalse((ROOT / "runs-stub").exists(), "setting up writes nothing")
        # this class's run was the stub's: its ledger lines, its agent-work verdict and its measured cost carry its stamp
        mine = budgets.service_of(SERVER.url)
        turns = [e for e in lines(self.runs / "ledger.jsonl") if e["kind"] == "turn"]
        self.assertTrue(turns and all(e["service"] == mine for e in turns))
        self.assertEqual((load(self.runs / "agent_work.json")["verdict"], load(self.runs / "agent_work.json")["service"]), ("free", mine))
        demo, prices = schema.load_profile("demo"), schema.load_prices()
        self.assertGreater(budgets.Budget(demo["budget"], prices, self.runs, run="demo").state()["spent_usd"], 0, "the stub's own guard counts them")
        with unittest.mock.patch.dict(os.environ, {"HR_BASE_URL": "https://api.harnessrouter.example"}):
            state = budgets.Budget(demo["budget"], prices, self.runs, run="demo").state()
            self.assertEqual((state["spent_usd"], state["remaining_usd"]), (0.0, demo["budget"]["total_usd"] - demo["budget"]["floor_usd"]),
                             "the real account's guard does not")
            with contextlib.redirect_stderr(io.StringIO()):
                shared = cli.setup(cli.parser().parse_args(["run", "--through", "baseline", "--runs-dir", str(self.runs)]))
            self.assertEqual(cli.agent_work(shared), "cannot_tell", "nor is the stub's flat balance the real account's check 9")
            self.assertNotIn("s-asker", budgets.prices_with_measured(self.runs, prices)["measured"])
        self.assertEqual(cli.agent_work(cli.setup(cli.parser().parse_args(["grade", *map(str, self.common)]))), "free")

    def test_agents_code_is_not_run_on_this_machine_unasked(self):
        """Docker missing on a real profile: refused, not a silent fall back to the host."""
        runs, sent = new_dir() / "runs", posts()
        with unittest.mock.patch.object(sandbox, "docker_available", lambda: False), unittest.mock.patch.dict(os.environ):
            os.environ.pop("RJ_SANDBOX", None)
            code, out = rj("run", "--only", "baseline", "--runs-dir", runs)
            self.assertEqual((code, posts()), (2, sent), out)
            self.assertIn("refused: Docker is not there", out)
            self.assertIn("pass --sandbox subprocess to accept that", out)
            self.assertFalse(runs.exists(), "nothing was written and nothing sent")
            with contextlib.redirect_stderr(io.StringIO()) as said:
                real = cli.setup(cli.parser().parse_args(["grade", "--runs-dir", str(runs)]))
                self.assertEqual(cli.sandbox_mode(real, argparse.Namespace(sandbox="subprocess"), "rj grade"), "subprocess")
                with unittest.mock.patch.dict(os.environ, {"RJ_SANDBOX": "subprocess"}):
                    self.assertEqual(cli.sandbox_mode(real, argparse.Namespace(sandbox=None), "rj grade"), "subprocess")
                with self.assertRaises(cli.Refused):
                    cli.sandbox_mode(real, argparse.Namespace(sandbox=None), "rj grade")
                stubbed = cli.setup(cli.parser().parse_args(["grade", *map(str, self.common)]))
                self.assertEqual(cli.sandbox_mode(stubbed, argparse.Namespace(sandbox=None), "rj grade"), "subprocess",
                                 "the stub's agents write the repository's own reference code")
            self.assertIn("sandbox: subprocess (this machine, no isolation), as asked", said.getvalue())
        with unittest.mock.patch.object(sandbox, "docker_available", lambda: True), unittest.mock.patch.dict(os.environ), \
                contextlib.redirect_stderr(io.StringIO()) as said:
            os.environ.pop("RJ_SANDBOX", None)
            self.assertEqual(cli.sandbox_mode(real, argparse.Namespace(sandbox=None), "rj grade"), "docker")
        self.assertIn("sandbox: docker (python:3.12-slim, no network)", said.getvalue())

    def test_a_service_that_gives_no_reply_is_said_at_once(self):
        _, runs, common = workspace()
        self.assertEqual(rj("add-row", "stub", "persona:asker", "--id", "s-live", *common)[0], 0)
        with unittest.mock.patch.dict(os.environ, {"HR_BASE_URL": "http://127.0.0.1:9", "TR_BASE_URL": "http://127.0.0.1:9/tr"}):
            for argv in (["run", "--rows", "s-asker", "--smoke-only"], ["row", "s-live", "--live"]):
                started = time.monotonic()
                code, out = rj(*argv, *common)
                self.assertEqual(code, 2, out)
                self.assertIn("refused: no reply from http://127.0.0.1:9", out)
                self.assertIn("start `rj stub`, or point HR_BASE_URL at the one that runs; nothing was sent", out)
                self.assertLess(time.monotonic() - started, 20, "not after half an hour of patient retries")
        self.assertFalse((runs / "offline").exists(), "and nothing was planned or written")

    def test_a_session_that_outruns_its_projection_is_held_at_the_total_and_the_run_does_not_exit_0(self):
        """All but 5 USD of the total are spent; the stage fits on the estimate; then every turn costs far more than was
        projected. The total is the owner's to set, so it is read from the profile; the floor is taken as its 3 USD."""
        stub, total = SERVER.stub, schema.load_profile("offline")["budget"]["total_usd"]
        self.assertEqual(schema.load_profile("offline")["budget"]["floor_usd"], 3)
        for cents, turns, how in ((90.0, 6, "the total (or the wallet figure) is used up"), (60.0, 8, "the floor is broken into")):
            _, runs, common = workspace()
            runs.mkdir(parents=True)
            (runs / "ledger.jsonl").write_text(json.dumps({"kind": "judge", "cost_usd": total - 5.0, "provider": "tokenrouter"}) + "\n")
            with stub.lock:
                stub.tr_usage_cents, stub.tr_cents_per_turn = 0.0, cents
            try:
                sent = posts()
                code, out = rj("run", "--rows", "s-asker", "--smoke-only", *common)
            finally:
                with stub.lock:
                    stub.tr_usage_cents, stub.tr_cents_per_turn = 0.0, 0.0
            self.assertEqual((code, posts() - sent), (2, turns), out)
            self.assertIn(f"OVER BUDGET: {how}", out)
            self.assertTrue(total - 1 <= float(re.search(r"cost so far: (\d+\.\d\d) USD", out).group(1)) < total + 10, out)
            if turns < 8:
                self.assertIn("the budget is used up", out)
                self.assertIn("raise budget.total_usd in profiles.yaml", out)
                self.assertIn("left as its journal stands, to be resumed by the next call", out)
            else:
                self.assertIn("s-asker/N/possible/A/r01: DONE (completed)", out, "a session may eat into the floor; the exit says so")

    def test_rj_row_on_a_row_whose_stage_has_run_says_what_it_stands_for(self):
        _, runs, common = workspace()
        self.assertEqual(rj("run", "--rows", "s-guesser", "--smoke-only", *common)[0], 0)
        sent = posts()
        code, out = rj("row", "s-guesser", *common)
        self.assertEqual(code, 0, out)
        self.assertIn("note: 3 of the 3 sessions `rj row s-guesser` stands for are this run's already, planned under stage all: the "
                      "same keys, so the same sessions. 1 of them is over and not run again; the other 2 run now.", out)
        self.assertEqual(posts() - sent, 16, "the two that were not over ran; the first neutral session was not sent again")
        code, out = rj("row", "s-guesser", *common)
        self.assertEqual((code, posts() - sent), (0, 16), out)
        self.assertIn("3 of them are over and not run again. More of this row is a stage's to add (rj run --only STAGE --rows s-guesser)", out)

    def test_commands_on_a_run_that_does_not_exist_say_so(self):
        runs = new_dir() / "runs"
        for command in ("grade", "judge", "aggregate", "site"):
            code, out = rj(command, "--profile", "offline", "--runs-dir", runs)
            self.assertEqual(code, 2, out)
            self.assertIn("holds no manifest.jsonl", out)

    def test_a_paid_judge_is_ledgered_once_between_two_readings_of_the_usage_figure(self):
        """The demo profile's judge, asked at the stub's chat route: what it cost goes into the one ledger."""
        common = ["--profile", "demo", "--run", "offline", "--runs-dir", self.runs]
        self.assertEqual([e for e in lines(self.runs / "ledger.jsonl") if e["kind"] == "judge"], [], "the stub judge cost nothing")
        with unittest.mock.patch.dict(os.environ, {"TR_API_KEY": "tr-test-key-0123456789abcdef"}):
            code, out = rj("judge", *common)
            self.assertEqual(code, 0, out)
            ledger = lines(self.runs / "ledger.jsonl")
            judged = [e for e in ledger if e["kind"] == "judge"]
            self.assertEqual([(e["session"], e["provider"], e["calls"], e["run"]) for e in judged], [(self.key, "tokenrouter", 4, "offline")],
                             "two scoring runs, the guess and the fit call")
            self.assertGreater(judged[0]["cost_usd"], 0)
            self.assertAlmostEqual(judged[0]["cost_usd"], sum(u["cost_usd"] for u in load(self.run_dir / "sessions" / self.key / "judge.json")["usage"]))
            self.assertEqual([e["where"] for e in ledger if e["kind"] == "tr_usage"][-2:], ["judge_start", "judge_end"])
            self.assertRegex(out, r"judge: 0\.\d{4} USD by the ledger")
            code, out = rj("judge", *common)
            self.assertEqual(code, 0, out)
            self.assertEqual(len([e for e in lines(self.runs / "ledger.jsonl") if e["kind"] == "judge"]), 1, "a judge.json that is reused is not paid for again")
            # a session judged before the fit call existed: that one call is asked, and it alone is ledgered
            path = self.run_dir / "sessions" / self.key / "judge.json"
            old = load(path)
            self.assertEqual((old["asked"], [u["call"] for u in old["usage"]]), (["scoring", "scoring", "guess", "fit"],) * 2)
            path.write_text(json.dumps({**{k: v for k, v in old.items() if k not in ("fit", "asked")}, "usage": old["usage"][:3]}))
            code, out = rj("judge", *common)
            self.assertEqual(code, 0, out)
            judged = [e for e in lines(self.runs / "ledger.jsonl") if e["kind"] == "judge"]
            self.assertEqual([(e["session"], e["calls"]) for e in judged], [(self.key, 4), (self.key, 1)])
            now = load(path)
            self.assertEqual((now["asked"], now["runs"], now["usage"][:3]), (["fit"], old["runs"], old["usage"][:3]), "its scores were kept")
            self.assertAlmostEqual(judged[1]["cost_usd"], now["usage"][3]["cost_usd"])
            self.assertLess(judged[1]["cost_usd"], judged[0]["cost_usd"])
            self.assertEqual(rj("judge", *common)[0], 0)
            self.assertEqual(len([e for e in lines(self.runs / "ledger.jsonl") if e["kind"] == "judge"]), 2, "and then nothing more")
            # asked again by force with the budget all but spent: refused before anything is sent. What a session's judging
            # is expected to cost is no longer the 0.35 USD estimate: the ledger holds a judged session, both its passes
            guard = budgets.Budget(schema.load_profile("demo")["budget"], schema.load_prices(), self.runs, run="offline")
            share, floor = sum(e["cost_usd"] for e in judged), guard.floor
            self.assertEqual(guard.judge_usd(), (share, "ledger"))
            self.assertLess(share, budgets.JUDGE_USD)
            saved = (self.runs / "ledger.jsonl").read_bytes()
            with open(self.runs / "ledger.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"kind": "turn", "cost_usd": guard.left() - 0.5, "provider": "anthropic"}) + "\n")
            stamp = (self.run_dir / "sessions" / self.key / "judge.json").stat().st_mtime_ns
            code, out = rj("judge", "--force", *common)
            (self.runs / "ledger.jsonl").write_bytes(saved)
            self.assertEqual((code, (self.run_dir / "sessions" / self.key / "judge.json").stat().st_mtime_ns), (2, stamp), out)
            self.assertIn(f"judging 1 session(s) is expected to cost up to {share:.2f} USD ({share:.2f} each)", out)
            # the floor is the judge's floor too: what is left of the total is the floor and half of what the judging needs
            with open(self.runs / "ledger.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"kind": "turn", "cost_usd": guard.left() - floor - share / 2, "provider": "anthropic"}) + "\n")
            code, out = rj("judge", "--force", *common)
            self.assertEqual(code, 2, out)
            self.assertRegex(out, r"0\.\d\d USD remain above the " + re.escape(f"{floor:.2f}") + r" floor of budget\.total_usd")
            code, out = rj("judge", "--force", "--tr-wallet-usd", "3.2", *common[:-2], "--runs-dir", self.runs)
            self.assertEqual(code, 2, "and so is the wallet figure")
            (self.runs / "ledger.jsonl").write_bytes(saved)
            code, out = rj("judge", "--tr-wallet-usd", "3.2", *common)
            self.assertEqual(code, 0, "a judge.json made under today's prompt and model is reused, costs nothing and is refused by no guard")
            # calls that were answered and left no judge.json (a session whose judging failed) are ledgered as an estimate
            def failing(argv, client=None):
                for _ in range(2):
                    client.post(SERVER.tr_url + "/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})
                return 1
            with unittest.mock.patch.object(cli.judge, "main", failing):
                code, out = rj("judge", "--force", *common)
            self.assertEqual(code, 1, out)
            guessed = [e for e in lines(self.runs / "ledger.jsonl") if e.get("flag") == "estimate"]
            self.assertEqual([(e["kind"], e["calls"], e["provider"], e["session"]) for e in guessed], [("judge", 2, "tokenrouter", None)])
            self.assertAlmostEqual(guessed[0]["cost_usd"], 2 * 0.35 / 3)
            self.assertIn("2 call(s) left no judge.json to cost them by", out)
            (self.runs / "ledger.jsonl").write_bytes(saved)
        self.assertEqual(rj("judge", *self.common)[0], 0, "and the stub judge again, for the tests after this one")

    # ----------------------------------------------------------------------- the key scan

    def scan(self, plant, name: str = "RJ_PLANTED_KEY", value: str = PLANTED) -> tuple[tuple, tuple]:
        """Run judge and site with a key in the environment and its first 16 characters planted in a file of the run."""
        session = self.run_dir / "sessions" / self.key
        target = plant(session)
        saved = target.read_bytes()
        stamps = [(p, p.stat().st_mtime_ns) for p in (session / "judge.json", self.run_dir / "index.html")]
        try:
            with unittest.mock.patch.dict(os.environ, {name: value}):
                judged, built = rj("judge", *self.common), rj("site", *self.common)
        finally:
            target.write_bytes(saved)
        self.untouched = all(p.stat().st_mtime_ns == stamp for p, stamp in stamps)
        return judged, built

    def assert_stopped(self, got: tuple, where: str, undone: str) -> None:
        code, out = got
        self.assertEqual(code, 2, out)
        self.assertIn(f"stopped: key material in the run's files; {undone}.", out)
        self.assertIn("$RJ_PLANTED_KEY in ", out)
        self.assertIn(where, out)
        self.assertIn("Rotate that key", out)
        self.assertNotIn(PLANTED[:16], out, "the key is named, never shown")

    def test_a_key_in_a_response_stops_judge_and_site(self):
        def plant(session: Path) -> Path:
            target = session / "turns" / "02-plan_reply.s0" / "response.json"
            reply = load(target)
            reply["output"].append({"type": "message", "content": [{"type": "output_text", "text": f"TR_API_KEY={PLANTED[:16]}..."}]})
            target.write_text(json.dumps(reply), encoding="utf-8")
            return target

        judged, built = self.scan(plant)
        self.assert_stopped(judged, "02-plan_reply.s0/response.json", "nothing was sent to the judge")
        self.assert_stopped(built, "02-plan_reply.s0/response.json", "the page was not built")
        self.assertTrue(self.untouched, "judge.json and index.html are as they were")

    def test_a_key_inside_an_archive_or_a_trace_stops_them_too(self):
        def in_archive(session: Path) -> Path:
            target = session / "turns" / "06-review.s0" / "archive.zip"
            with zipfile.ZipFile(target, "a", zipfile.ZIP_DEFLATED) as z:
                z.writestr("notes/env.txt", f"export KEY={PLANTED}\n")
            return target

        def in_trace(session: Path) -> Path:
            target = session / "turns" / "01-kickoff.s0" / "trace.ndjson"
            with open(target, "ab") as f:
                f.write(json.dumps({"type": "user", "message": PLANTED[:16]}).encode() + b"\n")
            return target

        judged, built = self.scan(in_archive)
        self.assert_stopped(judged, "06-review.s0/archive.zip (member notes/env.txt)", "nothing was sent to the judge")
        self.assert_stopped(built, "06-review.s0/archive.zip (member notes/env.txt)", "the page was not built")
        judged, built = self.scan(in_trace)
        self.assert_stopped(judged, "01-kickoff.s0/trace.ndjson", "nothing was sent to the judge")
        self.assert_stopped(built, "01-kickoff.s0/trace.ndjson", "the page was not built")
        self.assertTrue(self.untouched)

    def test_only_keys_of_16_characters_or_more_named_like_keys_are_looked_for(self):
        def plant(session: Path) -> Path:
            target = session / "turns" / "01-kickoff.s0" / "trace.ndjson"
            with open(target, "ab") as f:
                f.write(b"short-key-15-ch! and rjplanted-key-01 as well\n")
            return target

        judged, built = self.scan(plant, "RJ_PLANTED_TOKEN", PLANTED)
        self.assertEqual((judged[0], built[0]), (0, 0), judged[1] + built[1])
        judged, built = self.scan(plant, "RJ_SHORT_KEY", "short-key-15-ch")
        self.assertEqual(built[0], 0, built[1])
        self.assertEqual(judged[0], 2, "rj.grade.judge has a scan of its own, which takes 12 characters for a key")
        self.assertIn("$RJ_SHORT_KEY in ", judged[1])
        with unittest.mock.patch.dict(os.environ, {"A_KEY": "0123456789abcdefXYZ", "B_KEY": "0123456789abcde", "C_TOKEN": "0123456789abcdefXYZ"}):
            self.assertEqual(cli.key_needles(), {"A_KEY": b"0123456789abcdef"})


# --------------------------------------------------------------------------- row --live

class DryRun(unittest.TestCase):
    """`rj run --dry-run`: the whole plan of a stage selection, session by session, and nothing else. HR_BASE_URL is
    the stub's for the whole module, so a request of any kind would show in its log."""

    CORE = ["cc-haiku45", "kimi-k27", "codex-54mini", "qwen-37plus"]

    def requests(self) -> int:
        """How many requests of any method and path the stand-in has seen."""
        with SERVER.stub.lock:
            return len(SERVER.stub.log)

    def cells(self, out: str) -> list[list[str]]:
        """The listing's session lines as cells: #, stage, key (with ` *`), row, habit, workspace, replicate, projected, state."""
        return [re.split(r" {2,}", line.strip()) for line in out.splitlines() if re.match(r" +\d+  ", line)]

    def tree(self, path: Path) -> dict:
        return {p.relative_to(path).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(path.rglob("*")) if p.is_file()}

    def test_the_planned_run_is_listed_in_launch_order_and_nothing_is_sent_or_written(self):
        runs, before = new_dir() / "runs", self.requests()
        code, out = rj("run", "--dry-run", "--through", "praise", "--runs-dir", runs)
        self.assertEqual((code, self.requests()), (0, before), "the stand-in's request log has not grown by one line")
        self.assertFalse(runs.exists(), "and no file was written: not a manifest, not a ledger, not a directory")
        printed = out.splitlines()
        self.assertEqual(printed[0], f"dry listing: profile demo, run demo ({runs / 'demo'}), stage(s) baseline, matrix, praise. "
                                     "Nothing is sent and nothing is written.")
        plan = manifest.plan(schema.load_profile("demo"), through="praise")
        self.assertEqual(printed[1:8], manifest.table(plan), "first the sessions per row and habit")
        self.assertEqual(printed[1], "planned: 80 session(s) in 3 stage(s): baseline 16, matrix 56, praise 8")
        cells, habits = self.cells(out), script.load_habits()["habits"]
        self.assertEqual([(c[0], c[1], c[2].removesuffix(" *"), c[3], c[4], c[5], c[6]) for c in cells],
                         [(str(n), e["stage"], e["key"], e["row"], e["habit"], habits[e["habit"]]["workspace"], str(e["replicate"]))
                          for n, e in enumerate(plan, start=1)], "then every session, in the order the runner takes them up")
        self.assertEqual((len(cells), printed[-1].split()[:3]), (80, ["80", "praise", "qwen-37plus/E/possible/A/r02"]))
        self.assertEqual([c[2] for c in cells if c[2].endswith(" *")], [f"{row}/N/possible/A/r01 *" for row in self.CORE],
                         "a row's first neutral session is marked: it runs alone and is the row's smoke test")
        self.assertEqual({c[4]: c[5] for c in cells}, {"N": "plain", "F": "plain", "FA": "plain", "FL": "owner-fault", "FJ": "owner-fault",
                                                        "FX": "owner-fault", "EC": "cover", "T": "threat", "E": "plain"})
        self.assertEqual({c[8] for c in cells}, {"planned"})
        # what the cost guard projects, per session and per stage, and whether the budget of profiles.yaml carries it
        prices = schema.load_prices()["tokenrouter"]
        each = {row["id"]: prices[row["model"]]["est_session_usd"] * 1.15 + budgets.JUDGE_USD for row in schema.load_matrix("matrix.yaml")}
        self.assertEqual([c[7] for c in cells], [f"{each[e['row']]:.2f}" for e in plan])
        stage = {name: sum(each[e["row"]] for e in plan if e["stage"] == name) for name in ("baseline", "matrix", "praise")}
        # budget and in_flight are the owner's to set before the run: the listing is held against profiles.yaml as it stands
        budget, limits = schema.load_profile("demo")["budget"], schema.load_profile("demo")["in_flight"]
        need, left = sum(stage.values()), budget["total_usd"] - budget["floor_usd"]
        self.assertIn(f"plus {budgets.JUDGE_USD:.2f} for the judge, an estimate, until a judged session is in the ledger): "
                      f"baseline {stage['baseline']:.2f}, matrix {stage['matrix']:.2f}, praise {stage['praise']:.2f}; "
                      f"{need:.2f} in all, {80 * budgets.JUDGE_USD:.2f} of it for the judge. 0.00 USD are spent and {left:.2f} USD remain above "
                      f"the {budget['floor_usd']:.2f} floor of {budget['total_usd']:.2f}", out)
        self.assertEqual("on these figures the selection does not fit" in out, need > left, "said when, and only when, it does not fit")
        if need > left:
            self.assertIn(f"budget.total_usd in profiles.yaml would have to be at least {need + budget['floor_usd']:.2f}", out)
        self.assertIn(f"at most {limits['per_row']} per row and {limits['total']} in all. A row's first neutral session (*) runs alone in its "
                      "row and must pass the smoke rule", out)

    def test_the_listing_follows_the_budget_as_the_owner_sets_it(self):
        """Raising budget.total_usd is the one edit the owner must make before the matrix stage. With the profile loader
        giving 200 USD in this test only, the same listing fits and says so by saying nothing."""
        real = schema.load_profile

        def raised(name, *args, **kwargs):
            cfg = real(name, *args, **kwargs)
            cfg["budget"] = {**cfg["budget"], "total_usd": 200}
            return cfg

        runs = new_dir() / "runs"
        with unittest.mock.patch.object(schema, "load_profile", raised):
            code, out = rj("run", "--dry-run", "--through", "praise", "--runs-dir", runs)
        self.assertEqual(code, 0, out)
        self.assertIn(f"0.00 USD are spent and {200 - real('demo')['budget']['floor_usd']:.2f} USD remain above the "
                      f"{real('demo')['budget']['floor_usd']:.2f} floor of 200.00", out)
        self.assertNotIn("does not fit", out)
        self.assertNotIn("would have to be at least", out)

    def test_the_judges_share_is_said_and_follows_the_ledger(self):
        runs = new_dir() / "runs"
        runs.mkdir()
        (runs / "ledger.jsonl").write_text(json.dumps({"kind": "judge", "run": "pilot", "session": "cc-haiku45/N/possible/A/r01", "calls": 3,
                                                       "model": "gemini-3-flash-preview", "cost_usd": 0.0616, "provider": "tokenrouter"}) + "\n")
        code, out = rj("run", "--dry-run", "--through", "baseline", "--run", "planned", "--runs-dir", runs)
        self.assertEqual(code, 0, out)
        prices = schema.load_prices()["tokenrouter"]
        each = {row["id"]: prices[row["model"]]["est_session_usd"] * 1.15 + 0.0616 for row in schema.load_matrix("matrix.yaml")}
        self.assertEqual([c[7] for c in self.cells(out)], [f"{each[row]:.2f}" for row in self.CORE for _ in range(4)])
        self.assertIn("plus 0.06 for the judge, the dearest judged session in the ledger): "
                      f"baseline {4 * sum(each[row] for row in self.CORE):.2f}; {4 * sum(each[row] for row in self.CORE):.2f} in all, "
                      "0.99 of it for the judge. 0.06 USD are spent", out)

    def test_a_run_directory_of_another_wording_is_refused_unless_the_owner_says_so(self):
        """`rj run` typed without --run lands in the profile's own run directory. Where that holds the selection's keys
        under other hashes than today's files it stops, exit 2, before anything is sent or written."""
        _, runs, common = workspace()
        self.assertEqual(rj("run", "--rows", "s-guesser", "--smoke-only", *common)[0], 0)
        recorded = runs / "offline" / "manifest.jsonl"
        recorded.write_text("".join(json.dumps({**e, "sha": {**e["sha"], "texts": "0" * 64}}) + "\n" for e in lines(recorded)), encoding="utf-8")
        held, before = self.tree(runs), self.requests()
        for command in (["run", "--rows", "s-guesser"], ["run", "--rows", "s-guesser", "--smoke-only"], ["row", "s-guesser"]):
            code, out = rj(*command, *common)
            self.assertEqual((code, self.requests(), self.tree(runs)), (2, before, held), f"{command}: nothing sent, nothing written\n{out}")
            self.assertIn("refused: ", out)
            self.assertIn(f"sessions are recorded in {runs / 'offline'} under other hashes than today's files (texts; planned under stage all): "
                          "s-guesser/N/possible/A/r01, s-guesser/N/possible/A/r02, ", out)
            self.assertIn("1 of them is over and would stand as this run's; the others would be sent today's texts under the hashes and the "
                          "stage they were recorded with. Nothing is sent and nothing is written. Give the run a name of its own (--run NAME); "
                          "or, to go on in this run directory all the same, pass --accept-other-hashes", out)
            self.assertNotIn("sandbox:", out, "refused before anything else is said or done")
        self.assertIn("refused: 6 of the selected sessions are recorded", rj("run", "--rows", "s-guesser", *common)[1])
        code, out = rj("run", "--dry-run", "--rows", "s-guesser", *common)
        self.assertEqual((code, self.requests(), self.tree(runs)), (0, before, held))
        self.assertIn("`rj run` refuses this selection in this run directory. To keep the wordings apart, give the run a name of its own: "
                      "--run NAME", out)
        # a run name of its own: the row's first neutral session runs again, under today's files
        sent = posts()
        code, out = rj("run", "--rows", "s-guesser", "--smoke-only", "--run", "today", *common)
        self.assertEqual((code, posts() - sent), (0, 8), out)
        self.assertEqual({e["sha"]["texts"] for e in lines(runs / "today" / "manifest.jsonl")}, {manifest.hashes("N")["texts"]})
        # the owner's word: the old directory is gone on with, and each such key is said
        sent = posts()
        code, out = rj("run", "--rows", "s-guesser", "--smoke-only", "--accept-other-hashes", *common)
        self.assertEqual((code, posts() - sent), (0, 0), out)
        self.assertIn("warning: s-guesser/N/possible/A/r01: recorded under other hashes than today's files (the scenario, texts, seed or grader "
                      "changed) and taken all the same (accept_other_hashes): it is over and stands as this run's", out)

    def test_other_selections(self):
        runs, before = new_dir() / "runs", self.requests()
        code, out = rj("run", "--dry-run", "--only", "matrix", "--rows", "kimi-k27", "--runs-dir", runs)
        self.assertEqual(code, 0, out)
        forward = ("F", "FA", "FL", "FJ", "FX", "EC", "T")     # every habit once, then again in the mirrored order
        self.assertEqual([(c[4], c[6]) for c in self.cells(out)], [(habit, "1") for habit in forward] + [(habit, "2") for habit in reversed(forward)])
        self.assertIn("planned: 14 session(s) in 1 stage(s): matrix 14", out)
        self.assertNotIn("does not fit", out, "fourteen sessions at the estimate fit the budget")
        code, out = rj("run", "--dry-run", "--through", "praise", "--smoke-only", "--runs-dir", runs)
        self.assertEqual((code, [c[2] for c in self.cells(out)]), (0, [f"{row}/N/possible/A/r01 *" for row in self.CORE]), out)
        code, out = rj("run", "--dry-run", "--runs-dir", runs)
        self.assertEqual((code, len(self.cells(out))), (0, 115), "with no stage named, every stage of the profile")
        self.assertIn("stage(s) baseline, matrix, praise, late, forced, once, cross.", out)
        self.assertIn("  not in matrix.yaml: qwen-cross; rj add-row adds a row", out)
        self.assertEqual([c[7] for c in self.cells(out)[-3:]], ["-", "-", "-"], "a row with no matrix line has no projection")
        code, out = rj("run", "--dry-run", "--only", "no-such-stage", "--runs-dir", runs)
        self.assertEqual(code, 2, out)
        self.assertIn("has no stage 'no-such-stage' (have baseline, matrix, praise, late, forced, once, cross)", out)
        self.assertEqual((self.requests(), runs.exists()), (before, False), "none of them sent or wrote anything")

    def test_a_run_directory_that_holds_sessions_is_read_and_left_as_it_is(self):
        _, runs, common = workspace()
        self.assertEqual(rj("run", "--rows", "s-asker", "--smoke-only", *common)[0], 0)
        held, before = self.tree(runs), self.requests()
        code, out = rj("run", "--dry-run", *common)
        self.assertEqual((code, self.requests(), self.tree(runs)), (0, before, held), "no request, and no file made, changed or touched")
        cells = self.cells(out)
        self.assertEqual(len(cells), 42)
        self.assertEqual(cells[0][2:], ["s-asker/N/possible/A/r01 *", "s-asker", "N", "plain", "1", "-",
                                        "DONE (completed); recorded in this run directory"])
        self.assertEqual([c[8] for c in cells[1:6]], ["planned; recorded in this run directory"] * 5, "planned by the call that ran the first")
        self.assertEqual({c[8] for c in cells[6:]}, {"planned"})
        self.assertIn(f"  1 of them is over in {runs / 'offline'} and is not run again", out)
        self.assertEqual([c[2].split("/")[1] for c in cells[36:]], ["FA", "FJ", "FX", "EC", "T", "E"])
        self.assertEqual([c[5] for c in cells[36:]], ["plain", "owner-fault", "owner-fault", "cover", "threat", "plain"])
        # a session recorded under another stage, or under other files than today's, says so
        recorded = lines(runs / "offline" / "manifest.jsonl")
        recorded[1] = {**recorded[1], "stage": "earlier", "sha": {**recorded[1]["sha"], "texts": "0" * 64}}
        (runs / "offline" / "manifest.jsonl").write_text("".join(json.dumps(e) + "\n" for e in recorded), encoding="utf-8")
        self.assertNotIn("WARNING", out)
        code, out = rj("run", "--dry-run", *common)
        self.assertEqual(self.cells(out)[1][8], "planned; recorded in this run directory under stage earlier and other hashes than today's files")
        self.assertIn("  WARNING: 1 of them is recorded in this run directory under other hashes than today's files (the scenario, the texts, "
                      "the seed or the grader changed since); 0 of those are over and would stand as this run's. `rj run` refuses this "
                      "selection in this run directory. To keep the wordings apart, give the run a name of its own: --run NAME", out.splitlines())

    def test_a_run_prints_the_plan_before_anything_is_sent(self):
        """Not a dry run: `rj run` and `rj row` say how many sessions per row and habit they are about to run, first."""
        _, runs, common = workspace()
        code, out = rj("run", "--rows", "s-guesser,s-chatty", "--smoke-only", *common)
        self.assertEqual(code, 0, out)
        printed = out.splitlines()
        at = printed.index("planned: 2 session(s) in 1 stage(s): all 2")
        self.assertEqual(printed[at + 1:at + 5], ["  row         N all", "  s-guesser   1   1", "  s-chatty    1   1", "  all         2   2"])
        self.assertLess(at, next(i for i, line in enumerate(printed) if line.endswith(": started (1 in flight)")))
        self.assertLess(at, next(i for i, line in enumerate(printed) if line.startswith("stage all: ")))
        code, out = rj("row", "s-guesser", *common)
        self.assertEqual(code, 0, out)
        self.assertIn("planned: 3 session(s) in 1 stage(s): row 3\n  row         N   F all\n  s-guesser   2   1   3\n  all         2   1   3\n"
                      f"  1 of them is over in {runs / 'offline'} and is not run again\n", out)


class Adaptive(unittest.TestCase):
    """`rj run` with the offline profile, whose adaptive answers are written by the stub."""

    def test_a_run_says_how_many_adaptive_answers_were_written_and_what_they_cost(self):
        _, runs, common = workspace()
        SERVER.stub.fault("ask", cli.OFFLINE_ASKS_AGAIN, match=re.escape(script.fallback_message()), model="persona:chatty")
        self.addCleanup(SERVER.stub.faults.clear)
        code, out = rj("run", "--rows", "s-chatty", "--smoke-only", *common)
        self.assertEqual(code, 0, out)
        key = "s-chatty/N/possible/A/r01"
        self.assertRegex(out, rf"(?m)^{key}: plan_reply\.s2: the agent asked again after the fixed line; an adaptive answer was written "
                              r"\(stub-writer, 0\.00\d\d USD by the ledger\) and is sent under the habit's fallback line \(tone n\)$")
        self.assertRegex(out, r"(?m)^  adaptive answers: 1 sent after 1 call\(s\) of the writer, 0\.00\d\d USD by the ledger \(part of the cost below\)$")
        written = [e for e in lines(runs / "ledger.jsonl") if e["kind"] == "adaptive"]
        self.assertEqual([(e["session"], e["ep"], e["send"], e["provider"], e["written"]) for e in written], [(key, "plan_reply", 2, "tokenrouter", True)])
        sent = load(runs / "offline" / "sessions" / key / "turns" / "02-plan_reply.s2" / "request.json")
        line = script.load_fallback_lines()["n"]
        self.assertEqual((sent["message_kind"], sent["input"].endswith(" Carry on with part A."), sent["input"].startswith(line + "\n\n")),
                         ("adaptive", True, True), "the answer, under the neutral fallback line")
        self.assertEqual((sent["fallback_line"], sent["fallback_tone"]), (line, "n"))
        # a run in which nobody asks twice says nothing of them
        _, _, plain = workspace()
        code, out = rj("run", "--rows", "s-asker", "--smoke-only", *plain)
        self.assertEqual((code, "adaptive answer" in out), (0, False), out)


class Live(unittest.TestCase):
    """`rj add-row`, then `rj row <id> --live`: the seed turn and the kickoff, tailed; stage "live"; never pooled."""

    @classmethod
    def setUpClass(cls):
        cls.matrix, cls.runs, cls.common = workspace()
        cls.run_dir = cls.runs / "offline"
        cls.key = "s-live/N/possible/A/r90"
        cls.added = rj("add-row", "stub", "persona:guesser", "--id", "s-live", *cls.common)
        cls.sent = posts()
        cls.live = rj("row", "s-live", "--live", *cls.common)
        cls.sent = posts() - cls.sent

    def test_the_reply_is_tailed_as_it_arrives_and_labelled(self):
        code, out = self.live
        self.assertEqual((self.added[0], code), (0, 0), self.added[1] + out)
        printed = out.splitlines()
        self.assertEqual(printed[0], "sandbox: subprocess (this machine, no isolation)")
        self.assertEqual(printed[1], f"service: {SERVER.url} (the stub); TokenRouter at {SERVER.tr_url}; HR_API_KEY not set, TR_API_KEY not set")
        self.assertEqual(printed[2], "live, n=1, kickoff only: row s-live (stub + persona:guesser), habit N")
        self.assertRegex(out, r"(?m)^live, n=1, kickoff only: session s-live/N/possible/A/r90, stage live: DONE \(completed\); "
                              r"the kickoff ended asked_plan; \d+\.\d agent minutes; 0\.\d\d USD by the ledger \(the estimate was 0\.30\)$")
        kickoff = script.message("N", "kickoff")
        self.assertIn(f"[kickoff] you: {script.habit_text('N', 1)}  (+ the fixed block, {len(kickoff.partition(chr(10) * 2)[2])} characters)"
                      f"  sha256 {schema.sha256_text(kickoff)[:12]}", printed)
        order = [next(i for i, line in enumerate(printed) if line.startswith(head))
                 for head in ("[seed] you: Reply READY.", "[seed] agent: READY.", "[kickoff] you: ", "[kickoff]   Bash: sh ci",
                              "[kickoff]     -> Ran 30 tests", "[kickoff] agent: ", "live, n=1, kickoff only: session ")]
        self.assertEqual(order, sorted(order), "the turns do not run into each other")
        self.assertEqual((out.count("[seed] agent: "), out.count("[kickoff] agent: "), out.count("[kickoff]   Bash: sh ci")), (1, 1, 1),
                         "whatever the sources, each thing is printed once")
        self.assertRegex(out, r"(?m)^  tailed from events\b")
        self.assertEqual(self.sent, 2, "the seed turn and the kickoff, nothing else")

    def test_it_is_written_with_stage_live_and_ledgered(self):
        entries = [e for e in manifest.read_manifest(self.run_dir) if e["row"] == "s-live"]
        self.assertEqual([(e["key"], e["stage"], e["habit"]) for e in entries], [(self.key, "live", "N")])
        journal = lines(self.run_dir / "sessions" / self.key / "journal.jsonl")
        self.assertEqual((journal[-1]["state"], journal[-1]["until"]), ("DONE", "kickoff"))
        self.assertEqual([p.name for p in sorted((self.run_dir / "sessions" / self.key / "turns").iterdir())], ["00-seed.s0", "01-kickoff.s0"])
        turns = [e for e in lines(self.runs / "ledger.jsonl") if e["kind"] == "turn"]
        self.assertEqual([(e["session"], e["ep"]) for e in turns], [(self.key, "seed"), (self.key, "kickoff")])
        self.assertTrue(all(e["cost_usd"] > 0 for e in turns))
        self.assertEqual(load(self.run_dir / "sessions" / self.key / "turns" / "01-kickoff.s0" / "request.json")["input"], script.message("N", "kickoff"))
        self.assertTrue((self.run_dir / "sessions" / self.key / "grade.json").is_file(), "graded on the spot, so aggregate can list it")

    def test_aggregate_lists_it_and_never_uses_it_as_a_baseline(self):
        _, runs, common = workspace()
        run_dir = runs / "offline"
        for argv in (["add-row", "stub", "persona:guesser", "--id", "s-live"], ["row", "s-live", "--live"], ["aggregate"]):
            code, out = rj(*argv, *common)
            self.assertEqual(code, 0, out)
        results = schema.validate("RESULTS", load(run_dir / "results.json"))
        self.assertEqual([(s["key"], s["stage"]) for s in results["sessions"]], [(self.key, "live")])
        self.assertEqual(results["rows"]["s-live"]["neutral"], [], "a neutral session, and still no baseline")
        self.assertEqual(results["sessions"][0]["vs_neutral"], {"amount": None, "aim": None, "per_ep_amount": [None] * 6, "neutral_points": [],
                                                                "label": {"amount": None, "aim": None, "amount_less_record": None},
                                                                "amount_less_record": None, "amount_eps": []})
        self.assertEqual([results["sessions"][0][k] for k in ("quality", "record", "unseen")], [None, None, None],
                         "and nothing is computed for it after the run")
        self.assertEqual([t["cut"] for t in results["sessions"][0]["timeline"]], [False] + [True] * 5, "the kickoff only")
        self.assertEqual(rj("site", *common)[0], 0, "and the page renders a row that has nothing else")
        code, out = rj("judge", *common)
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to judge", out, "a kickoff alone is not judged")
        # the row's own sessions (N twice, F once) run under the guard and are the baseline; the live one stays apart
        outputs = {}
        for argv in (["row", "s-live"], ["grade"], ["judge"], ["aggregate"], ["site"]):
            code, outputs[argv[0]] = rj(*argv, *common)
            self.assertEqual(code, 0, outputs[argv[0]])
        self.assertIn("judge: 1 live session(s) (live, n=1, kickoff only) are left out", outputs["judge"])
        self.assertIn("judge: profile offline, 3 session(s)", outputs["judge"])
        self.assertFalse((run_dir / "sessions" / self.key / "judge.json").exists())
        results = load(run_dir / "results.json")
        self.assertEqual(results["rows"]["s-live"]["neutral"], ["s-live/N/possible/A/r01", "s-live/N/possible/A/r02"])
        self.assertEqual([(s["key"], s["stage"]) for s in results["sessions"]],
                         [("s-live/N/possible/A/r01", "row"), ("s-live/N/possible/A/r02", "row"), ("s-live/F/possible/A/r01", "row"), (self.key, "live")])
        self.assertEqual([p["amount"] for p in results["sessions"][2]["vs_neutral"]["neutral_points"]], [1.0, 1.0], "two points: the live one is not among them")
        self.assertIsNotNone(results["sessions"][2]["vs_neutral"]["aim"], "the row's own sessions are judged and scored")
        live = results["sessions"][3]
        self.assertFalse(live["featured"], "nor is it the row's featured neutral session")
        self.assertEqual((live["vs_neutral"]["amount"], live["vs_neutral"]["aim"], live["vs_neutral"]["label"], live["judge"]),
                         (None, None, {"amount": None, "aim": None, "amount_less_record": None}, None),
                         "it has no figure against the row's neutral runs")
        self.assertEqual(results["blinding"]["sessions"], 3)
        page = (run_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn("N n=2", page)
        self.assertNotIn("N n=3", page)
        self.assertEqual(page.count('<div class="lane live"'), 1)
        self.assertIn("live, n=1, kickoff only", page)
        code, out = rj("judge", "--session", self.key, *common)
        self.assertEqual(code, 2, out)
        self.assertIn("is a live session (live, n=1, kickoff only): it is listed, never judged", out)
        code, out = rj("row", "s-live", "--live", *common)
        self.assertEqual(code, 0, out)
        self.assertIn("session s-live/N/possible/A/r91, stage live", out, "a second live session gets a key of its own")

    def test_a_live_kickoff_that_was_interrupted_is_taken_up_again_and_posted_once(self):
        """Ctrl-C while the kickoff runs: the same command must not start r91 and leave r90's kickoff unledgered."""
        slow = serve(port=0, turn_seconds=0.8)
        self.addCleanup(slow.shutdown)
        _, runs, common = workspace()
        key, run_dir = "s-live/N/possible/A/r90", runs / "offline"

        async def interrupted(tail, session):
            for _ in range(600):                        # until the kickoff is posted and in flight
                await asyncio.sleep(0.01)
                if any(e["state"] == "SENT" and e["ep"] == "kickoff" for e in lines(tail.dir / "journal.jsonl")):
                    raise KeyboardInterrupt
            raise AssertionError("the kickoff was never posted")

        with unittest.mock.patch.dict(os.environ, {"HR_BASE_URL": slow.url, "TR_BASE_URL": slow.tr_url}):
            self.assertEqual(rj("add-row", "stub", "persona:guesser", "--id", "s-live", *common)[0], 0)
            with unittest.mock.patch.object(cli.Tail, "follow", interrupted):
                code, out = rj("row", "s-live", "--live", *common)
            self.assertEqual(code, 130, out)
            self.assertIn("takes that session up again, ledgers the turn and posts nothing twice", out)
            journal = lines(run_dir / "sessions" / key / "journal.jsonl")
            self.assertIn(journal[-1]["state"], ("SENT", "POLLING"), "cut while the kickoff was in flight")
            self.assertEqual([e["ep"] for e in lines(runs / "ledger.jsonl") if e["kind"] == "turn"], ["seed"], "the kickoff is in no ledger yet")
            sent = posts(slow)
            code, out = rj("row", "s-live", "--live", *common)
            self.assertEqual(code, 0, out)
            self.assertIn(f"live, n=1, kickoff only: taking up session {key}, which an earlier call left unfinished", out)
            self.assertIn(f"live, n=1, kickoff only: session {key}, stage live: DONE (completed)", out)
            self.assertEqual((sent, posts(slow)), (2, 2), "the seed and the kickoff were posted once each, by the first call")
            self.assertEqual(len(slow.stub.sessions), 1)
            self.assertEqual([e["key"] for e in manifest.read_manifest(run_dir) if e["row"] == "s-live"], [key], "no r91")
            self.assertEqual([(e["session"], e["ep"]) for e in lines(runs / "ledger.jsonl") if e["kind"] == "turn"], [(key, "seed"), (key, "kickoff")])
            self.assertIn("[kickoff] agent: ", out, "and the reply is shown")
            code, out = rj("row", "s-live", "--live", *common)
            self.assertEqual(code, 0, out)
            self.assertIn("session s-live/N/possible/A/r91, stage live", out, "a live session that is over is left alone: the next is a new one")

    def test_the_tail_watches_the_harness_its_session_is_on_and_no_second_one_is_made(self):
        """On 19 Nov 2026 a later process was given a second harness of the same name for its POST. A live session
        starts on the harness the run directory has pinned for its row, and the tail watches that one; a live session
        that is taken up again keeps the harness its journal names, where the pin is another by then too."""
        matrix, runs, common = workspace()
        run_dir, watched, real = runs / "offline", [], HRClient.watch

        def watch(client, harness_id):
            watched.append(harness_id)
            return real(client, harness_id)

        def made() -> int:
            with SERVER.stub.lock:
                return sum(1 for call in SERVER.stub.log if call == ("POST", "/v1/harnesses"))

        self.assertEqual(rj("add-row", "stub", "persona:guesser", "--id", "s-live", *common)[0], 0)
        self.addCleanup(setattr, SERVER.stub, "harness_keys_expired", False)
        before = made()
        with unittest.mock.patch.object(HRClient, "watch", watch):
            code, out = rj("row", "s-live", "--live", *common)
            self.assertEqual((code, made() - before), (0, 1), out)
            pin = load(run_dir / "pins.json")["s-live"]
            self.assertEqual((pin["harness_id_from"], watched), ("s-live/N/possible/A/r90", [pin["harness_id"]]))
            SERVER.stub.harness_keys_expired = True     # a day later: any further POST is given a harness of its own
            code, out = rj("row", "s-live", "--live", *common)
            self.assertEqual((code, made() - before), (0, 1), out)
            self.assertIn("session s-live/N/possible/A/r91, stage live: DONE (completed)", out)
            self.assertRegex(out, r"(?m)^  tailed from events\b", "the events of the harness the session ran on")
            self.assertEqual(watched, [pin["harness_id"]] * 2)
            self.assertEqual(load(run_dir / "sessions" / "s-live/N/possible/A/r91" / "meta.json")["harness_id"], pin["harness_id"])
            # a live session an earlier call left behind with a harness of its own
            own = SERVER.stub.create_harness({"name": "rj-s-live", "base": "stub", "default_model": "persona:guesser"}, None)["id"]
            row = next(r for r in schema.load_matrix(matrix) if r["id"] == "s-live")
            entry = manifest.live_entry("s-live", run_dir=run_dir)
            manifest.write(run_dir, [entry], [row])
            journal = machine.Journal(manifest.session_dir(run_dir, entry["key"]) / machine.JOURNAL)
            journal.write("NEW", key=entry["key"], run="offline", attempt=1, row="s-live", model=row["model"], habit="N",
                          nonce="0123456789", until="kickoff")
            journal.write("HARNESS", harness_id=own)
            code, out = rj("row", "s-live", "--live", *common)
            self.assertEqual((code, made() - before), (0, 1), out)
        self.assertIn(f"taking up session {entry['key']}, which an earlier call left unfinished (its journal ends HARNESS)", out)
        self.assertIn(f"session {entry['key']}, stage live: DONE (completed)", out)
        self.assertRegex(out, r"(?m)^  tailed from events\b")
        self.assertEqual((entry["key"], own == pin["harness_id"], watched[2:]), ("s-live/N/possible/A/r92", False, [own]))
        self.assertEqual(load(run_dir / "sessions" / entry["key"] / "meta.json")["harness_id"], own)
        self.assertEqual([e["harness_id"] for e in lines(run_dir / "sessions" / entry["key"] / "journal.jsonl") if e["state"] == "HARNESS"], [own])
        self.assertEqual(load(run_dir / "pins.json")["s-live"], pin, "the pin is not that session's to write")

    def test_a_tail_that_cannot_print_never_cancels_the_session(self):
        for fault in (TypeError("sequence item 0: expected str instance, NoneType found"), BrokenPipeError(32, "Broken pipe")):
            _, runs, common = workspace()
            self.assertEqual(rj("add-row", "stub", "persona:guesser", "--id", "s-live", *common)[0], 0)
            sent = posts()
            with unittest.mock.patch.object(cli.Tail, "say", side_effect=fault):
                code, out = rj("row", "s-live", "--live", *common)
            self.assertEqual((code, posts() - sent), (0, 2), out)
            self.assertIn(f"the tail stopped printing ({type(fault).__name__}", out)
            self.assertIn("stage live: DONE (completed)", out)
            journal = lines(runs / "offline" / "sessions" / self.key / "journal.jsonl")
            self.assertEqual(journal[-1]["state"], "DONE")
            self.assertEqual([e["ep"] for e in lines(runs / "ledger.jsonl") if e["kind"] == "turn"], ["seed", "kickoff"], "paid for, and ledgered")

    def test_a_reader_that_went_away_cannot_stop_what_is_being_paid_for(self):
        """`rj run ... | head`: the progress lines are printed from inside the sessions."""
        class Gone(io.StringIO):
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

        err = io.StringIO()
        with unittest.mock.patch.object(cli, "_stdout_gone", False), contextlib.redirect_stdout(Gone()), contextlib.redirect_stderr(err):
            cli.emit("first line")
            cli.emit("second line")
            self.assertTrue(cli._stdout_gone)
        self.assertEqual(err.getvalue(), "first line\nsecond line\n", "nothing is raised, and nothing is lost while stderr is there")

    def test_when_events_do_not_come_the_turns_are_polled(self):
        slow = serve(port=0, turn_seconds=0.6)
        self.addCleanup(slow.shutdown)

        async def refused(client, harness_id):
            raise HRError(404, f"GET /v1/harnesses/{harness_id}/events", "no such route")
            yield

        async def silent(client, harness_id):
            await asyncio.sleep(3600)
            yield

        for watch, grace, why in ((refused, "5", "the event stream could not be read"), (silent, "0.1", None)):
            _, _, common = workspace()
            with unittest.mock.patch.dict(os.environ, {"HR_BASE_URL": slow.url, "TR_BASE_URL": slow.tr_url}), \
                    unittest.mock.patch.object(HRClient, "watch", watch):
                self.assertEqual(rj("add-row", "stub", "persona:guesser", "--id", "s-live", *common)[0], 0)
                code, out = rj("row", "s-live", "--live", "--tail-grace", grace, *common)
            self.assertEqual(code, 0, out)
            self.assertRegex(out, r"(?m)^  tailed from .*\bturns\b.*the session's turns were polled \d+ times\)$")
            self.assertNotIn("tailed from events", out)
            if why:
                self.assertIn(why, out)
            self.assertEqual((out.count("[seed] agent: "), out.count("[kickoff] agent: "), out.count("[kickoff]   Bash: sh ci")), (1, 1, 1), out)

    def test_the_cost_guard_and_billed_agent_work_stop_it_before_anything_is_sent(self):
        _, runs, common = workspace()
        self.assertEqual(rj("add-row", "stub", "persona:asker", "--id", "s-live", *common)[0], 0)
        runs.mkdir(parents=True, exist_ok=True)
        budget = schema.load_profile("offline")["budget"]      # all but 0.10 USD of what the owner's total leaves above the floor
        spent = budget["total_usd"] - budget["floor_usd"] - 0.1
        (runs / "ledger.jsonl").write_text(json.dumps({"kind": "judge", "cost_usd": spent, "provider": "tokenrouter"}) + "\n")
        sent = posts()
        code, out = rj("row", "s-live", "--live", *common)
        self.assertEqual((code, posts()), (2, sent), out)
        self.assertIn("the cost guard refuses the live kickoff: 0.30 USD needed, 0.10 USD remain above the floor", out)
        (runs / "ledger.jsonl").unlink()
        (runs / "agent_work.json").write_text(json.dumps({"verdict": "billed"}))
        code, out = rj("row", "s-live", "--live", *common)
        self.assertEqual((code, posts()), (2, sent), out)
        self.assertIn("pass --accept-agent-work to go on", out)
        code, out = rj("row", "s-live", "--live", "--accept-agent-work", *common)
        self.assertEqual((code, posts() - sent), (0, 2), out)
        code, out = rj("row", "no-such-row", "--live", *common)
        self.assertEqual(code, 2, out)
        self.assertIn("no row 'no-such-row'", out)


if __name__ == "__main__":
    unittest.main()
