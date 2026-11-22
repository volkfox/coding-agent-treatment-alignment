"""WJ: the judge asked at Anthropic (`make selftest-blind`). Plan section 9, "Judge providers".

No network and no real key. The judge's client is `tests.judges.FakeClaude`, which answers in the process, or the
installed SDK itself in front of a mock transport (`TheSdk`), so that what the SDK puts on the wire and what it
raises are the real thing. The blinding, the prompts and the merge are held in tests/test_blind.py, which asks
TokenRouter's judge; here the same session is asked of Claude.
"""
import contextlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from rj import cli, schema
from rj.grade import blind, judge
from tests import judges
from tests import test_blind as wj
from tests.judges import CLAUDE_KEY, FakeClaude, claude_message

FOUL = wj.FIXTURE_KEYS[2]
REQUEST = {"model": "claude-opus-5-5", "max_tokens": 32000, "output_config": {"effort": "high"}}
CACHED = {**REQUEST, "cache_control": {"type": "ephemeral"}}       # the scoring call of two runs: the one prompt that is sent again
NEVER_SENT = ("temperature", "top_p", "top_k", "thinking", "response_format", "fallbacks", "tools", "tool_choice", "system", "betas")


def answer(prompt: str):
    """A schema-valid answer to each of the three prompts, as tests/test_blind.py's fake judge gives it."""
    if prompt.startswith("This transcript shows one coding session."):
        return wj.GUESS
    return wj.rated(prompt) if prompt.startswith(wj.FIT_HEAD) else wj.SCORING


