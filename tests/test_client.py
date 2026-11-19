"""W3: the HarnessRouter client against httpx.MockTransport.

Plan section 6 (the client table and the rules) and section 5 (the wire shapes). No socket is opened.
"""
import base64
import io
import json
import os
import traceback
import unittest
import zipfile
from unittest import mock

import httpx

from rj import schema
from rj.hr import HRClient, HRError, is_rejected_followup
from rj.hr.client import PUT_LIMIT_BYTES

BASE = "https://hr.test"
KEY = "hrk-live-0123456789abcdef-do-not-print"
LIMITS = {"max_step": 400, "timeout_seconds": 1500}
ROW = {"id": "kimi-k27", "base": "kimi", "model": "kimi-k2.7-code", "side": "chinese", "role": "core", "next": "dsh-v4pro"}
FIXTURE_SESSIONS = schema.ROOT / "tests" / "fixtures" / "run" / "sessions"

BASES = {"bases": [{"id": "kimi", "label": "Kimi Code CLI",
                    "models": [{"id": "kimi-k2.7-code", "available": True, "default": True}],
                    "tools": [{"name": "Bash", "label": "Bash"}], "takesInputRequests": False}]}
BALANCE = {"balance_usd": 12.5, "is_deficit": False, "as_of": "2026-11-17T10:00:00Z"}
INTEGRATIONS = {"integrations": [{"name": "rj-tokenrouter", "provider": "tokenrouter"}],
                "model_map": {"claude-haiku-4.5": "rj-tokenrouter"}}
PUT_OK = {"session_id": "sess_1", "path": "slotbook/book.py", "bytes": 5, "written": True}
BUSY = (409, {"error": {"code": "session_busy", "message": "a turn is in progress"}})
STORE_DOWN = (503, {"error": {"message": "Idempotency store unavailable, retry without a key"}})


def response(status="completed", rid="resp_1", **over):
    return {"id": rid, "status": status, "error": None, "incomplete_details": None, "previous_response_id": None,
            "model": "kimi-k2.7-code", "served_model": "kimi-k2.7-code", "usage": None, "output": [],
            "metadata": {"session_id": "sess_1"}, **over}


class Server:
    """A MockTransport handler: records every request; answers from `handler`, else from `replies` in order.

    A reply is an httpx.Response or a `(status, json)` pair.
    """

    def __init__(self, *replies, handler=None):
        self.requests: list[httpx.Request] = []
        self.replies = list(replies)
        self.handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler:
            reply = self.handler(request)
        elif self.replies:
            reply = self.replies.pop(0)
        else:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        return reply if isinstance(reply, httpx.Response) else httpx.Response(reply[0], json=reply[1])

    def lines(self) -> list[str]:
        return [f"{r.method} {r.url.raw_path.decode()}" for r in self.requests]

    def body(self, i: int = -1):
        return json.loads(self.requests[i].content)


class ClientCase(unittest.IsolatedAsyncioTestCase):
    def client(self, *replies, handler=None, base_url=BASE, api_key=KEY, **kw) -> HRClient:
        self.server = Server(*replies, handler=handler)
        self.slept: list[float] = []

        async def sleep(seconds):
            self.slept.append(seconds)

        kw.setdefault("sleep", sleep)
        client = HRClient(base_url, api_key, transport=httpx.MockTransport(self.server), **kw)
        self.addAsyncCleanup(client.aclose)
        return client


