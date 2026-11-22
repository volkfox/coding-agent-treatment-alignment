"""What the tests of the judge share: the judge block of TokenRouter's judge, and a stand-in for the Anthropic SDK's client.

No test asks a real service. The demo profile's judge is Claude Opus 5.5 on the Anthropic API; `FakeClaude` answers its
`client.messages.stream(...)` in the process and keeps every request. tests/test_judge.py also puts the SDK itself in
front of a mock transport, so what the installed SDK sends and reads is held too.

The replies are the SDK's own `Message` type, so the tests of the Anthropic judge need the `anthropic` package
(`make venv` installs it). Where it is not installed they are skipped (`NEEDS_SDK`), and everything else runs.
"""
import contextlib
import importlib.util
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from rj import schema

# A model of TokenRouter's, asked at its chat route: the demo profile's judge block before its judge moved to Anthropic.
TR_JUDGE = {"model": "gemini-3-flash-preview", "fallback_model": "gemini-3.7-flash", "path": "/chat/completions", "runs": 2,
            "guess_call": True, "max_input_chars": 600000}
CLAUDE_KEY = "sk-ant-test-0123456789abcdefghij"         # nobody's key
USAGE = {"input_tokens": 25, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 100}
# On a test class or a test that asks the Anthropic judge: skipped in a venv made before the package was a dependency.
NEEDS_SDK = unittest.skipUnless(importlib.util.find_spec("anthropic"), "the `anthropic` package is not installed (make venv installs it): "
                                                                     "the Anthropic judge is not tested")


def on_tokenrouter(profile: dict) -> dict:
    """The profile with TokenRouter's judge in place of its own."""
    return {**profile, "judge": dict(TR_JUDGE)}


def tokenrouter_judge():
    """A patch under which the demo profile loads with TokenRouter's judge. Every other profile loads as it is."""
    real = schema.load_profile

    def load(name, *args, **kwargs):
        cfg = real(name, *args, **kwargs)
        return on_tokenrouter(cfg) if name == "demo" else cfg
    return mock.patch.object(schema, "load_profile", load)


def claude_message(text: str | None = "", *, thinking: int = 1, stop_reason: str | None = "end_turn", category: str | None = None,
                   usage: dict | None = None):
    """A finished reply as the SDK hands it over (its own `Message` type): `thinking` thinking blocks with no text
    of their own, as the API sends them by default, then the text block; a stop reason; usage. A refusal carries
    `stop_details` with `category`. `text` None: no text block at all. `stop_reason` None: what the SDK hands over
    when the stream ended before the model ended its reply."""
    from anthropic.types import Message
    content = [{"type": "thinking", "thinking": "", "signature": "sig"}] * thinking + ([] if text is None else [{"type": "text", "text": text}])
    details = {"type": "refusal", "category": category, "explanation": None} if stop_reason == "refusal" else None
    return Message.model_validate({"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
                                   "stop_reason": stop_reason, "stop_sequence": None, "stop_details": details, "usage": {**USAGE, **(usage or {})}})


class FakeClaude:
    """Stands in for `anthropic.Anthropic`. `messages.stream(**request)` is a context manager whose
    `get_final_message()` gives the next scripted reply (an exception among them is raised when the request is
    made; a function among them is called when the reply is read, so it can raise there: a reply that broke off),
    and after those `answer(prompt)`: a message, or an object that is sent as JSON text behind one thinking
    block. Every request is kept as the keyword arguments it came with. `messages.create` fails the test: the
    judge streams."""

    def __init__(self, answer, first: list | None = None):
        self.answer, self.first, self.requests, self.closed = answer, list(first or []), [], False
        self.messages = self

    @contextlib.contextmanager
    def stream(self, **request):
        self.requests.append(request)
        reply = self.first.pop(0) if self.first else self.answer(request["messages"][0]["content"])
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, (dict, list)):
            reply = claude_message(json.dumps(reply))
        yield SimpleNamespace(get_final_message=reply if callable(reply) else lambda: reply)

    def create(self, **request):
        raise AssertionError("the judge asks with messages.stream, never with messages.create")

    def close(self) -> None:
        self.closed = True

    def prompts(self) -> list[str]:
        return [r["messages"][0]["content"] for r in self.requests]