class Asked(wj.Sessions):
    """The foul fixture session, the demo profile as profiles.yaml has it, a made-up Anthropic key and a fake client."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {judge.ANTHROPIC_KEY: CLAUDE_KEY})
        env.start()
        self.addCleanup(env.stop)
        for name in ("TR_API_KEY", "TR_BASE_URL", "ANTHROPIC_BASE_URL"):        # the Makefile exports the stub's address
            os.environ.pop(name, None)
        self.profile = schema.load_profile("demo")
        self.session = self.fixture(FOUL)
        (self.session / "judge.json").unlink()                 # the fixture's own

    def ask(self, fake, profile: dict | None = None, **kw) -> dict:
        return judge.judge_session(self.session, profile or self.profile, anthropic_client=fake, **kw)

    def with_judge(self, **keys) -> dict:
        return {**self.profile, "judge": {**self.profile["judge"], **keys}}

    def saved(self) -> dict:
        return json.loads((self.session / "judge.json").read_text(encoding="utf-8"))


class Profile(unittest.TestCase):
    def test_the_demo_profile_asks_claude_opus_5_5_at_anthropic(self):
        block = schema.load_profile("demo")["judge"]
        self.assertEqual(block, {"provider": "anthropic", "model": "claude-opus-5.5", "effort": "high", "runs": 2, "guess_call": True,
                                 "max_input_chars": 600000}, "one judge model: no fallback model, and no path of TokenRouter's")
        self.assertEqual((judge.judge_route(block), judge.judge_path(block)), (("anthropic", "high"), "/v1/messages"))
        self.assertEqual(judge.judge_model(block), ("claude-opus-5.5", {"slug": "claude-opus-5-5", "input": 4, "cache_read": 0.2,
                                                                        "cache_write": 5, "output": 20}))
        listed = schema.load_prices()["judge"]
        self.assertEqual((listed["gemini-3-flash-preview"]["slug"], listed["gemini-3.7-flash"]["slug"]),
                         ("google/gemini-3-flash-preview", "google/gemini-3.7-flash"), "the entries of TokenRouter's judges are kept")

    def test_the_provider_defaults_to_tokenrouter_and_the_stub_wins_over_it(self):
        self.assertEqual(judge.judge_route(judges.TR_JUDGE), ("tokenrouter", None))
        self.assertEqual(judge.judge_route({**judges.TR_JUDGE, "effort": "max"}), ("tokenrouter", None), "effort is the Anthropic judge's alone")
        self.assertEqual(judge.judge_route({"provider": "anthropic", "model": "claude-opus-5.5"}), ("anthropic", "high"), "its default")
        self.assertEqual(judge.judge_route({"provider": "anthropic", "effort": "low"}), ("anthropic", "low"))
        offline = schema.load_profile("offline")["judge"]
        self.assertEqual((offline["provider"], offline["stub"]), ("anthropic", True), "inherited from demo, with stub: true beside it")
        self.assertEqual((judge.judge_route(offline), judge.judge_path(offline)), (("tokenrouter", None), "/chat/completions"))
        with self.assertRaises(judge.JudgeError) as caught:
            judge.judge_route({"provider": "openai"})
        self.assertIn("judge.provider 'openai'", str(caught.exception))


@judges.NEEDS_SDK
class Request(Asked):
    def test_the_request_and_judge_json(self):
        fake = FakeClaude(answer)
        out = self.ask(fake)
        self.assertEqual(len(fake.requests), 4, "two scoring calls, the guess, then the fit call")
        transcript, shown = (self.session / "blind.txt").read_text(encoding="utf-8"), blind.replies(self.session)
        prompts = [judge.scoring_prompt(transcript)] * 2 + [judge.guess_prompt(transcript), judge.fit_prompt(shown.text)]
        for request, prompt, again in zip(fake.requests, prompts, (True, True, False, False)):
            self.assertEqual(request, {**(CACHED if again else REQUEST), "messages": [{"role": "user", "content": prompt}]},
                             "the model's id, the output limit, the effort, one user message; the cache mark on the scoring call, "
                             "which the second run repeats, and not on the guess and the fit call, which nothing reads back; nothing else")
            for name in NEVER_SENT:
                self.assertNotIn(name, request, "this model refuses the sampling parameters and always thinks; one judge model, so no fallback")
            self.assertNotIn("fx-row", prompt)
            self.assertEqual(blind.leak_check(prompt), [])
        self.assertEqual(fake.requests[0], fake.requests[1], "the second scoring run repeats the first, so it can be read from the cache")
        self.assertFalse(fake.closed, "a client that was handed in is its owner's to close")
        saved = self.saved()
        self.assertEqual(saved, out)
        schema.validate("JUDGE", saved)
        self.assertEqual((saved["model"], saved["slug"], saved["path"], saved["provider"], saved["effort"], saved["prompt_sha"]),
                         ("claude-opus-5.5", "claude-opus-5-5", "/v1/messages", "anthropic", "high", judge.PROMPT_SHA))
        self.assertEqual((saved["runs"], saved["guess"], saved["asked"]), ([wj.SCORING, wj.SCORING], wj.GUESS, ["scoring", "scoring", "guess", "fit"]))
        self.assertEqual([(u["call"], u["attempts"]) for u in saved["usage"]], [("scoring", 1)] * 2 + [("guess", 1), ("fit", 1)])
        self.assertEqual(saved["fit"], {
            "messages": [{"ep": ep, "send": 0, "level": 2, "unanswered": 1, "why": "synthetic"} for ep in wj.EPS[1:]],
            "prompt_sha": judge.FIT_PROMPT_SHA, "input_sha": schema.sha256_text(shown.text), "unseen": shown.unseen})
        self.assertEqual(saved["unseen"], blind.blind(self.session).unseen)
        self.assertNotIn(CLAUDE_KEY[:16], json.dumps(saved))

    def test_both_judges_are_sent_the_same_prompts(self):
        claude = FakeClaude(answer)
        self.ask(claude)
        chat = wj.FakeJudge()
        with mock.patch.dict(os.environ, {"TR_API_KEY": wj.API_KEY}), chat.client() as client:
            judge.judge_session(self.session, judges.on_tokenrouter(self.profile), client=client)
        self.assertEqual(claude.prompts(), chat.prompts(), "so the two judges read the same thing")

    def test_the_effort_and_the_output_limit_come_from_the_profile(self):
        fake = FakeClaude(answer)
        out = self.ask(fake, self.with_judge(effort="medium", max_tokens=9000, runs=1, guess_call=False))
        self.assertEqual([{k: v for k, v in r.items() if k != "messages"} for r in fake.requests],
                         [{**REQUEST, "max_tokens": 9000, "output_config": {"effort": "medium"}}] * 2,
                         "one scoring run, the fit call: no prompt is sent again, so none is written to the cache")
        self.assertEqual((out["effort"], out["asked"], out["guess"]), ("medium", ["scoring", "fit"], None))

    def test_a_reply_that_opens_with_thinking_blocks_is_read_by_its_text(self):
        fenced = "Here is the result:\n```json\n" + json.dumps(wj.SCORING, indent=2) + "\n```"
        split = claude_message(json.dumps(wj.SCORING)[:40], thinking=2)
        split.content.append(claude_message(json.dumps(wj.SCORING)[40:]).content[-1])      # a second text block: the two are joined
        self.assertEqual([b.type for b in split.content], ["thinking", "thinking", "text", "text"])
        out = self.ask(FakeClaude(answer, first=[claude_message(fenced, thinking=3), split]))
        self.assertEqual((out["runs"], [u["attempts"] for u in out["usage"]]), ([wj.SCORING, wj.SCORING], [1, 1, 1, 1]))

    def test_a_malformed_reply_is_asked_for_once_more(self):
        fake = FakeClaude(answer, first=[claude_message("I cannot produce JSON today.")])
        out = self.ask(fake)
        self.assertEqual(len(fake.requests), 5)
        self.assertEqual((out["runs"], [u["attempts"] for u in out["usage"]]), ([wj.SCORING, wj.SCORING], [2, 1, 1, 1]))
        self.assertEqual(fake.requests[0], fake.requests[1], "asked for again with the very same request")
        self.assertEqual((out["usage"][0]["prompt_tokens"], out["usage"][0]["completion_tokens"]), (2 * 25, 2 * 100), "both attempts were paid for")
        self.assertAlmostEqual(out["usage"][0]["cost_usd"], 2 * (25 * 4 + 100 * 20) / 1e6, places=12)
        twice = FakeClaude(answer, first=[claude_message(None), claude_message(f'{{"session": "{CLAUDE_KEY}"}}')])     # no text at all, then the wrong shape
        with self.assertRaises(judge.JudgeError) as caught:
            self.ask(twice)
        self.assertIn("malformed twice", str(caught.exception))
        self.assertNotIn(CLAUDE_KEY[:16], str(caught.exception))
        self.assertEqual(len(twice.requests), 2)

    def test_a_refusal_fails_the_session_and_no_other_model_is_asked(self):
        fake = FakeClaude(answer, first=[claude_message(None, thinking=0, stop_reason="refusal", category="cyber")])
        with self.assertRaises(judge.JudgeError) as caught:
            self.ask(fake)
        self.assertNotIsInstance(caught.exception, judge.FitNotJudged)
        self.assertIn("stop_reason refusal, category cyber", str(caught.exception))
        self.assertIn("no other model is asked", str(caught.exception))
        self.assertEqual(len(fake.requests), 1, "not asked again, of this model or of another")
        self.assertFalse((self.session / "judge.json").exists())
        bare = claude_message(json.dumps(wj.SCORING), stop_reason="refusal")          # no category, and a text that must not be read
        with self.assertRaises(judge.JudgeError) as caught:
            self.ask(FakeClaude(answer, first=[bare]))
        self.assertIn("category not given", str(caught.exception))
        # declined at the fit call: the scores that were paid for are written, and the fit call is asked again alone
        late = FakeClaude(lambda prompt: claude_message(None, stop_reason="refusal", category="bio") if prompt.startswith(wj.FIT_HEAD) else answer(prompt))
        with self.assertRaises(judge.FitNotJudged) as caught:
            self.ask(late)
        self.assertIn("the fit call: the judge declined the prompt (stop_reason refusal, category bio)", str(caught.exception))
        self.assertEqual((len(late.requests), self.saved()["asked"], "fit" in self.saved()), (4, ["scoring", "scoring", "guess"], False))
        again = FakeClaude(answer)
        self.assertEqual((self.ask(again, reuse=True)["asked"], len(again.requests)), (["fit"], 1))

    def test_a_reply_cut_at_max_tokens_is_not_read_as_malformed(self):
        for reason in ("max_tokens", "model_context_window_exceeded"):
            fake = FakeClaude(answer, first=[claude_message(json.dumps(wj.SCORING)[:200], stop_reason=reason)])
            with self.assertRaises(judge.JudgeError) as caught:
                self.ask(fake)
            self.assertIn(f"cut short (stop_reason {reason}, max_tokens 32000)", str(caught.exception))
            self.assertIn("raise judge.max_tokens or lower judge.effort", str(caught.exception))
            self.assertNotIn("malformed", str(caught.exception))
            self.assertEqual(len(fake.requests), 1, "asking again would be cut again")
        whole = FakeClaude(answer, first=[claude_message(json.dumps(wj.SCORING), stop_reason="max_tokens")])
        with self.assertRaises(judge.JudgeError):
            self.ask(whole)                                    # even where the cut reply happens to parse: its end is not known
        self.assertFalse((self.session / "judge.json").exists())

    def test_a_reply_whose_stream_ended_before_the_model_ended_it_is_not_read(self):
        """The SDK hands over what had arrived, with no stop reason: its text may be half a reply and its usage is not the bill."""
        for reason in (None, "pause_turn"):
            fake = FakeClaude(answer, first=[claude_message(json.dumps(wj.SCORING), stop_reason=reason)])      # even where it parses
            with self.assertRaises(judge.JudgeError) as caught:
                self.ask(fake)
            self.assertIn(f"did not end as a finished one (stop_reason {reason}): it is not read", str(caught.exception))
            self.assertNotIn("malformed", str(caught.exception))
            self.assertEqual(len(fake.requests), 1, "not asked again in the same pass")
            self.assertFalse((self.session / "judge.json").exists())

    def test_without_the_key_nothing_is_sent(self):
        fake = FakeClaude(answer)
        with mock.patch.dict(os.environ, {judge.ANTHROPIC_KEY: "  ", "TR_API_KEY": wj.API_KEY}):
            with self.assertRaises(judge.JudgeError) as caught:
                self.ask(fake)
            self.assertEqual(str(caught.exception), "ANTHROPIC_API_KEY is not set", "TokenRouter's key is not this judge's")
            with self.assertRaises(judge.JudgeError):
                judge.sdk_client()
        self.assertEqual(fake.requests, [])
        self.assertFalse((self.session / "judge.json").exists())

    def test_usage_and_cost_count_the_cache(self):
        """What the API reports apart: tokens not cached, tokens written to the cache, tokens read from it."""
        used = [{"input_tokens": 30, "cache_creation_input_tokens": 40_000, "cache_read_input_tokens": 0, "output_tokens": 2_000},
                {"input_tokens": 30, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 40_000, "output_tokens": 1_800},
                {"input_tokens": 41_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 300},
                {"input_tokens": 12, "cache_creation_input_tokens": 900, "cache_read_input_tokens": 0, "output_tokens": 500}]
        replies = iter(used)
        fake = FakeClaude(lambda prompt: claude_message(json.dumps(answer(prompt)), usage=next(replies)))
        out = schema.validate("JUDGE", self.ask(fake))
        self.assertEqual([(u["prompt_tokens"], u["completion_tokens"], u["cache_read_tokens"], u["cache_write_tokens"]) for u in out["usage"]],
                         [(40_030, 2_000, 0, 40_000), (40_030, 1_800, 40_000, 0), (41_000, 300, 0, 0), (912, 500, 0, 900)],
                         "prompt_tokens is everything that went in; the two cache counts stand beside it")
        want = [(30 * 4 + 40_000 * 5 + 2_000 * 20) / 1e6, (30 * 4 + 40_000 * 0.2 + 1_800 * 20) / 1e6, (41_000 * 4 + 300 * 20) / 1e6,
                (12 * 4 + 900 * 5 + 500 * 20) / 1e6]
        for u, cost in zip(out["usage"], want):
            self.assertAlmostEqual(u["cost_usd"], cost, places=12)
        self.assertLess(out["usage"][1]["cost_usd"], out["usage"][0]["cost_usd"] / 5, "the run read from the cache is the cheap one")
        self.assertEqual(judge._claude_usage(claude_message("x", usage={"cache_creation_input_tokens": None, "cache_read_input_tokens": None}).usage,
                                             judge.pricing.Price(4, 0.2, 5, 20)),
                         {"prompt_tokens": 25, "completion_tokens": 100, "cost_usd": (25 * 4 + 100 * 20) / 1e6, "cache_read_tokens": 0,
                          "cache_write_tokens": 0}, "a count the API left out is none")


class StubAndTokenRouter(Asked):
    def test_stub_true_wins_over_the_provider(self):
        offline = schema.load_profile("offline")
        claude, chat = FakeClaude(answer), wj.FakeJudge()
        with mock.patch.dict(os.environ, {"TR_BASE_URL": "http://127.0.0.1:8089/tr"}), chat.client() as client:
            os.environ.pop(judge.ANTHROPIC_KEY)                # the stub's judge needs no key of anyone's
            out = judge.judge_session(self.session, offline, client=client, anthropic_client=claude)
        self.assertEqual(claude.requests, [], "Anthropic is not asked")
        self.assertEqual([str(r.url) for r in chat.requests], ["http://127.0.0.1:8089/tr/chat/completions"] * 4)
        self.assertEqual({r.headers["authorization"] for r in chat.requests}, {"Bearer stub"})
        self.assertEqual({json.loads(r.content)["temperature"] for r in chat.requests}, {0}, "the chat call, as it always was")
        self.assertEqual((out["model"], out["path"], [u["cost_usd"] for u in out["usage"]]), ("stub-judge", "/chat/completions", [0.0] * 4))
        self.assertTrue({"provider", "effort"}.isdisjoint(out), "its judge.json is the chat judge's")
        self.assertTrue(judge.reusable(self.session, offline))

    def test_tokenrouter_s_judge_writes_no_new_key(self):
        chat, profile = wj.FakeJudge(), judges.on_tokenrouter(self.profile)
        with mock.patch.dict(os.environ, {"TR_API_KEY": wj.API_KEY}), chat.client() as client:
            out = judge.judge_session(self.session, profile, client=client, anthropic_client=FakeClaude(answer))
        self.assertEqual(len(chat.requests), 4)
        self.assertEqual(sorted(out), sorted(["runs", "guess", "usage", "asked", "model", "path", "prompt_sha", "slug", "opaque_id", "blind_sha",
                                              "redactions", "keep_lines", "unseen", "fit"]))
        self.assertEqual([sorted(u) for u in out["usage"]], [sorted(["prompt_tokens", "completion_tokens", "cost_usd", "call", "attempts"])] * 4)
        self.assertEqual(judge.judged_by(out), ("tokenrouter", None))

    def test_judge_json_files_written_before_still_validate(self):
        for key in wj.FIXTURE_KEYS:
            old = schema.validate("JUDGE", wj.fixture_judge(key))
            self.assertEqual(judge.judged_by(old), ("tokenrouter", None), "a file that names no provider was judged on TokenRouter")
        first = {"runs": [wj.SCORING, wj.SCORING], "guess": wj.GUESS, "model": "gemini-3-flash-preview", "path": "/chat/completions",
                 "usage": [{"prompt_tokens": 1000, "completion_tokens": 100, "cost_usd": 0.0008}] * 3, "prompt_sha": judge.PROMPT_SHA}
        schema.validate("JUDGE", first)                        # as the first paid sessions were written: no fit, no asked, no unseen
        for bad in ({**first, "provider": 7}, {**first, "usage": [{**first["usage"][0], "cache_read_tokens": "many"}]}):
            with self.assertRaises(schema.SchemaError):
                schema.validate("JUDGE", bad)


@judges.NEEDS_SDK
class Reuse(Asked):
    def test_a_judge_json_is_kept_only_for_the_judge_that_wrote_it(self):
        out = self.ask(FakeClaude(answer))
        again = FakeClaude(answer)
        self.assertEqual((self.ask(again, reuse=True), again.requests), (out, []), "same transcript, prompts, model, provider and effort")
        self.assertTrue(judge.reusable(self.session, self.profile))
        # another effort
        lower = self.with_judge(effort="medium")
        self.assertFalse(judge.reusable(self.session, lower))
        fake = FakeClaude(answer)
        moved = self.ask(fake, lower, reuse=True)
        self.assertEqual((len(fake.requests), moved["asked"], moved["effort"]), (4, ["scoring", "scoring", "guess", "fit"], "medium"))
        self.assertFalse(judge.reusable(self.session, self.profile), "and what the lower effort wrote is not the higher one's")
        # another provider
        on_tr, chat = judges.on_tokenrouter(self.profile), wj.FakeJudge()
        self.assertFalse(judge.reusable(self.session, on_tr))
        with mock.patch.dict(os.environ, {"TR_API_KEY": wj.API_KEY}), chat.client() as client:
            routed = judge.judge_session(self.session, on_tr, client=client, reuse=True)
        self.assertEqual((len(chat.requests), routed["model"], "provider" in routed), (4, "gemini-3-flash-preview", False))
        self.assertTrue(judge.reusable(self.session, on_tr))
        self.assertFalse(judge.reusable(self.session, self.profile))
        fake = FakeClaude(answer)
        back = self.ask(fake, reuse=True)
        self.assertEqual((len(fake.requests), back["model"], back["provider"]), (4, "claude-opus-5.5", "anthropic"),
                         "a session judged by TokenRouter's model is asked again whole")
        # the same model name under another provider: a file that names none was judged on TokenRouter
        unnamed = {k: v for k, v in back.items() if k not in ("provider", "effort")}
        (self.session / "judge.json").write_text(json.dumps(schema.validate("JUDGE", unnamed)), encoding="utf-8")
        self.assertFalse(judge.reusable(self.session, self.profile))
        fake = FakeClaude(answer)
        self.assertEqual((self.ask(fake, reuse=True)["asked"], len(fake.requests)), (["scoring", "scoring", "guess", "fit"], 4),
                         "scores and fit: neither is kept")
        # another model of the same provider
        other = self.with_judge(model="gemini-3.7-flash")
        self.assertFalse(judge.reusable(self.session, other))
        fake = FakeClaude(answer)
        self.assertEqual((self.ask(fake, other, reuse=True)["model"], len(fake.requests)), ("gemini-3.7-flash", 4))


# --------------------------------------------------------------------------- the installed SDK, over a mock transport

def sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode("utf-8")


def opening(usage: dict) -> dict:
    return {"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                                                 "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}}


def stream_of(text: str, *, stop_reason: str = "end_turn", details: dict | None = None, usage: dict | None = None,
              ends: bool = True) -> bytes:
    """The event stream of one reply, as the Messages API sends it: a thinking block with no text, then the text in two pieces.
    `ends` False: the stream stops after the text, with neither the stop reason nor the closing event."""
    used = {**judges.USAGE, **(usage or {})}
    return sse(events_of(text, stop_reason, details, used)[:None if ends else -2])


def events_of(text: str, stop_reason: str, details: dict | None, used: dict) -> list[dict]:
    return [
        opening({**used, "output_tokens": 1}),
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "ping"},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text[:len(text) // 2]}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text[len(text) // 2:]}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None, **({"stop_details": details} if details else {})},
         "usage": {"output_tokens": used["output_tokens"]}},
        {"type": "message_stop"},
    ]


@judges.NEEDS_SDK
class TheSdk(Asked):
    """`anthropic.Anthropic` itself, with a transport that answers in the process: nothing leaves this machine."""

    def setUp(self):
        super().setUp()
        import anthropic
        import httpx2                                          # the HTTP library of the SDK (anthropic 2.x), for its mock transport
        self.sdk, self.http, self.requests, self.first = anthropic, httpx2, [], []
        self.client = anthropic.Anthropic(api_key=CLAUDE_KEY, max_retries=0, timeout=5.0,
                                          http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self.reply)))
        self.addCleanup(self.client.close)

    def events(self, body: bytes):
        return self.http.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    def reply(self, request):
        self.requests.append(request)
        if self.first:
            scripted = self.first.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            return scripted
        body = json.loads(request.content)
        return self.events(stream_of(json.dumps(answer(body["messages"][0]["content"])),
                                     usage={"cache_creation_input_tokens": 1000} if "cache_control" in body else {}))

    def refused(self, status: int, kind: str):
        return self.http.Response(status, json={"type": "error", "error": {"type": kind, "message": f"no: the key {CLAUDE_KEY} said so"},
                                                "request_id": "req_test"})

    def test_what_the_sdk_puts_on_the_wire_and_reads_back(self):
        out = schema.validate("JUDGE", self.ask(self.client))
        self.assertEqual(len(self.requests), 4)
        transcript = (self.session / "blind.txt").read_text(encoding="utf-8")
        for request, again in zip(self.requests, (True, True, False, False)):
            self.assertEqual((request.method, str(request.url)), ("POST", "https://api.anthropic.com/v1/messages"))
            self.assertIn(CLAUDE_KEY, request.headers.get("x-api-key", "") + request.headers.get("authorization", ""), "the key that was handed over")
            body = json.loads(request.content)
            self.assertEqual({k: v for k, v in body.items() if k != "messages"}, {**(CACHED if again else REQUEST), "stream": True},
                             "the effort where the API reads it, the cache mark on the two scoring runs alone, streamed, and nothing else")
            self.assertEqual([m["role"] for m in body["messages"]], ["user"])
        self.assertEqual(json.loads(self.requests[0].content)["messages"][0]["content"], judge.scoring_prompt(transcript))
        self.assertEqual((out["runs"], out["guess"], len(out["fit"]["messages"])), ([wj.SCORING, wj.SCORING], wj.GUESS, 5),
                         "the text is read past the thinking block")
        self.assertEqual([(u["prompt_tokens"], u["cache_write_tokens"], u["cache_read_tokens"], u["completion_tokens"]) for u in out["usage"]],
                         [(1025, 1000, 0, 100)] * 2 + [(25, 0, 0, 100)] * 2, "the usage of the finished message: where it opened and where it ended")
        self.assertAlmostEqual(out["usage"][0]["cost_usd"], (25 * 4 + 1000 * 5 + 100 * 20) / 1e6, places=12)

    def test_the_client_the_judge_makes_for_itself(self):
        made = judge.sdk_client()
        self.addCleanup(made.close)
        self.assertIsInstance(made, self.sdk.Anthropic)
        self.assertEqual((made.api_key, made.max_retries, made.timeout, str(made.base_url).rstrip("/")),
                         (CLAUDE_KEY, 5, judge.TIMEOUT, "https://api.anthropic.com"))
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9/claude"}):
            elsewhere = judge.sdk_client()
            self.addCleanup(elsewhere.close)
        self.assertEqual(str(elsewhere.base_url).rstrip("/"), "http://127.0.0.1:9/claude", "the SDK reads ANTHROPIC_BASE_URL by itself")
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": " "}):       # as a line left empty in .env sets it
            with self.assertRaises(judge.JudgeError) as caught:
                self.ask(None)
        self.assertIn("ANTHROPIC_BASE_URL is set but empty", str(caught.exception))
        self.assertFalse((self.session / "judge.json").exists())

    def test_a_refusal_as_the_api_sends_it(self):
        self.first = [self.events(stream_of("", stop_reason="refusal", details={"type": "refusal", "category": "cyber", "explanation": "x"}))]
        with self.assertRaises(judge.JudgeError) as caught:
            self.ask(self.client)
        self.assertIn("stop_reason refusal, category cyber", str(caught.exception))
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("fallbacks", json.loads(self.requests[0].content))

    def test_a_stream_that_ends_without_a_stop_reason_is_not_a_reply(self):
        """A stream that closes cleanly after the text, as a gateway that cuts long replies would send it: the SDK hands
        over what had arrived, with no stop reason and the output count the stream opened with."""
        self.first = [self.events(stream_of(json.dumps(wj.SCORING), ends=False))]
        with self.assertRaises(judge.JudgeError) as caught:
            self.ask(self.client)
        self.assertIn("did not end as a finished one (stop_reason None)", str(caught.exception), "the whole JSON had arrived, and still")
        self.assertEqual(len(self.requests), 1)
        self.assertFalse((self.session / "judge.json").exists())

    def test_the_sdk_s_errors_become_judge_errors_that_carry_no_key(self):
        records_seen = []

        class Keep(logging.Handler):
            def emit(self, record):
                records_seen.append(record.getMessage() + repr(record.args))
        handler, root = Keep(level=logging.DEBUG), logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(level)))

        broken = self.http.Response(200, headers={"content-type": "text/event-stream"}, content=self.breaks_off())
        late = self.events(sse([opening(judges.USAGE)]) + b'event: error\ndata: {"type": "error", "error": {"type": "overloaded_error", '
                           b'"message": "Overloaded"}}\n\n')
        cases = [
            (self.refused(401, "authentication_error"), "judge call refused: HTTP 401 authentication_error: no: the key [ANTHROPIC_API_KEY] said so; "
                                                        "check ANTHROPIC_API_KEY"),
            (self.refused(403, "permission_error"), "judge call refused: HTTP 403 permission_error: "),
            (self.refused(404, "not_found_error"), "is model claude-opus-5-5 available to this key?"),
            (self.refused(400, "invalid_request_error"), "judge call refused: HTTP 400 invalid_request_error: "),
            (self.refused(402, "billing_error"), "judge call refused: HTTP 402 billing_error: "),
            (self.refused(429, "rate_limit_error"), "judge call failed: HTTP 429 rate_limit_error: "),
            (self.refused(500, "api_error"), "judge call failed: HTTP 500 api_error: "),
            (self.refused(529, "overloaded_error"), "judge call failed: HTTP 529 overloaded_error: "),
            (late, "judge call failed: HTTP 200 overloaded_error: Overloaded"),
            (self.http.ConnectError("connection refused"), "judge call failed: no reply (APIConnectionError)"),
            (self.http.ReadTimeout("too slow"), "judge call failed: no reply (APITimeoutError)"),
            (broken, "judge call failed: the reply broke off (ReadError)"),
        ]
        for scripted, said in cases:
            self.first, sent = [scripted], len(self.requests)
            with self.assertRaises(judge.JudgeError) as caught:
                self.ask(self.client)
            error = caught.exception
            self.assertIn(said, str(error))
            self.assertEqual(len(self.requests) - sent, 1, f"{said}: asked once; the retries are the SDK's, and this client has none")
            for shown in [f"{e!s} {e!r} {e.args!r}" for e in (error, error.__cause__, error.__context__) if e is not None]:
                self.assertNotIn(CLAUDE_KEY[:16], shown)
            self.assertIsNone(error.__context__, "raised outside the handler: no request, and so no header, travels with it")
            self.assertFalse((self.session / "judge.json").exists())
        self.assertTrue(records_seen, "the SDK logs each request")
        self.assertNotIn(CLAUDE_KEY[:16], "\n".join(records_seen))

    def breaks_off(self):
        yield sse([opening(judges.USAGE)])
        raise self.http.ReadError("the peer closed the connection")

    def test_the_client_rj_judge_hands_over_counts_every_call_and_costs_every_reply_that_ended(self):
        """`rj judge` ledgers what the calls cost that left no judge.json: by their usage where the reply came to its
        end, a declined and a cut one among them, and as an estimate where it broke off. It counts them here."""
        calls = {"made": 0, "read": 0, "usd": 0.0}
        counted = cli.CountedClaude(calls, judge.pricing.price_from_yaml(judge.judge_model(self.profile["judge"])[1]))
        with mock.patch.object(judge, "sdk_client", return_value=self.client) as made:
            out = self.ask(counted)
            self.assertEqual((calls["made"], calls["read"], len(self.requests), made.call_count, out["asked"]),
                             (4, 4, 4, 1, ["scoring", "scoring", "guess", "fit"]), "the SDK's client is made once, when the first call is sent")
            self.assertAlmostEqual(calls["usd"], sum(u["cost_usd"] for u in out["usage"]), places=12, msg="each reply by its own usage, as judge.json has it")
            declined = self.events(stream_of("", stop_reason="refusal", details={"type": "refusal", "category": "bio", "explanation": None},
                                             usage={"output_tokens": 7}))
            cut = self.events(stream_of(json.dumps(wj.SCORING)[:200], stop_reason="max_tokens",
                                        usage={"cache_creation_input_tokens": 50_000, "output_tokens": 32_000}))
            broken = self.http.Response(200, headers={"content-type": "text/event-stream"}, content=self.breaks_off())
            early = self.events(stream_of(json.dumps(wj.SCORING), ends=False))
            was = calls["usd"]
            for scripted, said, counts, cost in (
                    (declined, "stop_reason refusal, category bio", (5, 5), (25 * 4 + 7 * 20) / 1e6),
                    (cut, "cut short (stop_reason max_tokens", (6, 6), (25 * 4 + 50_000 * 5 + 32_000 * 20) / 1e6),
                    (self.refused(401, "authentication_error"), "HTTP 401", (6, 6), 0.0),
                    (broken, "the reply broke off (ReadError)", (7, 6), 0.0),
                    (early, "stop_reason None", (8, 6), 0.0)):
                self.first = [scripted]
                with self.assertRaises(judge.JudgeError) as caught:
                    self.ask(counted)
                self.assertIn(said, str(caught.exception), "said as it is with the SDK's client alone")
                self.assertEqual((calls["made"], calls["read"]), counts,
                                 f"{said}: a reply that began was made; one that ended with a stop reason was read; a refused call is neither")
                self.assertAlmostEqual(calls["usd"] - was, cost, places=12, msg=f"{said}: billed in full where the reply ended, not known where it did not")
                was = calls["usd"]
        self.assertFalse(self.client.is_closed())
        counted.close()
        self.assertTrue(self.client.is_closed(), "the client it made is its own to close")
        idle = cli.CountedClaude(calls, None)
        idle.close()                                           # nothing was asked: there is no client to close
        self.assertIsNone(idle.sdk)


class Package(Asked):
    def test_the_package_is_imported_only_when_the_anthropic_judge_is_asked(self):
        loaded = subprocess.run([sys.executable, "-c", "import sys, rj.cli, rj.preflight, rj.grade.judge; print('anthropic' in sys.modules)"],
                                cwd=schema.ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(loaded.stdout.strip(), "False", "the command line, preflight and the judge load without it")
        with mock.patch.dict(sys.modules, {"anthropic": None}):                 # as on a machine where it is not installed
            with self.assertRaises(judge.JudgeError) as caught:
                self.ask(None)
            self.assertIn("needs the `anthropic` package", str(caught.exception))
            self.assertIn("uv pip install --python .venv/bin/python anthropic", str(caught.exception))
            chat = wj.FakeJudge()
            with mock.patch.dict(os.environ, {"TR_API_KEY": wj.API_KEY}), chat.client() as client:
                out = judge.judge_session(self.session, judges.on_tokenrouter(self.profile), client=client)
            self.assertEqual((len(chat.requests), out["model"]), (4, "gemini-3-flash-preview"), "TokenRouter's judge needs none of it")


# --------------------------------------------------------------------------- python -m rj.grade.judge

@judges.NEEDS_SDK
class CommandLine(wj.Sessions):
    def setUp(self):
        super().setUp()
        self.run_dir = self.tmp / "demo"                       # named after its profile, as `rj run` names it
        shutil.copytree(wj.RUN, self.run_dir)
        env = mock.patch.dict(os.environ, {judge.ANTHROPIC_KEY: CLAUDE_KEY})
        env.start()
        self.addCleanup(env.stop)

    def main(self, fake, *argv: str, written=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = judge.main([str(self.run_dir), *argv], anthropic_client=fake, written=written)
        return code, out.getvalue(), err.getvalue()

    def rating(self, key: str):
        return schema.validate("GRADE", json.loads((self.run_dir / "sessions" / key / "grade.json").read_text(encoding="utf-8")))["effort"]["judge"]

    def declines(self, key: str):
        """A fake Claude that declines every prompt of one session and answers the others."""
        return FakeClaude(lambda prompt: claude_message(None, stop_reason="refusal", category="cyber") if f"SESSION {blind.opaque_id(key)}\n" in prompt
                          else answer(prompt))

    def test_a_run_judged_by_the_stub_is_asked_again_of_claude_and_merged(self):
        fake = FakeClaude(answer)
        code, out, err = self.main(fake)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("judge: profile demo, 3 session(s), model claude-opus-5.5, asked at Anthropic with effort high", out)
        self.assertEqual(len(fake.requests), 12, "the fixture's judge.json files are the stub's: every session is asked again, four calls each")
        for key in wj.FIXTURE_KEYS:
            session = self.run_dir / "sessions" / key
            saved = schema.validate("JUDGE", json.loads((session / "judge.json").read_text(encoding="utf-8")))
            self.assertEqual((saved["model"], saved["provider"], saved["effort"]), ("claude-opus-5.5", "anthropic", "high"))
            grade = schema.validate("GRADE", json.loads((session / "grade.json").read_text(encoding="utf-8")))
            self.assertEqual((grade["effort"]["judge"]["model"], grade["effort"]["judge"]["prompt_sha"]), ("claude-opus-5.5", judge.PROMPT_SHA))
        self.assertNotIn(CLAUDE_KEY[:16], out + err)
        again = FakeClaude(answer)
        self.assertEqual((self.main(again)[0], again.requests), (0, []), "a second pass sends nothing")
        refusing = self.declines(FOUL)
        code, out, err = self.main(refusing, "--force")
        self.assertEqual(code, 1)
        self.assertIn(f"{FOUL}: FAILED: the judge declined the prompt (stop_reason refusal, category cyber); no other model is asked", err)
        self.assertEqual(len(refusing.requests), 9, "the session that was declined is asked once and not again; the others are judged")
        self.assertEqual(self.rating(FOUL)["model"], "claude-opus-5.5", "the rating this judge gave it before stands")
        self.assertNotIn("taken out of grade.json", err)

    def test_a_session_this_judge_fails_on_keeps_no_rating_of_another_judge(self):
        """The fixture run was rated by the stub's judge. Claude declines one session: asking again will not cure that, and
        the other judge's rating must not stand beside Claude's in the roll-up."""
        self.assertEqual({self.rating(key)["model"] for key in wj.FIXTURE_KEYS}, {"stub-judge"})
        before, told = (self.run_dir / "sessions" / FOUL / "judge.json").read_bytes(), []
        refusing = self.declines(FOUL)
        code, out, err = self.main(refusing, written=lambda path: told.append((path, len(refusing.requests))))
        self.assertEqual(code, 1)
        self.assertIn(f"{FOUL}: FAILED: the judge declined the prompt (stop_reason refusal, category cyber)", err)
        self.assertIn(f"{FOUL}: the rating stub-judge gave it is taken out of grade.json: that is not the judge the profile asks now", err)
        self.assertIsNone(self.rating(FOUL), "one judge rates every session")
        self.assertEqual((self.run_dir / "sessions" / FOUL / "judge.json").read_bytes(), before, "the other judge's own file is left as it is")
        rest = [key for key in wj.FIXTURE_KEYS if key != FOUL]
        self.assertEqual([self.rating(key)["model"] for key in rest], ["claude-opus-5.5"] * 2)
        self.assertEqual([path for path, _ in told], [self.run_dir / "sessions" / key / "judge.json" for key in rest],
                         "each judge.json is named as it is written; the session that failed wrote none")
        asked = [n for _, n in told]
        self.assertEqual((len(asked), asked == sorted(asked), asked[-1] <= len(refusing.requests), asked[1] - asked[0] in (4, 5)), (2, True, True, True),
                         "and before the next session is asked, so that what a session cost can be ledgered there and then")
        code, out, err = self.main(self.declines(FOUL))
        self.assertEqual((code, self.rating(FOUL)), (1, None))
        self.assertNotIn("taken out of grade.json", err, "nothing is left to take out")
        # the same model at another effort is another judge too: judge.json says who was asked
        self.assertEqual(self.main(FakeClaude(answer))[0], 0)
        self.assertEqual(self.rating(FOUL)["model"], "claude-opus-5.5")
        real = schema.load_profile("demo")
        with mock.patch.object(judge.schema, "load_profile", return_value={**real, "judge": {**real["judge"], "effort": "medium"}}):
            code, out, err = self.main(self.declines(FOUL))
        self.assertEqual((code, self.rating(FOUL)), (1, None))
        self.assertIn(f"{FOUL}: the rating claude-opus-5.5 gave it is taken out of grade.json", err)

    def test_a_provider_the_judge_does_not_know_stops_the_command(self):
        real = schema.load_profile("demo")
        with mock.patch.object(judge.schema, "load_profile", return_value={**real, "judge": {**real["judge"], "provider": "openai"}}):
            code, out, err = self.main(FakeClaude(answer))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("error: judge.provider 'openai' is neither 'tokenrouter' nor 'anthropic'", err)


if __name__ == "__main__":
    unittest.main()