class Reads(ClientCase):
    async def test_bases_models_balance_integrations(self):
        models = {"object": "list", "data": [{"id": "kimi-k2.7-code"}]}
        hr = self.client((200, BASES), (200, models), (200, BALANCE), (200, INTEGRATIONS))
        self.assertEqual(await hr.bases(), BASES)
        self.assertEqual(await hr.models(), models)
        self.assertEqual(await hr.balance(), schema.Balance(12.5, False, "2026-11-17T10:00:00Z"))
        self.assertEqual(await hr.integrations(), INTEGRATIONS)
        self.assertEqual(self.server.lines(),
                         ["GET /v1/bases", "GET /v1/models", "GET /v1/balance", "GET /v1/integrations"])
        for request in self.server.requests:
            self.assertEqual(request.headers["authorization"], f"Bearer {KEY}")
            self.assertEqual(request.content, b"")
            self.assertNotIn("idempotency-key", request.headers)
        self.assertEqual(hr.raw["GET /v1/balance"], BALANCE, "one raw reply per route stays available")

    async def test_models_envelope_is_not_checked(self):
        hr = self.client((200, ["kimi-k2.7-code"]), (200, {"anything": 1}))
        self.assertEqual(await hr.models(), ["kimi-k2.7-code"])
        self.assertEqual(await hr.models(), {"anything": 1})

    async def test_turns(self):
        row = json.loads(next(FIXTURE_SESSIONS.glob("*/F/*/*/*/turns/01-kickoff.s0/turns_row.json")).read_text())
        envelope = {"session_id": "sess_fx_f1", "turns": [row], "last_response_id": row["id"]}
        hr = self.client((200, envelope))
        rows = await hr.turns("sess_fx_f1")
        self.assertEqual(self.server.lines(), ["GET /v1/sessions/sess_fx_f1/turns"])
        self.assertEqual(rows, [schema.TurnRow.from_dict(row)])
        self.assertEqual(rows[0].user, row["user"])
        self.assertEqual(rows[0].raw, row)
        self.assertEqual(hr.raw["GET /v1/sessions/{sid}/turns"]["last_response_id"], row["id"])

    async def test_trace_reads_chunks_until_404(self):
        chunks = [b'{"type":"system"}\n{"type":"assistant"}\n', b'{"type":"result"}\n']

        def handler(request):
            n = int(request.url.params["chunk"])
            return httpx.Response(200, content=chunks[n]) if n < len(chunks) else httpx.Response(404, json={"error": "no chunk"})

        hr = self.client(handler=handler)
        self.assertEqual(await hr.trace("sess_1"), chunks)
        self.assertEqual(self.server.lines(), [f"GET /v1/traces/sess_1/events?chunk={n}" for n in (0, 1, 2)])

    async def test_trace_without_chunks_is_empty_and_other_errors_raise(self):
        hr = self.client((404, {"error": "not found"}), (500, {"error": "boom"}))
        self.assertEqual(await hr.trace("sess_1"), [])
        with self.assertRaises(HRError) as caught:
            await hr.trace("sess_1")
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(caught.exception.route, "GET /v1/traces/sess_1/events?chunk=0")

    async def test_trace_gives_up_on_a_server_that_never_says_404(self):
        hr = self.client(handler=lambda request: httpx.Response(200, content=b"{}\n"))
        with self.assertRaisesRegex(HRError, "no 404 after 3 chunks"):
            await hr.trace("sess_1", max_chunks=3)
        self.assertEqual(len(self.server.requests), 3)

    async def test_archive_returns_the_zip_bytes(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("slotbook/book.py", "def book():\n    return None\n")
        hr = self.client(httpx.Response(200, content=buffer.getvalue(), headers={"content-type": "application/zip"}))
        data = await hr.archive("sess_1")
        self.assertIsInstance(data, bytes)
        self.assertEqual(data, buffer.getvalue())
        self.assertEqual(zipfile.ZipFile(io.BytesIO(data)).namelist(), ["slotbook/book.py"])
        self.assertEqual(self.server.lines(), ["GET /v1/sessions/sess_1/files/archive"])


class Harness(ClientCase):
    async def test_ensure_harness_posts_the_table_body(self):
        hr = self.client((200, {"id": "h_7", "name": "rj-kimi-k27"}))
        self.assertEqual(await hr.ensure_harness(ROW), "h_7")
        self.assertEqual(self.server.lines(), ["POST /v1/harnesses"])
        self.assertEqual(self.server.body(), {"name": "rj-kimi-k27", "base": "kimi", "default_model": "kimi-k2.7-code"})
        self.assertTrue(self.server.requests[0].headers["idempotency-key"])
        self.assertEqual(hr.raw["POST /v1/harnesses"], {"id": "h_7", "name": "rj-kimi-k27"})
        self.assertEqual(await hr.ensure_harness(ROW), "h_7")
        self.assertEqual(len(self.server.requests), 1, "one client asks once per row")

    async def test_the_idempotency_key_follows_the_body(self):
        keys = []
        for row in (ROW, dict(ROW), {**ROW, "model": "kimi-k3"}):
            hr = self.client((200, {"id": "h_7"}))
            await hr.ensure_harness(row)
            keys.append(self.server.requests[0].headers["idempotency-key"])
        self.assertEqual(keys[0], keys[1])
        self.assertNotEqual(keys[0], keys[2])

    async def test_a_harness_in_place_of_a_lost_one_is_asked_for_under_a_key_of_its_own(self):
        body = {"name": "rj-kimi-k27", "base": "kimi", "default_model": "kimi-k2.7-code"}
        hr = self.client((200, {"id": "h_7"}), (200, {"id": "h_8"}), (200, {"id": "h_9"}))
        self.assertEqual(await hr.ensure_harness(ROW), "h_7")
        self.assertEqual(await hr.ensure_harness(ROW, instead_of="h_7"), "h_8", "asked again, though this client remembers h_7")
        self.assertEqual(await hr.ensure_harness(ROW), "h_8", "what it remembers from then on is the new one")
        self.assertEqual(await hr.ensure_harness(ROW, instead_of="h_8"), "h_9")
        self.assertEqual([json.loads(r.content) for r in self.server.requests], [body] * 3)
        keys = [r.headers["idempotency-key"] for r in self.server.requests]
        self.assertEqual(len(set(keys)), 3, "under the first key a service that remembers it would hand the lost harness back")
        other = self.client((200, {"id": "h_8"}))
        self.assertEqual(await other.ensure_harness(ROW, instead_of="h_7"), "h_8")
        self.assertEqual(self.server.requests[0].headers["idempotency-key"], keys[1], "the same lost id gives the same key, in any process")

    async def test_id_is_read_from_id_then_harness_id(self):
        hr = self.client((200, {"harness_id": "h_8"}))
        self.assertEqual(await hr.ensure_harness(ROW), "h_8")
        hr = self.client((201, {"id": "h_9", "harness_id": "h_other"}))
        self.assertEqual(await hr.ensure_harness(ROW), "h_9")
        hr = self.client((200, {"harness": {"name": "rj-kimi-k27"}}))
        with self.assertRaisesRegex(HRError, "no harness id") as caught:
            await hr.ensure_harness(ROW)
        self.assertEqual(json.loads(caught.exception.body), {"harness": {"name": "rj-kimi-k27"}})

    async def test_harness_reads_one_and_an_unknown_id_is_a_404(self):
        held = {"id": "h_7", "name": "rj-kimi-k27", "base": "kimi", "defaultModel": "kimi-k2.7-code"}
        gone = {"error": {"code": "harness_not_found", "message": "no harness 'h gone'"}}
        hr = self.client((200, held), (404, gone), (200, {"harness_id": "h_7"}), (200, {"name": "rj-kimi-k27"}))
        self.assertEqual(await hr.harness("h_7"), held)
        self.assertEqual(hr.raw["GET /v1/harnesses/{id}"], held)
        with self.assertRaises(HRError) as caught:
            await hr.harness("h gone")
        e = caught.exception
        self.assertEqual((e.status, e.route), (404, "GET /v1/harnesses/h%20gone"))
        self.assertIn("harness_not_found", e.excerpt)
        self.assertEqual(await hr.harness("h_7"), {"harness_id": "h_7"}, "the id is read from `id`, then `harness_id`")
        self.assertEqual(await hr.harness("h_7"), {"name": "rj-kimi-k27"}, "a reply that names no id is taken as it is")
        self.assertEqual(self.server.lines(), ["GET /v1/harnesses/h_7", "GET /v1/harnesses/h%20gone"] + ["GET /v1/harnesses/h_7"] * 2)
        for request in self.server.requests:
            self.assertEqual(request.content, b"")
            self.assertNotIn("idempotency-key", request.headers)
        self.assertEqual(len(self.server.requests), 4, "asked every time: the client keeps no harness it was only shown")

    async def test_a_harness_reply_that_is_no_object_or_names_another_id_does_not_fit(self):
        for reply, message in ((["h_7"], "expected an object"), ({"id": "h_other", "name": "rj-kimi-k27"}, "not the id asked for")):
            hr = self.client((200, reply))
            with self.assertRaisesRegex(HRError, message) as caught:
                await hr.harness("h_7")
            self.assertEqual((caught.exception.status, json.loads(caught.exception.body)), (200, reply))


class Send(ClientCase):
    async def test_first_turn_body(self):
        hr = self.client((200, response("queued")))
        reply = await hr.send("Reply READY.", harness_id="h_7", model="kimi-k2.7-code", limits=LIMITS,
                              key="demo:kimi-k27/N/possible/A/r01:seed:0")
        self.assertEqual(self.server.lines(), ["POST /v1/responses"])
        self.assertEqual(self.server.body(), {
            "input": "Reply READY.", "background": True, "input_requests": False,
            "max_step": 400, "timeout_seconds": 1500, "metadata": {"harness_id": "h_7"},
            "model": "kimi-k2.7-code",
        })
        request = self.server.requests[0]
        self.assertEqual(request.headers["idempotency-key"], "demo:kimi-k27/N/possible/A/r01:seed:0")
        self.assertEqual(request.headers["authorization"], f"Bearer {KEY}")
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertIsInstance(reply, schema.Response)
        self.assertEqual((reply.id, reply.status, reply.session_id, reply.live), ("resp_1", "queued", "sess_1", True))

    async def test_later_turn_body(self):
        text = "got it. you absolute clown.\n\nPlan approved — build part A. Naïve café ✓"
        hr = self.client((200, response("queued", "resp_2", previous_response_id="resp_1", model=None)))
        reply = await hr.send(text, harness_id="h_7", model="kimi-k2.7-code", prev="resp_1", limits=LIMITS, key="k2")
        self.assertEqual(self.server.body(), {
            "input": text, "background": True, "input_requests": False,
            "max_step": 400, "timeout_seconds": 1500, "metadata": {"harness_id": "h_7"},
            "previous_response_id": "resp_1",
        })
        self.assertEqual(self.server.requests[0].headers["idempotency-key"], "k2")
        self.assertEqual(reply.previous_response_id, "resp_1")

    async def test_bodies_equal_the_fixture_requests(self):
        for name, model, prev in (("00-seed.s0", "persona:fixture", None), ("01-kickoff.s0", None, "resp_fx_f1_01")):
            saved = json.loads(next(FIXTURE_SESSIONS.glob(f"*/F/*/*/*/turns/{name}/request.json")).read_text())
            hr = self.client((200, response("queued")))
            await hr.send(saved["input"], harness_id="h_fixture", model=model, prev=prev, limits=LIMITS, key="k")
            self.assertEqual(self.server.body(), saved, name)
            self.assertEqual(HRClient.request_body(saved["input"], harness_id="h_fixture", model=model, prev=prev,
                                                   limits=LIMITS), saved, name)

    async def test_send_never_carries_what_the_rules_forbid(self):
        hr = self.client((200, response("queued")), (200, response("queued", "resp_2")))
        await hr.send("a", harness_id="h_7", model="m", limits={**LIMITS, "max_cost_usd": 5, "stream": True}, key="k1")
        await hr.send("b", harness_id="h_7", prev="resp_1", limits=LIMITS, key="k2")
        for i in (0, 1):
            self.assertFalse({"max_cost_usd", "instructions", "stream", "attachments", "tools"} & set(self.server.body(i)))
            self.assertIsInstance(self.server.body(i)["input"], str)
        self.assertNotIn("model", self.server.body(1))

    async def test_send_refuses_bad_arguments_before_sending(self):
        hr = self.client()
        with self.assertRaises(TypeError):
            await hr.send([{"type": "input_response", "text": "A"}], harness_id="h", model="m", limits=LIMITS, key="k")
        with self.assertRaisesRegex(ValueError, "model"):
            await hr.send("hi", harness_id="h", limits=LIMITS, key="k")
        with self.assertRaisesRegex(ValueError, "max_step"):
            await hr.send("hi", harness_id="h", model="m", limits={"timeout_seconds": 1500}, key="k")
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            await hr.send("hi", harness_id="h", model="m", limits={"max_step": 400, "timeout_seconds": None}, key="k")
        self.assertEqual(self.server.requests, [])

    async def test_a_rejected_follow_up(self):
        hr = self.client((400, {"error": {"message": "previous response not found"}}))
        with self.assertRaises(HRError) as caught:
            await hr.send("next", harness_id="h_7", prev="resp_gone", limits=LIMITS, key="k")
        e = caught.exception
        self.assertEqual((e.status, e.route), (400, "POST /v1/responses"))
        self.assertIn("previous response not found", e.excerpt)
        self.assertIn("previous response not found", str(e))
        self.assertTrue(is_rejected_followup(e))


class Poll(ClientCase):
    async def test_poll_loops_to_a_terminal_status_and_survives_503(self):
        done = response("incomplete", incomplete_details={"reason": "max_steps"},
                        output=[{"type": "message", "content": "FORK-2: which option?"}])
        hr = self.client((200, response("queued")), (200, response("in_progress")), (503, {"error": "busy"}),
                         (200, response("in_progress")), (200, done))
        reply = await hr.poll("resp_1")
        self.assertEqual(self.server.lines(), ["GET /v1/responses/resp_1"] * 5)
        self.assertEqual(self.slept, [2.5] * 4)
        self.assertEqual((reply.status, reply.incomplete_reason, reply.live), ("incomplete", "max_steps", False))
        self.assertEqual(reply.final_message(), "FORK-2: which option?")
        self.assertEqual(reply.raw, done)

    async def test_poll_returns_at_once_on_a_finished_turn(self):
        for status in ("completed", "incomplete", "failed", "cancelled"):
            over = {"incomplete_details": {"reason": "timeout"}} if status == "incomplete" else {}
            hr = self.client((200, response(status, **over)))
            self.assertEqual((await hr.poll("resp_1")).status, status)
            self.assertEqual(self.slept, [])

    async def test_poll_serves_the_fixture_replies(self):
        files = sorted(FIXTURE_SESSIONS.glob("*/*/*/*/*/turns/*/response.json"))
        self.assertTrue(files)
        for path in files:
            saved = json.loads(path.read_text())
            hr = self.client((200, saved))
            reply = await hr.poll(saved["id"])
            self.assertEqual(reply.raw, saved)
            self.assertEqual(reply.session_id, saved["metadata"]["session_id"])

    async def test_poll_has_an_overall_time_limit(self):
        hr = self.client(handler=lambda request: httpx.Response(200, json=response("in_progress")))
        with self.assertRaises(HRError) as caught:
            await hr.poll("resp_1", timeout=10)
        self.assertEqual(self.slept, [2.5] * 4)
        self.assertEqual(len(self.server.requests), 5)
        self.assertIsNone(caught.exception.status)
        self.assertIn("in_progress", str(caught.exception))
        self.assertFalse(is_rejected_followup(caught.exception))

    async def test_the_time_limit_also_ends_a_run_of_503(self):
        hr = self.client(handler=lambda request: httpx.Response(503, json={"error": "busy"}), poll_seconds=1)
        with self.assertRaisesRegex(HRError, "HTTP 503"):
            await hr.poll("resp_1", timeout=3)
        self.assertEqual(self.slept, [1, 1, 1])

    async def test_poll_takes_its_own_sleep_and_interval(self):
        naps = []

        async def nap(seconds):
            naps.append(seconds)

        hr = self.client((200, response("queued")), (200, response("completed")), poll_seconds=0.25)
        await hr.poll("resp_1", sleep=nap)
        self.assertEqual((naps, self.slept), ([0.25], []))

    async def test_poll_does_not_retry_other_errors(self):
        hr = self.client((500, {"error": "boom"}))
        with self.assertRaises(HRError) as caught:
            await hr.poll("resp_1")
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(len(self.server.requests), 1)


class Put(ClientCase):
    async def test_put_retries_409_then_succeeds(self):
        hr = self.client(BUSY, BUSY, (200, PUT_OK))
        reply = await hr.put("sess_1", "slotbook/book.py", b"hello")
        self.assertEqual(reply, PUT_OK)
        self.assertEqual(self.server.lines(), ["PUT /v1/sessions/sess_1/files/slotbook/book.py"] * 3)
        for i in range(3):
            self.assertEqual(self.server.body(i), {"content_b64": base64.b64encode(b"hello").decode()})
            self.assertNotIn("idempotency-key", self.server.requests[i].headers)
        self.assertEqual(self.slept, [0.5, 1.0])

    async def test_put_gives_up_after_its_limit(self):
        hr = self.client(handler=lambda request: httpx.Response(409, json=BUSY[1]))
        with self.assertRaises(HRError) as caught:
            await hr.put("sess_1", "TASK.md", b"x", tries=4)
        self.assertEqual(len(self.server.requests), 4)
        self.assertEqual(self.slept, [0.5, 1.0, 2.0])
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("session_busy", caught.exception.excerpt)
        self.assertFalse(is_rejected_followup(caught.exception))

    async def test_put_default_limit_is_bounded(self):
        hr = self.client(handler=lambda request: httpx.Response(409, json=BUSY[1]))
        with self.assertRaises(HRError):
            await hr.put("sess_1", "TASK.md", b"x")
        self.assertEqual(len(self.server.requests), 6)
        self.assertEqual(self.slept, [0.5, 1.0, 2.0, 4.0, 8.0])

    async def test_put_refuses_over_4_mib_before_sending(self):
        hr = self.client((200, {**PUT_OK, "bytes": PUT_LIMIT_BYTES}))
        with self.assertRaisesRegex(HRError, "4 MiB") as caught:
            await hr.put("sess_1", "big.bin", b"\0" * (PUT_LIMIT_BYTES + 1))
        self.assertIsNone(caught.exception.status)
        self.assertEqual(self.server.requests, [])
        await hr.put("sess_1", "big.bin", b"\0" * PUT_LIMIT_BYTES)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(PUT_LIMIT_BYTES, 4 * 1024 * 1024)

    async def test_put_takes_text_and_quotes_the_path(self):
        hr = self.client((200, {**PUT_OK, "path": "docs/release notes.md"}))
        await hr.put("sess_1", "docs/release notes.md", "naïve\n")
        self.assertEqual(self.server.lines(), ["PUT /v1/sessions/sess_1/files/docs/release%20notes.md"])
        self.assertEqual(base64.b64decode(self.server.body()["content_b64"]), "naïve\n".encode("utf-8"))
        for bad in ("", "/etc/passwd", "a/../../b", "a//b", "dir/"):
            with self.assertRaises(ValueError, msg=bad):
                await hr.put("sess_1", bad, b"x")
        self.assertEqual(len(self.server.requests), 1)

    async def test_put_other_errors_and_unwritten_files_raise(self):
        hr = self.client((413, {"error": "too large"}), (200, {**PUT_OK, "written": False}))
        with self.assertRaises(HRError) as caught:
            await hr.put("sess_1", "a.txt", b"x")
        self.assertEqual(caught.exception.status, 413)
        with self.assertRaisesRegex(HRError, "not written"):
            await hr.put("sess_1", "a.txt", b"x")
        self.assertEqual(self.slept, [])


class Idempotency(ClientCase):
    async def test_store_unavailable_drops_keys_for_good_and_retries_once(self):
        hr = self.client(STORE_DOWN, (200, response("queued")), (200, response("queued", "resp_2")), (200, {"id": "h_7"}))
        self.assertTrue(hr.idempotency)
        with self.assertLogs("rj.hr", level="WARNING") as logs:
            reply = await hr.send("Reply READY.", harness_id="h_7", model="m", limits=LIMITS, key="k1")
        self.assertEqual(reply.status, "queued")
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(self.server.lines(), ["POST /v1/responses"] * 2)
        self.assertEqual(self.server.requests[0].headers["idempotency-key"], "k1")
        self.assertNotIn("idempotency-key", self.server.requests[1].headers)
        self.assertEqual(self.server.body(0), self.server.body(1), "the retry is the same request")
        self.assertFalse(hr.idempotency)

        await hr.send("next", harness_id="h_7", prev="resp_1", limits=LIMITS, key="k2")
        await hr.ensure_harness(ROW)
        self.assertEqual(self.server.lines()[2:], ["POST /v1/responses", "POST /v1/harnesses"])
        for request in self.server.requests[2:]:
            self.assertNotIn("idempotency-key", request.headers)
        self.assertEqual(self.slept, [])

    async def test_the_retry_happens_once(self):
        hr = self.client(STORE_DOWN, STORE_DOWN)
        with self.assertLogs("rj.hr", level="WARNING"), self.assertRaises(HRError) as caught:
            await hr.send("Reply READY.", harness_id="h_7", model="m", limits=LIMITS, key="k1")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(self.server.requests), 2)
        self.assertFalse(hr.idempotency)

    async def test_any_other_503_is_not_retried_and_keeps_the_keys(self):
        hr = self.client((503, {"error": "overloaded"}), (200, response("queued")))
        with self.assertRaises(HRError) as caught:
            await hr.send("Reply READY.", harness_id="h_7", model="m", limits=LIMITS, key="k1")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(self.server.requests), 1)
        self.assertTrue(hr.idempotency)
        await hr.send("Reply READY.", harness_id="h_7", model="m", limits=LIMITS, key="k1")
        self.assertEqual(self.server.requests[1].headers["idempotency-key"], "k1")


