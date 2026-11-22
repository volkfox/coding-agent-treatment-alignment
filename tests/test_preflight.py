"""W9 (`make preflight-test`): `rj preflight` against the local stub, and against the replies HarnessRouter
Cloud gave on 17 Nov 2026 (tests/fixtures/cloud/) served by an httpx.MockTransport. Plan section 2.

No test here reaches another machine: the stub listens on 127.0.0.1, the mock transport answers in the
process, and RJ_ENV_FILE names a file that does not exist, so the repository's .env is never read.
Every test works in its own runs directory under one temporary directory; the module checks that the
repository's top level and its runs/ are as they were.
"""
import asyncio
import base64
import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

import httpx
import yaml

from rj import cli, preflight, pricing, schema
from rj.runner import budget as budgets
from rj.runner import machine
from rj.stub.server import serve
from tests import judges

ROOT = schema.ROOT
CLOUD = ROOT / "tests" / "fixtures" / "cloud"
STEPS = ("ready", "remember", "recall", "files")
FAKE_KEY = "sk-rj-test-0123456789abcdefghijklmnopqrstuvwxyz"       # nobody's key
FOREIGN = "KIMI_MODEL_API_KEY=" + "k" * 40                          # what a sandbox's own environment holds

SERVER = None
TMP: Path = None
_env = None
_before = None


def snapshot() -> dict:
    runs = ROOT / "runs"
    return {"top": sorted(p.name for p in ROOT.iterdir() if p.name != "__pycache__"),
            "runs": {p.relative_to(ROOT).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(runs.rglob("*")) if p.is_file()}}


def setUpModule():
    global SERVER, TMP, _env, _before
    _before = snapshot()
    TMP = Path(tempfile.mkdtemp(prefix="rj-preflight-test-"))
    SERVER = serve(port=0, turn_seconds=0.02, keepalive=0.2)
    _env = unittest.mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
                                                 "HR_BASE_URL": SERVER.url, "TR_BASE_URL": SERVER.tr_url,
                                                 cli.ENV_NAME: str(TMP / "no-such.env")})
    _env.start()
    for name in [n for n in os.environ if n.endswith("_KEY")]:
        os.environ.pop(name)


def tearDownModule():
    SERVER.shutdown()
    _env.stop()
    shutil.rmtree(TMP, ignore_errors=True)
    assert snapshot() == _before, "the tests wrote into the repository"