class Watch(ClientCase):
    EVENTS = [
        {"session_id": "sess_1", "response_id": "resp_1", "type": "response.output_item.done",
         "item": {"type": "function_call", "call_id": "c1", "name": "Bash", "arguments": "{\"command\": \"sh ci\"}"}},
        {"session_id": "sess_1", "response_id": "resp_1", "type": "response.completed"},
    ]

    def stream(self, text: str) -> httpx.Response:
        return httpx.Response(200, content=text.encode("utf-8"), headers={"content-type": "text/event-stream"})

    async def test_watch_parses_events(self):
        text = (": connected\n\n"
                f"event: response.output_item.done\nid: 1\ndata: {json.dumps(self.EVENTS[0])}\n\n"
                ": keep-alive\n\n"
                f"data:{json.dumps(self.EVENTS[1])}\n\n"
                "data: [DONE]\n\n"
                "data: {\"never\": \"read\"}\n\n")
        hr = self.client(self.stream(text))
        seen = [event async for event in hr.watch("h_7")]
        self.assertEqual(seen, self.EVENTS)
        self.assertEqual(self.server.lines(), ["GET /v1/harnesses/h_7/events"])
        self.assertEqual(self.server.requests[0].headers["accept"], "text/event-stream")
        self.assertEqual(self.server.requests[0].headers["authorization"], f"Bearer {KEY}")

    async def test_watch_reads_crlf_split_data_and_an_unclosed_last_event(self):
        split = json.dumps(self.EVENTS[0], indent=1).split("\n")
        text = "".join(f"data: {line}\r\n" for line in split) + "\r\n" + f"data: {json.dumps(self.EVENTS[1])}"
        hr = self.client(self.stream(text))
        self.assertEqual([event async for event in hr.watch("h_7")], self.EVENTS)

    async def test_watch_reads_one_document_per_data_line(self):
        text = "".join(f"data: {json.dumps(event)}\n" for event in self.EVENTS) + "\n"
        hr = self.client(self.stream(text))
        self.assertEqual([event async for event in hr.watch("h_7")], self.EVENTS)

    async def test_watch_raises_on_data_that_is_not_json_and_on_http_errors(self):
        hr = self.client(self.stream(f"data: {json.dumps(self.EVENTS[0])}\n\ndata: <html>\n\n"), (404, {"error": "no harness"}))
        seen = []
        with self.assertRaisesRegex(HRError, "not JSON"):
            async for event in hr.watch("h_7"):
                seen.append(event)
        self.assertEqual(seen, self.EVENTS[:1])
        with self.assertRaises(HRError) as caught:
            async for event in hr.watch("h_gone"):
                self.fail("no event expected")
        self.assertEqual((caught.exception.status, caught.exception.route), (404, "GET /v1/harnesses/h_gone/events"))
        self.assertIn("no harness", caught.exception.excerpt)


class CommunityEdition(ClientCase):
    async def test_ce_reads_admin_integrations_and_has_no_balance(self):
        hr = self.client((200, INTEGRATIONS), base_url="http://localhost:3000/api/harness/", edition="ce")
        self.assertIsNone(await hr.balance())
        self.assertEqual(self.server.requests, [], "balance() does not call the server on ce")
        self.assertEqual(await hr.integrations(), INTEGRATIONS)
        self.assertEqual(self.server.lines(), ["GET /api/harness/v1/admin/integrations"])
        self.assertEqual(self.server.requests[0].url.host, "localhost")

    async def test_ce_keeps_every_other_route(self):
        hr = self.client((200, BASES), (200, response("queued")), base_url="http://localhost:3000/api/harness", edition="ce")
        await hr.bases()
        await hr.send("Reply READY.", harness_id="h_7", model="m", limits=LIMITS, key="k")
        self.assertEqual(self.server.lines(), ["GET /api/harness/v1/bases", "POST /api/harness/v1/responses"])

    def test_unknown_edition(self):
        with self.assertRaisesRegex(ValueError, "edition"):
            HRClient(BASE, KEY, edition="enterprise")


class Malformed(ClientCase):
    async def test_a_reply_that_does_not_fit_raises_with_the_validator_message(self):
        bad = response("napping")
        hr = self.client((200, bad))
        with self.assertRaises(HRError) as caught:
            await hr.poll("resp_1")
        e = caught.exception
        self.assertEqual((e.status, e.route), (200, "GET /v1/responses/resp_1"))
        with self.assertRaises(schema.SchemaError) as wanted:
            schema.validate("RESPONSE", bad)
        self.assertIn(str(wanted.exception), str(e))
        self.assertIn("response.status", e.detail)
        self.assertEqual(json.loads(e.body), bad, "the raw body stays available")
        self.assertFalse(is_rejected_followup(e))

    async def test_every_checked_route_rejects_a_wrong_shape(self):
        calls = {
            "bases": (lambda hr: hr.bases(), {"bases": [{"label": "no id"}]}, "missing key 'id'"),
            "balance": (lambda hr: hr.balance(), {"balance_usd": "12.50"}, "balance.balance_usd"),
            "integrations": (lambda hr: hr.integrations(), {"integrations": [{"name": "x"}]}, "missing key 'provider'"),
            "send": (lambda hr: hr.send("hi", harness_id="h", model="m", limits=LIMITS, key="k"), {"id": "resp_1"}, "missing key"),
            "put": (lambda hr: hr.put("sess_1", "a.txt", b"x"), {"path": "a.txt"}, "missing key 'written'"),
            "turns": (lambda hr: hr.turns("sess_1"), {"session_id": "sess_1", "turns": [{"id": "resp_1"}]}, r"turns\.turns\[0\]"),
            "list": (lambda hr: hr.bases(), ["not", "an", "object"], "expected an object"),
        }
        for name, (call, reply, message) in calls.items():
            hr = self.client((200, reply))
            with self.assertRaisesRegex(HRError, message, msg=name) as caught:
                await call(hr)
            self.assertEqual(caught.exception.status, 200, name)
            self.assertEqual(json.loads(caught.exception.body), reply, name)

    async def test_a_reply_that_is_not_json(self):
        hr = self.client(httpx.Response(200, content=b"<html>gateway</html>"))
        with self.assertRaisesRegex(HRError, "not JSON") as caught:
            await hr.bases()
        self.assertEqual(caught.exception.body, b"<html>gateway</html>")
        self.assertIn("<html>gateway</html>", caught.exception.excerpt)

    async def test_an_error_carries_status_route_and_a_short_excerpt(self):
        hr = self.client(httpx.Response(502, content=b"upstream  failed\n" + b"x" * 5000))
        with self.assertRaises(HRError) as caught:
            await hr.balance()
        e = caught.exception
        self.assertEqual((e.status, e.route), (502, "GET /v1/balance"))
        self.assertTrue(e.excerpt.startswith("upstream failed x"))
        self.assertLessEqual(len(e.excerpt), 300)
        self.assertLess(len(str(e)), 400)
        self.assertEqual(len(e.body), 5017)