def new_dir(prefix: str = "runs-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir=TMP))


def rj(*argv: str) -> tuple[int, str]:
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
    """How many turns the stub has been sent so far."""
    stub = (server or SERVER).stub
    with stub.lock:
        return sum(1 for method, path in stub.log if (method, path) == ("POST", "/v1/responses"))


# --------------------------------------------------------------------------- against the stub

class Stub(unittest.TestCase):
    """`rj preflight --free` and `--paid` with the offline profile: the stub answers both services."""

    def setUp(self):
        self.runs = new_dir()
        self.common = ["--profile", "offline", "--runs-dir", self.runs]
        self.posts_before = posts()

    def tearDown(self):
        stub = SERVER.stub
        with stub.lock:
            stub.faults.clear()
            stub.tr_usage_cents, stub.tr_cents_per_turn, stub.usd_per_turn, stub.balance_usd = 0.0, 0.0, 0.0, 15.0

    def test_free_writes_a_valid_file_with_checks_1_to_3(self):
        code, out = rj("preflight", "--free", "--wallet-usd", "50", *self.common)
        self.assertEqual(code, 0, out)
        doc = schema.validate("PREFLIGHT", load(self.runs / "preflight.json"))
        self.assertEqual([doc["checks"][n]["status"] for n in ("1", "2", "3")], ["pass", "pass", "pass"], out)
        self.assertEqual([doc["checks"][n]["status"] for n in ("4", "10", "11")], ["skipped"] * 3)
        account = doc["account"]
        self.assertEqual((account["b0_usd"], account["w0_usd"], account["judge_path"]), (15.0, 50.0, "/chat/completions"))
        self.assertEqual(account["wallet_expiry"], "2030-01-01T00:00:00Z")
        self.assertEqual((account["agent_work"], account["two_task_limit"], account["task_is_session"]), ("cannot_tell", None, None))
        self.assertEqual(sorted(doc["rows"]), sorted(r["id"] for r in schema.load_matrix("matrix.stub.yaml")))
        for row in doc["rows"].values():
            self.assertEqual((row["payer"], row["provider"], row["pin_served_model"]), ("default", "tokenrouter", None))
        self.assertIn("largest difference 0.0%", doc["checks"]["3"]["evidence"])
        self.assertEqual(posts(), self.posts_before, "the free checks send no turn")
        self.assertEqual(lines(self.runs / "ledger.jsonl"), [], "and ledger nothing")
        raw = [p.name for p in (self.runs / "preflight-raw").glob("*-free/*.json")]
        self.assertEqual(sorted(raw), ["hr_balance.json", "hr_bases.json", "hr_integrations.json", "hr_models.json",
                                       "tr_billing_subscription.json", "tr_billing_usage.json", "tr_models.json", "tr_pricing.json"])

    def test_paid_writes_the_pins_and_flags_the_runner_and_the_grader_read(self):
        with SERVER.stub.lock:
            SERVER.stub.tr_cents_per_turn = 1.5         # TokenRouter's figure moves as the turns are billed
        code, out = rj("preflight", "--paid", "--rows", "s-asker,s-guesser", *self.common)
        self.assertEqual(code, 0, out)
        doc = schema.validate("PREFLIGHT", load(self.runs / "preflight.json"))
        for rid, persona in (("s-asker", "persona:asker"), ("s-guesser", "persona:guesser")):
            row = doc["rows"][rid]
            self.assertEqual((row["pin_served_model"], row["pin_connection"], row["connection_returned"], row["tool_output_visible"]),
                             (persona, None, False, True), rid)
            self.assertEqual({n: c["status"] for n, c in row["checks"].items()}, {"5": "pass", "6": "pass", "7": "pass", "8": "pass"}, out)
            self.assertEqual(row["observed"]["tr_usage"]["rise_cents"], 6.0, "four turns at 1.5 cents")
            self.assertEqual(row["observed"]["steps"], ["ready", "resume", "files"])
            self.assertTrue(row["observed"]["recall"]["recalled"])
            self.assertEqual(row["observed"]["files"]["key_hits"], [])
        self.assertEqual([doc["checks"][n]["status"] for n in ("5", "6", "7", "8", "9")], ["pass", "pass", "cannot_tell", "pass", "cannot_tell"])
        self.assertIn("the recalls after 15 and 31 minutes have not run on any row", doc["checks"]["7"]["evidence"],
                      "each row kept the word at once; the account's question is open until the late recall has run")
        self.assertIsNone(doc["account"]["task_is_session"])
        self.assertEqual(doc["service"], budgets.service_of(SERVER.url))
        self.assertIn(("PUT", f"/v1/sessions/{row['observed']['files']['session_id']}/files/rj-preflight/day1/nonce.txt"), list(SERVER.stub.log))
        self.assertRegex(doc["account"]["python"], r"^3\.\d+")
        self.assertEqual(doc["account"]["agent_work"], "cannot_tell", "a flat balance over a few tiny turns settles nothing")
        self.assertEqual(posts() - self.posts_before, 8, "four turns a row, each sent once")

        # what the runner reads: the pin per row, and no two-task limit on record
        pins = machine.Pins(self.runs / "offline", self.runs)
        self.assertEqual(pins.get("s-asker"), {"served_model": "persona:asker", "connection": None})
        self.assertEqual(pins.rows["s-asker"]["served_model_from"], "preflight")
        self.assertIsNone(pins.get("s-runner")["served_model"])

        # the turns are ledgered as preflight calls on TokenRouter, between readings of its usage figure
        ledger = lines(self.runs / "ledger.jsonl")
        paid = [e for e in ledger if e["kind"] == "preflight"]
        self.assertEqual([(e["row"], e["step"]) for e in paid],
                         [(r, s) for r in ("s-asker", "s-guesser") for s in ("ready", "remember", "recall", "files")])
        self.assertTrue(all(e["provider"] == "tokenrouter" and e["cost_usd"] > 0 and e["run"] == "preflight" for e in paid))
        self.assertEqual([e["where"] for e in ledger if e["kind"] == "tr_usage"],
                         ["run_start", "preflight_before", "preflight_after", "preflight_before", "preflight_after"])
        entry = cli.price_book(schema.load_matrix("matrix.stub.yaml"))["tokenrouter"]["persona:asker"]
        self.assertAlmostEqual(paid[0]["cost_usd"], pricing.turn_cost_usd(paid[0]["usage"], pricing.price_from_yaml(entry)))
        self.assertAlmostEqual(doc["rows"]["s-asker"]["observed"]["ledger_usd"], sum(e["cost_usd"] for e in paid[:4]), places=5)

    def test_hand_readings_change_a_verdict_and_send_nothing(self):
        code, out = rj("preflight", "--paid", "--rows", "s-chatty", "--steps", "ready", *self.common)
        self.assertEqual(code, 0, out)
        doc = load(self.runs / "preflight.json")
        row = doc["rows"]["s-chatty"]
        self.assertEqual({n: c["status"] for n, c in row["checks"].items()},
                         {"5": "cannot_tell", "6": "pass", "7": "skipped", "8": "skipped"}, "the usage figure stood still")
        self.assertIn("--confirm-usage-log s-chatty", row["checks"]["5"]["evidence"])
        self.assertIsNone(row["tool_output_visible"], "not seen: the file and tool turn was not sent")
        sent = posts()
        code, out = rj("preflight", "--confirm-usage-log", "s-chatty", "--wallet-usd", "41.5", *self.common)
        self.assertEqual((code, posts()), (0, sent), out)
        self.assertIn("s-chatty: check 5 pass, check 6 pass, check 7 skipped, check 8 skipped; pin 'persona:chatty'", out,
                      "a hand reading shows which verdicts it changed and what the pin is")
        self.assertIn("wallet: 41.5 USD noted", out)
        doc = schema.validate("PREFLIGHT", load(self.runs / "preflight.json"))
        self.assertEqual(doc["rows"]["s-chatty"]["checks"]["5"]["status"], "pass")
        self.assertEqual((doc["checks"]["5"]["status"], doc["account"]["w0_usd"]), ("pass", 41.5))
        # a row that is checked again starts without the hand reading of its last check
        code, out = rj("preflight", "--paid", "--rows", "s-chatty", "--steps", "ready", *self.common)
        self.assertEqual((code, posts() - sent), (0, 1), out)
        self.assertEqual(load(self.runs / "preflight.json")["rows"]["s-chatty"]["checks"]["5"]["status"], "cannot_tell")
        code, out = rj("preflight", *self.common)
        self.assertEqual(code, 2, "with nothing to do it says so")
        self.assertIn("nothing to do", out)

    def test_a_served_model_that_differs_fails_until_the_owner_accepts_it(self):
        SERVER.stub.fault("served_model", "persona-asker-20261117", model="persona:coaster", times=4)
        code, out = rj("preflight", "--paid", "--rows", "s-coaster", *self.common)
        self.assertEqual(code, 1, out)
        row = load(self.runs / "preflight.json")["rows"]["s-coaster"]
        self.assertEqual((row["checks"]["6"]["status"], row["pin_served_model"]), ("fail", None))
        self.assertIn("--accept-served s-coaster", row["checks"]["6"]["evidence"])
        self.assertNotIn("rj replace", out, "a served string that differs is the owner's to accept, not a step-up")
        sent = posts()
        code, out = rj("preflight", "--accept-served", "s-coaster", *self.common)
        self.assertEqual((code, posts()), (0, sent), out)
        self.assertIn("s-coaster: check 5 cannot_tell, check 6 pass, check 7 pass, check 8 pass; pin 'persona-asker-20261117'", out)
        row = load(self.runs / "preflight.json")["rows"]["s-coaster"]
        self.assertEqual((row["checks"]["6"]["status"], row["pin_served_model"]), ("pass", "persona-asker-20261117"))
        self.assertEqual(machine.Pins(self.runs / "x", self.runs).get("s-coaster")["served_model"], "persona-asker-20261117")

    def test_a_balance_that_falls_is_agent_work_billed(self):
        with SERVER.stub.lock:
            SERVER.stub.usd_per_turn, SERVER.stub.tr_cents_per_turn = 0.02, 1.0
        code, out = rj("preflight", "--paid", "--rows", "s-flailer", *self.common)
        self.assertEqual(code, 1, out)
        doc = schema.validate("PREFLIGHT", load(self.runs / "preflight.json"))
        self.assertEqual((doc["account"]["agent_work"], doc["checks"]["9"]["status"]), ("billed", "fail"))
        self.assertAlmostEqual(doc["rows"]["s-flailer"]["observed"]["balance"]["fall_usd"], 0.08)
        # a whole session's verdict, which `rj run` keeps in agent_work.json, outweighs the tiny turns
        (self.runs / "agent_work.json").write_text(json.dumps({"verdict": "free", "session": "s-flailer/N/possible/A/r01", "run": "offline",
                                                               "balance_fall_usd": 0.0}))
        code, out = rj("preflight", "--wallet-usd", "50", *self.common)
        doc = load(self.runs / "preflight.json")
        self.assertEqual((code, doc["account"]["agent_work"], doc["checks"]["9"]["status"]), (0, "free", "pass"))

    def test_a_turn_that_does_not_complete_ends_the_rows_steps(self):
        """Nothing is put or posted behind a turn that failed: the session's latest turn is not the one this check holds."""
        SERVER.stub.fault("fail", "The turn failed on your tokenrouter connection: API Error: 400 Bad request", match="Remember the word")
        code, out = rj("preflight", "--paid", "--rows", "s-asker", *self.common)
        self.assertEqual((code, posts() - self.posts_before), (1, 2), out)
        row = load(self.runs / "preflight.json")["rows"]["s-asker"]
        self.assertEqual({n: c["status"] for n, c in row["checks"].items()}, {"5": "cannot_tell", "6": "pass", "7": "fail", "8": "skipped"},
                         "check 8 is not failed for what was check 7's")
        self.assertIn("API Error: 400 Bad request", row["checks"]["7"]["evidence"])
        self.assertEqual((row["observed"]["halted"], row["observed"]["steps"], row["observed"]["failure"]["kind"]), ("remember", ["ready", "resume"], "model"))
        sid = row["observed"]["ready"]["session_id"]
        self.assertFalse([entry for entry in SERVER.stub.log if entry[0] == "PUT" and sid in entry[1]], "no file was put on that session")
        self.assertIn("stopping this row: the remember turn did not come back completed", out)
        self.assertNotIn("not_latest_response", out)

    def late(self, forget_before: str | None = None, rows: str = "s-asker") -> tuple[dict, str, list]:
        """`--steps ready,resume,files,late` with every wait made instant; the stub's agent forgets the word
        before one of the late recalls when asked to."""
        naps, said = [], []

        async def nap(seconds: float) -> None:
            naps.append(seconds)
            due = {"recall_15": 1, "recall_31": 2}.get(forget_before)
            if due is not None and sum(1 for n in naps if n > 600) == due:
                with SERVER.stub.lock:
                    for session in SERVER.stub.sessions.values():
                        session.agent.word = "forgotten"

        cfg, matrix = schema.load_profile("offline"), schema.load_matrix("matrix.stub.yaml")
        got = asyncio.run(preflight.run(cfg, matrix, paid_checks=True, rows=[rows], steps=("ready", "resume", "files", "late"),
                                        runs_dir=self.runs, prices=cli.price_book(matrix), sleep=nap, log=said.append))
        return got, "\n".join(said), naps

    def test_the_late_recalls_say_whether_a_task_is_a_turn_or_a_session(self):
        got, out, naps = self.late()
        self.assertEqual(got["exit_code"], 0, out)
        doc = schema.validate("PREFLIGHT", load(self.runs / "preflight.json"))
        row = doc["rows"]["s-asker"]
        self.assertEqual([round(n / 60) for n in naps if n > 600], [15, 31],
                         "each wait runs up to 15 and to 31 minutes after the first turn (no time passes in this test)")
        self.assertEqual([(row["observed"][n]["recalled"], row["observed"][n]["after_seconds"]) for n in ("recall_15", "recall_31")],
                         [(True, 900.0), (True, 1860.0)])
        self.assertEqual((row["checks"]["7"]["status"], doc["checks"]["7"]["status"], doc["account"]["task_is_session"]), ("pass", "pass", False))
        self.assertIn("again 15 minutes and 31 minutes after the row's first turn", row["checks"]["7"]["evidence"])
        self.assertEqual(posts() - self.posts_before, 6, "ready, remember, recall, files and the two late recalls, each once")
        # a row checked later without the late step leaves the account's answer standing
        code, out = rj("preflight", "--paid", "--rows", "s-guesser", *self.common)
        self.assertEqual((code, load(self.runs / "preflight.json")["checks"]["7"]["status"]), (0, "pass"), out)

    def test_a_conversation_lost_only_at_31_minutes_is_the_accounts_limit_and_steps_nothing_up(self):
        got, out, _ = self.late(forget_before="recall_31")
        doc = load(self.runs / "preflight.json")
        row = doc["rows"]["s-asker"]
        self.assertEqual((got["exit_code"], got["proposals"]), (1, []), out)
        self.assertEqual((row["checks"]["7"]["status"], doc["checks"]["7"]["status"], doc["account"]["task_is_session"]), ("fail", "fail", True))
        self.assertIn("lost only 31 minutes after the row's first turn", row["checks"]["7"]["evidence"])
        self.assertIn("the account's 30-minute task limit ends a session (check 7): stop and ask HarnessRouter; the row is not stepped up", out)
        self.assertNotIn("model reason", out)

    def test_a_conversation_lost_at_15_minutes_is_not_the_accounts_limit(self):
        got, out, _ = self.late(forget_before="recall_15")
        doc = load(self.runs / "preflight.json")
        row = doc["rows"]["s-asker"]
        self.assertEqual((got["exit_code"], row["checks"]["7"]["status"], doc["account"]["task_is_session"]), (1, "fail", None), out)
        self.assertIn("before the 30-minute mark, so not the account's task limit", row["checks"]["7"]["evidence"])
        self.assertIn("s-asker: may be a model reason, but the row lists no step-up", out)
        self.assertNotIn("recall_31", row["observed"], "nothing more was asked of a session that lost its thread")

    def test_the_cap_step_shows_that_the_caps_cut_and_what_the_cut_is_called(self):
        sessions = len(SERVER.stub.sessions)
        code, out = rj("preflight", "--paid", "--rows", "s-asker", "--steps", "ready,resume,files,cap", *self.common)
        self.assertEqual(code, 0, out)
        doc = schema.validate("PREFLIGHT", load(self.runs / "preflight.json"))
        obs = doc["rows"]["s-asker"]["observed"]
        self.assertEqual((obs["cap_steps"]["status"], obs["cap_steps"]["reason"], obs["cap_steps"]["tool_calls_n"]), ("incomplete", "max_steps", 2))
        self.assertEqual((obs["cap_time"]["status"], obs["cap_time"]["reason"]), ("incomplete", "timeout"))
        self.assertEqual(doc["account"]["caps"], {"max_step": {"honoured": True, "reason": "max_steps"},
                                                  "timeout_seconds": {"honoured": True, "reason": "timeout"}})
        self.assertEqual((doc["checks"]["caps"]["status"], doc["rows"]["s-asker"]["checks"]["caps"]["status"]), ("pass", "pass"))
        self.assertIsNone(obs["failure"], "a turn cut by its cap, as meant, is no failure of the row")
        self.assertEqual({n: c["status"] for n, c in doc["rows"]["s-asker"]["checks"].items() if n != "caps"},
                         {"5": "cannot_tell", "6": "pass", "7": "pass", "8": "pass"})
        self.assertEqual((posts() - self.posts_before, len(SERVER.stub.sessions) - sessions), (6, 2),
                         "the timeout turn has a session of its own: nothing is posted behind the turn that was cut")
        self.assertNotEqual(obs["cap_time"]["session_id"], obs["cap_steps"]["session_id"])
        self.assertIn("check caps, over every row on record: pass", out)
        code, out = rj("preflight", "--paid", "--steps", "ready,nap", *self.common)
        self.assertEqual(code, 2, out)
        self.assertIn("--steps takes ready, resume, files, late, cap", out)

    def test_what_a_capped_turn_shows(self):
        read = preflight._cap_reading
        odd = read({"status": "incomplete", "reason": "max_cost", "cap": {"max_step": 2}})
        self.assertEqual((odd["status"], odd["honoured"], odd["reason"]), ("pass", True, "max_cost"))
        self.assertIn("a reason the runner has not met and files as a cap (max_steps)", odd["evidence"])
        ran_on = read({"status": "completed", "seconds": 190.0, "tool_calls_n": 3, "cap": {"timeout_seconds": 45}})
        self.assertEqual((ran_on["status"], ran_on["honoured"]), ("fail", False))
        self.assertIn("NOT honoured", ran_on["evidence"])
        under = read({"status": "completed", "seconds": 9.0, "tool_calls_n": 5, "cap": {"max_step": 2}})
        self.assertEqual((under["status"], under["honoured"]), ("cannot_tell", None))
        limit = read({"status": "failed", "error": "This task ran past the 30-minute limit", "cap": {"timeout_seconds": 45}})
        self.assertIn("the runner reads this error as the account's limit", limit["evidence"])

    def test_the_checks_of_another_service_are_not_merged_into(self):
        """`rj preflight --profile offline` in the real run's directory, or the other way round."""
        other = {"checks": {"1": {"status": "pass", "evidence": "the real account"}}, "service": "https://api.harnessrouter.example",
                 "account": {"b0_usd": 15.0, "agent_work": "billed"}, "rows": {}}
        (self.runs / "preflight.json").write_text(json.dumps(other))
        for argv in (["--free"], ["--paid", "--rows", "s-asker"], ["--wallet-usd", "5"]):
            code, out = rj("preflight", *argv, *self.common)
            self.assertEqual((code, posts()), (2, self.posts_before), out)
            self.assertIn("holds the checks of another service (https://api.harnessrouter.example)", out)
        self.assertEqual(load(self.runs / "preflight.json"), other, "left as it was")
        # nor does what another service's session showed about agent work stand in for this account's check 9
        (self.runs / "preflight.json").unlink()
        (self.runs / "agent_work.json").write_text(json.dumps({"verdict": "free", "session": "s-asker/N/possible/A/r01", "run": "offline",
                                                               "balance_fall_usd": 0.0, "service": "http://127.0.0.1:9"}))
        code, out = rj("preflight", "--paid", "--rows", "s-asker", "--steps", "ready", *self.common)
        doc = load(self.runs / "preflight.json")
        self.assertEqual((code, doc["account"]["agent_work"], doc["checks"]["9"]["status"]), (0, "cannot_tell", "cannot_tell"), out)

    def test_the_file_is_merged_not_started_over(self):
        (self.runs / "preflight.json").write_text(json.dumps({
            "checks": {"10": {"status": "pass", "evidence": "by hand"}}, "account": {"two_task_limit": False, "b0_usd": 14.0},
            "rows": {"old-row": {"pin_served_model": "old", "pin_connection": None, "connection_returned": True,
                                 "tool_output_visible": False, "question_ending": "prose"}}}))
        code, out = rj("preflight", "--free", "--rows", "s-asker", *self.common)
        self.assertEqual(code, 0, out)
        doc = schema.validate("PREFLIGHT", load(self.runs / "preflight.json"))
        self.assertEqual((doc["checks"]["10"]["status"], doc["account"]["two_task_limit"], doc["account"]["b0_usd"]), ("pass", False, 14.0))
        self.assertEqual(doc["account"]["balance_usd"], 15.0, "the balance read now, beside the first one noted")
        self.assertEqual(sorted(doc["rows"]), ["old-row", "s-asker"])
        self.assertEqual((doc["rows"]["old-row"]["pin_served_model"], doc["rows"]["old-row"]["connection_returned"]), ("old", True))

    def test_an_unknown_row_is_refused_before_anything_is_sent(self):
        code, out = rj("preflight", "--paid", "--rows", "no-such-row", *self.common)
        self.assertEqual((code, posts()), (2, self.posts_before), out)
        self.assertIn("no row no-such-row", out)
        self.assertFalse((self.runs / "preflight.json").exists())

    def test_a_stub_profile_never_asks_another_machine(self):
        with unittest.mock.patch.dict(os.environ, {"HR_BASE_URL": "https://api.harnessrouter.invalid"}):
            code, out = rj("preflight", "--free", *self.common)
        self.assertEqual(code, 2, out)
        self.assertIn("is not this machine", out)


# --------------------------------------------------------------------------- against what Cloud showed on 17 Nov 2026

class Cloud:
    """HarnessRouter Cloud and TokenRouter as an httpx handler: the captured turns of one base, replayed.

    The words the capture remembered and uploaded are swapped for the ones this run sends, so the replies say
    what the real agents would have said. `ready`: a reply to put in place of the READY turn's. `env`: what
    the sandbox's `env` printed (the capture has it cut out).
    """

    def __init__(self, base: str, *, ready: dict | None = None, env: str = "HOME=/root\n" + FOREIGN, cents_per_turn: float = 1.02,
                 model_map: dict | None = None, catalogue: dict | None = None, unavailable: tuple = (), tr_slugs: set | None = None,
                 record=lambda text: text):
        self.base, self.ready, self.env = base, ready, env
        self.record = record            # what the service keeps of a posted message (the captures hold the texts of 17 Nov)
        self.harness_status = 200
        self.puts: list[str] = []
        self.matrix = schema.load_matrix("matrix.yaml")
        self.prices = schema.load_prices()
        self.model_map = model_map if model_map is not None else {
            **{r["model"]: "default" for r in self.matrix}, "claude-haiku-4.5": "anthropic", "claude-opus-5.5": "anthropic",
            "gpt-5.6-terra": "openai", "gemini-3-flash-preview": "default"}
        self.catalogue, self.unavailable, self.tr_slugs = catalogue or {}, unavailable, tr_slugs
        self.cents, self.cents_per_turn, self.balance = 12.5, cents_per_turn, 20.0
        self.step = -1
        self.word = self.file_word = None
        self.posts: list[dict] = []
        self.requests: list[tuple[str, str]] = []
        self.pending: dict | None = None

    def fixture(self, kind: str) -> dict:
        text = (CLOUD / f"{self.base}.{STEPS[self.step]}.{kind}.json").read_text(encoding="utf-8")
        was_word = re.search(r"Remember the word (zq\w+)\.", (CLOUD / f"{self.base}.remember.turns.json").read_text(encoding="utf-8")).group(1)
        was_file = re.search(r"\bfx[0-9a-f]{8}\b", (CLOUD / f"{self.base}.files.response.json").read_text(encoding="utf-8")).group(0)
        if self.word:
            text = text.replace(was_word, self.word)
        if self.file_word:
            text = text.replace(was_file, self.file_word)
        return json.loads(text.replace("[environment dump cut]", json.dumps(self.env)[1:-1]))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.requests.append((method, path))
        body = json.loads(request.content) if request.content else {}

        def reply(obj, status: int = 200) -> httpx.Response:
            return httpx.Response(status, json=obj)

        if path == "/v1/balance":
            return reply({"org": "org.test", "plan": "test", "plan_name": "Test", "balance_usd": self.balance, "is_deficit": False,
                          "currency": "usd", "as_of": "2026-11-17T23:45:29Z"})
        if path == "/v1/bases":
            bases: dict[str, list] = {}
            for r in self.matrix:
                bases.setdefault(r["base"], []).append({"id": r["model"], "available": r["model"] not in self.unavailable, "default": False})
            return reply({"bases": [{"id": b, "label": b, "models": models, "tools": [], "takesInputRequests": False} for b, models in bases.items()]})
        if path == "/v1/models":
            return reply({"backends": {}})
        if path == "/v1/integrations":
            return reply({"integrations": [{"name": "default", "provider": "tokenrouter", "config": {"api_key": FAKE_KEY}},
                                           {"name": "anthropic", "provider": "anthropic"}, {"name": "openai", "provider": "openai"}],
                          "model_map": self.model_map})
        if (method, path) == ("POST", "/v1/harnesses"):
            return reply({"id": "chrn_test"}) if self.harness_status == 200 else reply({"error": "Too many requests"}, self.harness_status)
        if (method, path) == ("POST", "/v1/responses"):
            self.posts.append(body)
            self.step += 1
            text = body["input"]
            if text.startswith("Remember the word"):
                self.word = text.split()[3].rstrip(".")
            done = self.fixture("response")
            if self.step == 0 and self.ready is not None:
                done = {**done, **self.ready}
            else:
                self.cents += self.cents_per_turn
            self.pending = done
            return reply({**done, "status": "queued", "output": [], "usage": None, "served_model": None, "error": None})
        if method == "GET" and path.startswith("/v1/responses/"):
            return reply(self.pending)
        if path.endswith("/turns"):
            turns = self.fixture("turns")
            turns["turns"][-1]["user"] = self.record(self.posts[-1]["input"])
            if self.step == 0 and self.ready is not None:
                turns["turns"][-1].update(status=self.ready["status"], served_model=None,
                                          error=(self.ready.get("error") or {}).get("message"))
            return reply(turns)
        if method == "PUT" and "/files/" in path:
            self.file_word = base64.b64decode(body["content_b64"]).decode().strip()
            self.puts.append(path.split("/files/", 1)[1])
            return reply({"session_id": "hsess", "path": self.puts[-1], "bytes": len(self.file_word) + 1, "written": True})
        if path.endswith("/files/archive"):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as z:
                z.writestr("nonce.txt", f"{self.file_word}\n")
            return httpx.Response(200, content=data.getvalue(), headers={"content-type": "application/zip"})
        if path.startswith("/v1/traces/"):
            return reply({"error": "no chunk"}, 404)
        if path == "/tr/dashboard/billing/usage":
            return reply({"object": "list", "total_usage": round(self.cents, 4)})
        if path == "/tr/dashboard/billing/subscription":
            return reply({"object": "billing_subscription", "access_until": 1893456000})
        if path == "/tr/models":
            slugs = self.tr_slugs if self.tr_slugs is not None else \
                {e["slug"] for group in ("tokenrouter", "judge") for e in self.prices[group].values()}
            return reply({"data": [{"id": slug, "object": "model"} for slug in sorted(slugs)]})
        if path == "/tr/api/pricing":
            rows = []
            for group in ("tokenrouter", "judge"):
                for e in self.prices[group].values():
                    factor = self.catalogue.get(e["slug"], 1.0)
                    rows.append({"model_name": e["slug"], "model_ratio": e["input"] * factor / 2, "completion_ratio": e["output"] / e["input"],
                                 "cache_ratio": e.get("cache_read", e["input"]) / e["input"]})
            return reply({"data": rows})
        return reply({"error": f"not in the test's service: {method} {path}"}, 404)


class Replayed(unittest.TestCase):
    """The demo profile, with the service played by `Cloud`. The 90 s pause before the balance is read again is set to 0."""

    def setUp(self):
        self.runs = new_dir()
        self.common = ["--runs-dir", self.runs]
        patches = [unittest.mock.patch.object(preflight, "BALANCE_PAUSE", 0.0),
                   unittest.mock.patch.dict(os.environ, {"RJ_FAKE_KEY": FAKE_KEY})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def go(self, cloud: Cloud, *argv: str) -> tuple[int, str]:
        """`rj preflight` with `cloud` for the service. No check may make or use an Anthropic client: a call through the
        SDK would leave by the SDK's own HTTP client and show in no request list of `cloud`, so both ways to a client
        fail here, and are looked at afterwards in case a check had caught the failure."""
        never = AssertionError("preflight asks Anthropic nothing: it says whether the key is set, and no more")
        with unittest.mock.patch.object(cli, "TRANSPORT", httpx.MockTransport(cloud)), \
                unittest.mock.patch.object(cli.judge, "sdk_client", side_effect=never) as client, \
                unittest.mock.patch.object(cli.judge, "_sdk", side_effect=never) as package:
            done = rj("preflight", *argv, *self.common)
        self.assertEqual((client.called, package.called), (False, False), "no Anthropic client was made and the package was not loaded")
        return done

    def row(self, rid: str) -> dict:
        return schema.validate("PREFLIGHT", load(self.runs / "preflight.json"))["rows"][rid]

    def test_kimi_on_tokenrouter_passes_and_is_pinned(self):
        cloud = Cloud("kimi")
        code, out = self.go(cloud, "--paid", "--rows", "kimi-k27")
        self.assertEqual(code, 0, out)
        row = self.row("kimi-k27")
        self.assertEqual({n: c["status"] for n, c in row["checks"].items()}, {"5": "pass", "6": "pass", "7": "pass", "8": "pass"}, out)
        self.assertEqual((row["pin_served_model"], row["pin_connection"], row["connection_returned"], row["tool_output_visible"]),
                         ("kimi-k2.7-code", None, False, True))
        self.assertEqual((row["payer"], row["provider"]), ("default", "tokenrouter"))
        self.assertAlmostEqual(row["observed"]["tr_usage"]["rise_cents"], 4.08)
        self.assertIn("rose by 4.08 cents", row["checks"]["5"]["evidence"])
        self.assertIn("carry no connection, so that guard is off", row["checks"]["6"]["evidence"])
        files = row["observed"]["files"]
        self.assertEqual((files["python"], files["tool_calls"], files["nonce_in_reply"], files["key_hits"]),
                         ("3.12.15", ["Read", "Bash", "Bash"], True, []))
        self.assertEqual(files["credential_like_env_names"], ["KIMI_MODEL_API_KEY (40 chars)"], "names and lengths only")
        doc = load(self.runs / "preflight.json")
        self.assertEqual((doc["account"]["python"], doc["account"]["agent_work"], doc["checks"]["9"]["status"]),
                         ("3.12.15", "cannot_tell", "cannot_tell"))
        # each turn was posted once, in plain text, with the low cap and a key of its own; the model went with the first only
        self.assertEqual([p["input"].split()[0] for p in cloud.posts], ["Reply", "Remember", "Which", "Hello."])
        # check 8 exercises what the first session depends on: a file two directories deep, and a message built
        # like a scripted one (an opening line, a blank line, a block of several lines), compared byte for byte
        self.assertEqual(cloud.puts, ["rj-preflight/day1/nonce.txt"])
        sent = cloud.posts[3]["input"]
        self.assertEqual((sent.count("\n\n"), len(sent.splitlines())), (1, 7))
        self.assertIn("the turns rows carry the sent bytes on 4 of 4 turns", row["checks"]["8"]["evidence"])
        self.assertEqual([(p["max_step"], p["timeout_seconds"], p["input_requests"]) for p in cloud.posts], [(40, 600, False)] * 4)
        self.assertEqual([p.get("model") for p in cloud.posts], ["kimi-k2.7-code", None, None, None])
        # the ledger at prices.yaml's prices, the dearer reading
        entry = schema.load_prices()["tokenrouter"]["kimi-k2.7-code"]
        paid = [e for e in lines(self.runs / "ledger.jsonl") if e["kind"] == "preflight"]
        want = [pricing.turn_cost_usd(load(CLOUD / f"kimi.{step}.response.json")["usage"], pricing.price_from_yaml(entry)) for step in STEPS]
        self.assertEqual([round(e["cost_usd"], 9) for e in paid], [round(c, 9) for c in want])
        self.assertAlmostEqual(sum(want), 0.0409, places=4, msg="the 4.09 cents of 17 Nov 2026")

    def test_codex_reports_no_served_model_so_it_has_no_pin(self):
        cloud = Cloud("codex")
        code, out = self.go(cloud, "--paid", "--rows", "codex-54mini")
        self.assertEqual(code, 0, out)
        row = self.row("codex-54mini")
        self.assertEqual((row["pin_served_model"], row["checks"]["6"]["status"], row["checks"]["5"]["status"]), (None, "cannot_tell", "pass"))
        self.assertIn("no served_model is reported", row["checks"]["6"]["evidence"])
        code, out = self.go(cloud, "--confirm-usage-log", "codex-54mini")
        self.assertEqual((code, len(cloud.posts)), (0, 4), out)
        row = self.row("codex-54mini")
        self.assertEqual((row["pin_served_model"], row["checks"]["6"]["status"]), (None, "pass"))
        self.assertIsNone(machine.Pins(self.runs / "x", self.runs).get("codex-54mini")["served_model"])

    def test_claude_is_paid_on_another_connection_and_serves_a_dated_name(self):
        cloud = Cloud("claude-code", cents_per_turn=0.0)       # TokenRouter's figure did not move during the Claude turns
        code, out = self.go(cloud, "--paid", "--rows", "cc-haiku45")
        self.assertEqual(code, 1, out)
        row = self.row("cc-haiku45")
        self.assertEqual((row["payer"], row["provider"], row["observed"]["tr_usage"]["rise_cents"]), ("anthropic", "anthropic", 0.0))
        self.assertEqual({n: c["status"] for n, c in row["checks"].items()}, {"5": "cannot_tell", "6": "fail", "7": "pass", "8": "pass"}, out)
        self.assertIn("whose usage rj cannot read", row["checks"]["5"]["evidence"])
        self.assertIn("'claude-haiku-4-5-20251001' is not 'claude-haiku-4.5'", row["checks"]["6"]["evidence"])
        self.assertNotIn("rj replace", out)
        code, out = self.go(cloud, "--accept-served", "cc-haiku45", "--confirm-usage-log", "cc-haiku45")
        self.assertEqual((code, len(cloud.posts)), (0, 4), out)
        row = self.row("cc-haiku45")
        self.assertEqual({n: c["status"] for n, c in row["checks"].items()}, {"5": "pass", "6": "pass", "7": "pass", "8": "pass"})
        self.assertEqual(row["pin_served_model"], "claude-haiku-4-5-20251001")

    def test_a_message_the_service_does_not_keep_byte_for_byte_fails_check_8(self):
        """The scripted messages are several lines with a blank one; a single-line check would never see this."""
        cloud = Cloud("kimi", record=lambda text: text.replace("\n\n", "\n"))
        code, out = self.go(cloud, "--paid", "--rows", "kimi-k27")
        self.assertEqual(code, 1, out)
        row = self.row("kimi-k27")
        self.assertEqual({n: c["status"] for n, c in row["checks"].items()}, {"5": "pass", "6": "pass", "7": "pass", "8": "fail"})
        self.assertIn("a turns row's user text differs from the sent bytes (files)", row["checks"]["8"]["evidence"])

    def test_a_check_that_gets_no_answer_keeps_what_the_row_showed_before(self):
        cloud = Cloud("claude-code", cents_per_turn=0.0)
        self.assertEqual(self.go(cloud, "--paid", "--rows", "cc-haiku45")[0], 1)
        self.assertEqual(self.go(cloud, "--accept-served", "cc-haiku45", "--confirm-usage-log", "cc-haiku45")[0], 0)
        before = self.row("cc-haiku45")
        self.assertEqual(before["pin_served_model"], "claude-haiku-4-5-20251001")
        cloud.harness_status = 429                      # the second check never gets as far as a turn
        code, out = self.go(cloud, "--paid", "--rows", "cc-haiku45")
        self.assertEqual((code, len(cloud.posts)), (1, 4), out)
        self.assertIn("cc-haiku45: NO TURN WAS ANSWERED (the harness could not be made:", out)
        self.assertIn("what its earlier check showed stands:", out)
        after = self.row("cc-haiku45")
        self.assertEqual((after["pin_served_model"], after["hand"], after["observed"], after["at"]),
                         (before["pin_served_model"], before["hand"], before["observed"], before["at"]))
        self.assertEqual({n: c["status"] for n, c in after["checks"].items()}, {"5": "pass", "6": "pass", "7": "pass", "8": "pass"})
        self.assertEqual((after["last_try"]["failure"]["kind"], after["last_try"]["steps"]), ("rate", []))
        self.assertEqual(machine.Pins(self.runs / "x", self.runs).get("cc-haiku45")["served_model"], "claude-haiku-4-5-20251001")
        # a row with nothing on record that gets no answer ends the call with 1 as well
        fresh = Cloud("kimi")
        fresh.harness_status = 429
        code, out = self.go(fresh, "--paid", "--rows", "kimi-k27")
        self.assertEqual((code, fresh.posts), (1, []), out)
        self.assertIn("kimi-k27: NO TURN WAS ANSWERED", out)
        self.assertEqual({n: c["status"] for n, c in self.row("kimi-k27")["checks"].items()}, {"5": "skipped", "6": "skipped", "7": "skipped", "8": "skipped"})

    def test_qwen_with_reasoning_items_reads_the_same(self):
        code, out = self.go(Cloud("qwen"), "--paid", "--rows", "qwen-37plus")
        self.assertEqual(code, 0, out)
        row = self.row("qwen-37plus")
        self.assertEqual((row["pin_served_model"], row["tool_output_visible"], row["observed"]["files"]["tool_calls"]),
                         ("qwen3.7-plus", True, ["read_file", "run_shell_command", "run_shell_command"]))

    def test_a_refused_key_is_an_account_reason_and_steps_nothing_up(self):
        refused = {"status": "failed", "usage": None, "output": [], "served_model": None,
                   "error": {"code": "provider_error", "message": "Your tokenrouter key was refused: provider returned 401 Unauthorized"}}
        cloud = Cloud("claude-code", ready=refused, model_map={"claude-haiku-4.5": "default"})
        code, out = self.go(cloud, "--paid", "--rows", "cc-haiku45")
        self.assertEqual((code, len(cloud.posts)), (1, 1), out)
        row = self.row("cc-haiku45")
        self.assertEqual(({n: c["status"] for n, c in row["checks"].items()}, row["observed"]["failure"]["kind"]),
                         ({"5": "fail", "6": "skipped", "7": "skipped", "8": "skipped"}, "account"))
        self.assertIn("an account reason", out)
        self.assertNotIn("rj replace", out)
        self.assertIsNone(row["pin_served_model"])

    def test_a_provider_400_may_be_the_model_and_proposes_the_step_up(self):
        bad = {"status": "failed", "usage": None, "output": [], "served_model": None,
               "error": {"code": "provider_error", "message": "The turn failed on your tokenrouter connection: API Error: 400 Bad request"}}
        cloud = Cloud("claude-code", ready=bad, model_map={"claude-haiku-4.5": "default"})
        code, out = self.go(cloud, "--paid", "--rows", "cc-haiku45")
        self.assertEqual((code, len(cloud.posts)), (1, 1), out)
        self.assertEqual(self.row("cc-haiku45")["observed"]["failure"]["kind"], "model")
        self.assertIn("proposed, not done, if this is a model reason: rj replace cc-haiku45 cc-sonnet55", out)
        self.assertFalse((self.runs / "replacements.json").exists(), "rj proposes; the owner replaces")

    def test_a_key_in_the_sandbox_stops_and_is_never_written_down(self):
        cloud = Cloud("kimi", env=f"HOME=/root\nTR_API_KEY={FAKE_KEY}\n{FOREIGN}")
        code, out = self.go(cloud, "--paid", "--rows", "kimi-k27")
        self.assertEqual(code, 2, out)
        row = self.row("kimi-k27")
        self.assertEqual((row["checks"]["8"]["status"], row["observed"]["files"]["key_hits"]), ("fail", ["RJ_FAKE_KEY"]))
        self.assertIn("STOP: the first 16 characters of $RJ_FAKE_KEY", out)
        self.assertIn("rotate that key", row["checks"]["8"]["evidence"])
        written = "".join(p.read_text(encoding="utf-8") for p in self.runs.rglob("*") if p.is_file())
        self.assertNotIn(FAKE_KEY[:16], out + written, "neither the key nor its first 16 characters, on screen or on disk")
        self.assertNotIn("k" * 40, out + written, "nor a credential of the sandbox's own")
        self.assertIn("KIMI_MODEL_API_KEY=[MASKED:40 chars]", written)
        self.assertIn("TR_API_KEY=[MASKED:", written)

    def test_the_free_checks_on_three_payers(self):
        cloud = Cloud("kimi")
        with unittest.mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": judges.CLAUDE_KEY}):      # the demo profile's judge is asked at Anthropic
            code, out = self.go(cloud, "--free", "--wallet-usd", "30")
        self.assertEqual(code, 0, out)
        doc = schema.validate("PREFLIGHT", load(self.runs / "preflight.json"))
        self.assertEqual([doc["checks"][n]["status"] for n in ("1", "2", "3")], ["pass"] * 3, out)
        self.assertIn("the judge (claude-opus-5.5) is asked at Anthropic, not at TokenRouter: ANTHROPIC_API_KEY is set; no call was made to try it",
                      doc["checks"]["3"]["evidence"])
        self.assertNotIn("the judge's model", doc["checks"]["3"]["evidence"], "TokenRouter is not asked whether it lists that model")
        self.assertIn("the adaptive writer's model google/gemini-3-flash-preview is listed", doc["checks"]["3"]["evidence"],
                      "the model that writes the adaptive answers is paid on TokenRouter whoever the judge is")
        self.assertEqual(doc["account"]["judge_path"], "/v1/messages")
        self.assertNotIn(judges.CLAUDE_KEY[:16], out + json.dumps(doc))
        self.assertEqual((doc["rows"]["cc-haiku45"]["payer"], doc["rows"]["kimi-k27"]["payer"], doc["rows"]["codex-56terra"]["provider"]),
                         ("anthropic", "default", "openai"))
        self.assertIn("anthropic: cc-haiku45", doc["checks"]["2"]["evidence"])
        self.assertIn("9 of the 9 that TokenRouter is paid for", doc["checks"]["3"]["evidence"], "not the two Claude rows' nor the OpenAI row's")
        self.assertTrue(all(r["question_ending"] == "prose" for r in doc["rows"].values()), "no base takes a typed question")
        self.assertEqual((doc["account"]["b0_usd"], doc["account"]["w0_usd"], doc["account"]["plan"], doc["account"]["tr_usage_cents"]),
                         (20.0, 30.0, "Test", 12.5))
        self.assertEqual(cloud.posts, [])
        self.assertTrue(all(method == "GET" for method, _ in cloud.requests), "read-only")
        saved = "".join(p.read_text(encoding="utf-8") for p in (self.runs / "preflight-raw").rglob("*.json"))
        self.assertNotIn(FAKE_KEY[:16], saved)
        self.assertIn('"api_key": "[REDACTED]"', saved)

    def test_the_free_checks_say_what_is_wrong(self):
        moved = Cloud("kimi", catalogue={"moonshotai/kimi-k2.7-code": 1.05})
        code, out = self.go(moved, "--free")
        doc = load(self.runs / "preflight.json")
        self.assertEqual((code, doc["checks"]["3"]["status"]), (1, "fail"), out)
        self.assertIn("moonshotai/kimi-k2.7-code: the price moved", doc["checks"]["3"]["evidence"])

        self.assertIn("the judge (claude-opus-5.5) is asked at Anthropic, not at TokenRouter: ANTHROPIC_API_KEY is not set",
                      doc["checks"]["3"]["evidence"], "the key is looked for in the environment; Anthropic is not called to try it")
        self.assertTrue(all(method == "GET" and not where.startswith("/v1/messages") for method, where in moved.requests))

        gone = Cloud("kimi", tr_slugs={"google/gemini-3.7-flash"}, unavailable=("qwen3.7-plus",),
                     model_map={r["model"]: "default" for r in schema.load_matrix("matrix.yaml") if r["id"] != "kimi-k27"})
        with judges.tokenrouter_judge():                       # a judge of TokenRouter's must be on its list
            code, out = self.go(gone, "--free")
        doc = load(self.runs / "preflight.json")
        self.assertEqual((code, [doc["checks"][n]["status"] for n in ("1", "2", "3")]), (1, ["fail"] * 3), out)
        self.assertEqual(doc["account"]["judge_path"], "/chat/completions")
        self.assertIn("row qwen-37plus: model 'qwen3.7-plus' is listed but not available", doc["checks"]["1"]["evidence"])
        self.assertIn("row kimi-k27: model 'kimi-k2.7-code' maps to no connection", doc["checks"]["2"]["evidence"])
        self.assertIn("TokenRouter does not list qwen/qwen3.7-plus", doc["checks"]["3"]["evidence"])
        self.assertIn("the judge's model google/gemini-3-flash-preview is not listed; its fallback google/gemini-3.7-flash is",
                      doc["checks"]["3"]["evidence"])

        real = schema.load_profile("demo")
        with unittest.mock.patch.object(schema, "load_profile", return_value={**real, "judge": {**real["judge"], "provider": "openai"}}):
            code, out = self.go(Cloud("kimi"), "--free")
        doc = schema.validate("PREFLIGHT", load(self.runs / "preflight.json"))
        self.assertEqual((code, doc["checks"]["3"]["status"], doc["account"]["judge_path"]), (1, "fail", None), out)
        self.assertIn("judge.provider 'openai' is neither 'tokenrouter' nor 'anthropic'", doc["checks"]["3"]["evidence"])

    def test_the_adaptive_writers_model_is_held_to_tokenrouter_whoever_the_judge_is(self):
        """The writer of the adaptive answers is asked on TokenRouter and priced by its entry under `judge` in prices.yaml.
        While the judge was that same model the judge's check covered it; with the judge at Anthropic it has its own."""
        writer = "google/gemini-3-flash-preview"
        listed = {e["slug"] for group in ("tokenrouter", "judge") for e in schema.load_prices()[group].values()}

        def third(cloud: Cloud) -> tuple[int, str]:
            with unittest.mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": judges.CLAUDE_KEY}):
                code, out = self.go(cloud, "--free")
            return code, load(self.runs / "preflight.json")["checks"]["3"]["evidence"]

        code, said = third(Cloud("kimi", catalogue={writer: 1.05}))
        self.assertEqual(code, 1, said)
        self.assertIn(f"{writer}: the price moved", said)
        code, said = third(Cloud("kimi", tr_slugs=listed - {writer}))
        self.assertEqual(code, 1, said)
        self.assertIn(f"the adaptive writer's model {writer} is not listed", said)
        real = schema.load_profile("demo")
        for adaptive in ({"enabled": False}, {"stub": True}):          # no writer is asked on TokenRouter: nothing of it is checked
            with unittest.mock.patch.object(schema, "load_profile", return_value={**real, "adaptive": {**real["adaptive"], **adaptive}}):
                code, said = third(Cloud("kimi", tr_slugs=listed - {writer}))
            self.assertEqual((code, "adaptive writer" in said), (0, False), said)
        with unittest.mock.patch.object(schema, "load_profile", return_value={**real, "adaptive": {**real["adaptive"], "model": "no-such-model"}}):
            code, said = third(Cloud("kimi"))
        self.assertEqual(code, 1, said)
        self.assertIn("prices.yaml has no entry under `judge` for the adaptive writer's model 'no-such-model'", said)
        with judges.tokenrouter_judge():                       # the writer's model is the judge's: said once, as the judge's, as it was
            code, said = third(Cloud("kimi"))
        self.assertEqual((code, "adaptive writer" in said), (0, False), said)
        self.assertIn(f"the judge's model {writer} is listed", said)

    def test_a_row_without_a_price_is_not_sent_a_turn(self):
        matrix = new_dir("matrix-") / "matrix.yaml"
        matrix.write_text(yaml.safe_dump([{"id": "mystery", "base": "kimi", "model": "mystery-1", "role": "core"}]), encoding="utf-8")
        cloud = Cloud("kimi", model_map={"mystery-1": "default"})
        code, out = self.go(cloud, "--paid", "--matrix", str(matrix))
        self.assertEqual((code, cloud.posts), (2, []), out)
        self.assertIn("prices.yaml has no entry for 'mystery-1'", out)

    def test_the_guard_refuses_a_row_when_the_budget_is_spent(self):
        budget = schema.load_profile("demo")["budget"]         # the owner's to set: all but 0.05 USD of what it leaves above the floor
        spent = budget["total_usd"] - budget["floor_usd"] - 0.05
        (self.runs / "ledger.jsonl").write_text(json.dumps({"kind": "judge", "cost_usd": spent, "provider": "tokenrouter"}) + "\n")
        cloud = Cloud("kimi")
        code, out = self.go(cloud, "--paid", "--rows", "kimi-k27")
        self.assertEqual((code, cloud.posts), (2, []), out)
        self.assertIn("the cost guard refuses it", out)


class Scrubbing(unittest.TestCase):
    def test_keys_are_scrubbed_redacted_and_masked(self):
        with unittest.mock.patch.dict(os.environ, {"RJ_FAKE_KEY": FAKE_KEY, "RJ_SHORT_KEY": "abc"}):
            self.assertEqual(preflight.scrub(f"a {FAKE_KEY} b {FAKE_KEY[:16]}x abc"), "a [KEY] b [KEY-PREFIX]x abc")
            self.assertEqual(preflight.key_hits("x" + FAKE_KEY[:16], b"nothing"), ["RJ_FAKE_KEY"])
            self.assertEqual(preflight.key_hits(FAKE_KEY[:15]), [])
        self.assertEqual(preflight.redact({"config": {"api_key": "v", "base_url": "u"}, "usage": {"input_tokens": 3}, "list": [{"secret": "s"}]}),
                         {"config": {"api_key": "[REDACTED]", "base_url": "u"}, "usage": {"input_tokens": 3}, "list": [{"secret": "[REDACTED]"}]})
        self.assertEqual(preflight.mask_env("PATH=/bin ANTHROPIC_API_KEY=abcdef GH_TOKEN=xyz"),
                         "PATH=/bin ANTHROPIC_API_KEY=[MASKED:6 chars] GH_TOKEN=[MASKED:3 chars]")


if __name__ == "__main__":
    unittest.main()