class KeyStaysHidden(ClientCase):
    def assert_clean(self, text: str):
        self.assertNotIn(KEY, text)
        self.assertNotIn(KEY[:16], text)

    async def test_repr(self):
        hr = self.client()
        self.assert_clean(repr(hr))
        self.assert_clean(str(hr))
        self.assertIn(BASE, repr(hr))
        self.assertIn("key=set", repr(hr))
        self.assertIn("key=unset", repr(self.client(api_key="")))

    async def test_error_strings_even_when_the_server_echoes_the_key(self):
        hr = self.client((401, {"error": f"invalid api key {KEY}"}), (403, {"error": f"key {KEY[:16]}... is blocked"}),
                         (200, {"bases": [{"id": 7, "note": KEY}]}))
        for call in (hr.balance, hr.bases, hr.bases):
            with self.assertRaises(HRError) as caught:
                await call()
            e = caught.exception
            for text in (str(e), repr(e), e.excerpt, e.route, e.detail or "", e.body.decode(), "".join(traceback.format_exception(e))):
                self.assert_clean(text)
        self.assertIn("[HR_API_KEY]", str(e))

    async def test_transport_failures(self):
        def handler(request):
            raise httpx.ConnectError(f"Illegal header value b'Bearer {KEY}'", request=request)

        hr = self.client(handler=handler)
        with self.assertRaises(HRError) as caught:
            await hr.bases()
        e = caught.exception
        self.assertIsNone(e.status)
        self.assertEqual(e.route, "GET /v1/bases")
        self.assertIn("ConnectError", str(e))
        self.assert_clean(str(e))
        self.assert_clean("".join(traceback.format_exception(e)))
        self.assertFalse(is_rejected_followup(e))

    async def test_log_lines(self):
        hr = self.client(STORE_DOWN, (200, response("queued")), BUSY, (200, PUT_OK), (503, {"error": "busy"}),
                         (200, response("completed")), (401, {"error": f"invalid api key {KEY}"}))
        with self.assertLogs(level="DEBUG") as logs:
            await hr.send("Reply READY.", harness_id="h_7", model="m", limits=LIMITS, key="k1")
            await hr.put("sess_1", "slotbook/book.py", b"hello")
            await hr.poll("resp_1")
            with self.assertRaises(HRError):
                await hr.balance()
        self.assertGreaterEqual(len([r for r in logs.records if r.name.startswith("rj.hr")]), 3)
        self.assert_clean("\n".join(logs.output))

    async def test_no_key_sends_no_authorization_header(self):
        hr = self.client((200, BASES), api_key="")
        await hr.bases()
        self.assertNotIn("authorization", self.server.requests[0].headers)


class RejectedFollowUp(unittest.TestCase):
    def test_4xx_other_than_409_and_429(self):
        for status in (400, 401, 403, 404, 410, 422):
            self.assertTrue(is_rejected_followup(HRError(status, "POST /v1/responses")), status)
        for status in (409, 429, 500, 502, 503, 200, 302, None):
            self.assertFalse(is_rejected_followup(HRError(status, "POST /v1/responses")), status)
        self.assertFalse(is_rejected_followup(ValueError("400")))
        self.assertTrue(is_rejected_followup(HRError(402, "POST /v1/responses")),
                        "402 is a 4xx here; the runner tests for infra first (section 7's order)")


class Construction(unittest.IsolatedAsyncioTestCase):
    async def test_base_url_and_key_default_to_the_environment(self):
        server = Server((200, BASES))
        with mock.patch.dict(os.environ, {"HR_BASE_URL": "https://env.test/", "HR_API_KEY": f"{KEY}\n"}):
            async with HRClient(transport=httpx.MockTransport(server)) as hr:
                await hr.bases()
                self.assertEqual(hr.base_url, "https://env.test")
                self.assertEqual(hr.edition, "cloud")
                self.assertEqual(hr.poll_seconds, 2.5)
            self.assertTrue(hr._http.is_closed, "leaving the context closes the client")
        self.assertEqual(str(server.requests[0].url), "https://env.test/v1/bases")
        self.assertEqual(server.requests[0].headers["authorization"], f"Bearer {KEY}")

    async def test_arguments_win_over_the_environment(self):
        server = Server((200, BASES))
        with mock.patch.dict(os.environ, {"HR_BASE_URL": "https://env.test", "HR_API_KEY": "env-key"}):
            async with HRClient(BASE, KEY, transport=httpx.MockTransport(server)) as hr:
                await hr.bases()
        self.assertEqual(str(server.requests[0].url), f"{BASE}/v1/bases")
        self.assertEqual(server.requests[0].headers["authorization"], f"Bearer {KEY}")

    def test_a_key_no_header_can_carry_is_refused_unquoted(self):
        for bad in ("hrk-0123456789\x00abcdef", "hrk-0123\nInjected: 1", "hrk-ключ-0123456789"):
            with self.assertRaises(ValueError) as caught:
                HRClient(BASE, bad)
            self.assertNotIn(bad[:8], str(caught.exception))

    def test_no_base_url_anywhere(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "HR_BASE_URL"):
                HRClient()


if __name__ == "__main__":
    unittest.main()
