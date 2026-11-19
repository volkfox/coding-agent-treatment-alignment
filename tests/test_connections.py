"""tools/connections.py against a fake HarnessRouter account (httpx.MockTransport). No socket is opened.

What is checked: listing and the dry mode send one GET and nothing else; what --set sends is the request shape the
project's own checks sent; every connection that is not being replaced goes back as the service returned it; no
key, key prefix or key hint reaches the output, not even from an error reply that echoes the request; a refusal
or a failed read sends no PUT.
"""
import importlib.util
import json
import os
import re
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from rj import preflight

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rj_tools_connections", ROOT / "tools" / "connections.py")
connections = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(connections)

HR = "https://hr.test"
HR_KEY = "hrk-test-0123456789abcdef-not-a-real-key"
TR_KEY = "tr-test-0123456789abcdef-not-a-real-key"
ANTHROPIC_KEY = "sk-ant-test-0123456789abcdef-not-a-real-key"
OPENAI_KEY = "sk-oai-test-0123456789abcdef-not-a-real-key"
OTHER_KEY = "other-test-0123456789abcdef-not-a-real-key"       # stored in the account; never in the environment
SENTINEL = "********"                                          # what the fake returns in place of a stored key
ENV = {"HR_BASE_URL": HR, "HR_API_KEY": HR_KEY}
ALL = {**ENV, "TR_API_KEY": TR_KEY, "ANTHROPIC_API_KEY": ANTHROPIC_KEY, "OPENAI_API_KEY": OPENAI_KEY}


def hint(key: str) -> str:
    return key[:6] + "..." + key[-4:]


def row(name: str, provider: str, key: str, **more) -> dict:
    return {"name": name, "provider": provider, "config": {"api_key": key}, "models": [], **more}


class Account:
    """The two calls of /v1/integrations. GET returns every stored key as a sentinel plus a `key_hint`; PUT replaces
    the whole document, and a sentinel sent back keeps the stored key (the fake's assumption, not a known fact)."""

    def __init__(self, rows: list | None = None, routes: dict | None = None, *, get: tuple | None = None, put: tuple | None = None):
        self.rows = [dict(r) for r in rows or []]
        self.routes = dict(routes or {})
        self.get, self.put = get, put           # (status, body) to answer with in place of the real thing
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []

    @property
    def calls(self) -> list[tuple]:
        return [(r.method, r.url.path) for r in self.requests]

    def document(self) -> dict:
        shown = []
        for r in self.rows:
            config = dict(r.get("config") or {})
            key = config.get("api_key", "")
            shown.append({**r, "config": {**config, "api_key": SENTINEL}, "key_hint": hint(key)})
        return {"integrations": shown, "model_map": dict(self.routes)}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path != "/v1/integrations":
            return httpx.Response(404, json={"error": "not in the test's service"})
        if request.method == "GET":
            if self.get:
                return httpx.Response(self.get[0], json=self.get[1]) if not isinstance(self.get[1], str) else httpx.Response(self.get[0], text=self.get[1])
            return httpx.Response(200, json=self.document())
        if request.method == "PUT":
            body = json.loads(request.content)
            self.bodies.append(body)
            if self.put:
                return httpx.Response(self.put[0], text=self.put[1] + request.content.decode())
            stored = {r["name"]: r["config"].get("api_key") for r in self.rows}
            self.rows = []
            for r in body["integrations"]:
                r = json.loads(json.dumps(r))
                if r["config"].get("api_key") == SENTINEL:
                    r["config"]["api_key"] = stored[r["name"]]
                self.rows.append(r)
            if "model_map" in body:
                self.routes = dict(body["model_map"])
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(405, json={"error": "method"})


def run(account: Account, *argv: str, env: dict = ALL) -> tuple[int, str]:
    lines: list[str] = []
    code = connections.main(list(argv), env=env, transport=httpx.MockTransport(account), out=lines.append)
    return code, "\n".join(lines)


class NothingSecretIsPrinted:
    """Mixed in: whatever a test printed holds no key, no first 16 characters of one and no key hint."""

    def clean(self, text: str, *more: str) -> None:
        for key in (HR_KEY, TR_KEY, ANTHROPIC_KEY, OPENAI_KEY, OTHER_KEY, *more):
            self.assertNotIn(key, text)
            self.assertNotIn(key[:16], text)
            self.assertNotIn(hint(key), text)
        self.assertNotIn(SENTINEL, text, "a masked value the service returned is not printed either")


class Listing(NothingSecretIsPrinted, unittest.TestCase):
    def test_no_flag_sends_one_get_and_changes_nothing(self):
        account = Account([row("default", "tokenrouter", TR_KEY, config={"base_url": "https://api.tokenrouter.com/v1", "api_key": TR_KEY}),
                           row("other", "some-provider", OTHER_KEY, models=[{"canonical": "m-1", "provider_id": "m-1"}])],
                          {"m-1": "other", "m-2": "default", "m-3": "default"})
        code, text = run(account)
        self.assertEqual(code, 0)
        self.assertEqual(account.calls, [("GET", "/v1/integrations")])
        self.assertIn("holds 2 connection(s) and 3 route(s)", text)
        self.assertIn("default (tokenrouter): config fields ['api_key', 'base_url'], base_url https://api.tokenrouter.com/v1; "
                      "0 model(s) listed, 2 routed to it; its key hint fits TR_API_KEY: True", text)
        self.assertIn("other (some-provider): config fields ['api_key']; 1 model(s) listed, 1 routed to it; "
                      "no variable of this helper belongs to it", text)
        self.assertIn("keys in the environment: TR_API_KEY set, ANTHROPIC_API_KEY set, OPENAI_API_KEY set", text)
        self.clean(text)

    def test_the_request_carries_the_key_as_a_bearer_token(self):
        account = Account()
        run(account)
        self.assertEqual(account.requests[0].headers["authorization"], "Bearer " + HR_KEY)
        self.assertEqual(account.requests[0].headers["user-agent"], "rj-preflight/0.1")
        self.assertEqual(str(account.requests[0].url), HR + "/v1/integrations")

    def test_a_key_that_is_not_the_stored_one_is_said_so_with_a_boolean(self):
        account = Account([row("default", "tokenrouter", "tr-stored-another-key-0123456789abcdef")])
        code, text = run(account, env={**ENV, "TR_API_KEY": TR_KEY})
        self.assertEqual(code, 0)
        self.assertIn("its key hint fits TR_API_KEY: False", text)
        self.clean(text, "tr-stored-another-key-0123456789abcdef")

    def test_the_account_variables_are_needed(self):
        account = Account()
        code, text = run(account, env={"HR_BASE_URL": HR, "TR_API_KEY": TR_KEY})
        self.assertEqual((code, account.calls), (2, []))
        self.assertIn("missing: HR_API_KEY", text)

    def test_options_of_a_write_are_refused_without_a_mode(self):
        account = Account()
        code, text = run(account, "--route", "m-1=default")
        self.assertEqual((code, account.calls), (2, []))
        self.assertIn("go with --dry or --set", text)

    def test_fields_the_helper_does_not_send_back_are_named(self):
        """The PUT replaces the whole document, so a field this helper does not know would be lost without a word."""
        doc = {"integrations": [{**row("other", "some-provider", SENTINEL), "enabled": False, "priority": 3, "key_hint": "ot...ey"},
                                row("plain", "some-provider", SENTINEL)],
               "model_map": {"m-1": "other"}, "fallback": "other"}
        for argv in ([], ["--dry", "--route", "m-2=other"]):
            with self.subTest(argv=argv):
                account = Account(get=(200, doc))
                code, text = run(account, *argv, env=ENV)
                self.assertEqual(code, 0, text)
                self.assertEqual(account.calls, [("GET", "/v1/integrations")])
                self.assertIn("note: the reply's field(s) ['fallback'] are not sent back by --set", text)
                self.assertIn("note: connection 'other' has field(s) ['enabled', 'priority'] that --set does not send back", text)
                self.assertNotIn("connection 'plain' has field(s)", text)
                self.assertNotIn("'key_hint'", text, "the service's own field, known not to be sent")
        code, text = run(Account([row("default", "tokenrouter", TR_KEY)], {"m-1": "default"}))
        self.assertEqual(code, 0)
        self.assertNotIn("not sent back", text, "nothing to say about a reply of the known fields")
        self.assertNotIn("does not send back", text)

    def test_an_account_address_that_is_no_url_is_refused_before_any_request(self):
        for base in ("api.harnessrouter.example", "https://hr.test:notaport", "ftp://hr.test"):
            for argv in ([], ["--dry"], ["--set"]):
                with self.subTest(base=base, argv=argv):
                    account = Account()
                    code, text = run(account, *argv, env={**ALL, "HR_BASE_URL": base})
                    self.assertEqual((code, account.calls), (2, []), text)
                    self.assertIn("HR_BASE_URL is no http(s) URL", text)
                    self.clean(text)

    def test_an_account_key_that_is_a_placeholder_or_too_short_sends_nothing(self):
        for key, says in (("your-harnessrouter-key", "HR_API_KEY still holds the placeholder of .env.example"),
                          ("abc1234", "HR_API_KEY is shorter than 8 characters")):
            with self.subTest(says=says):
                account = Account()
                code, text = run(account, env={**ENV, "HR_API_KEY": key})
                self.assertEqual((code, account.calls), (2, []), text)
                self.assertIn(says, text)
                self.assertNotIn("abc1234", text)

    def test_a_base_url_is_shown_without_credentials_or_query(self):
        self.assertEqual(connections.shown_url("https://user:pw@host.example:8443/openai/v1?api-key=abc#frag"),
                         "https://[MASKED]@host.example:8443/openai/v1?[MASKED]#[MASKED]")
        self.assertEqual(connections.shown_url("https://api.tokenrouter.com/v1"), "https://api.tokenrouter.com/v1")
        self.assertEqual(connections.shown_url("not a url"), "[MASKED]")
        self.assertEqual(connections.shown_url(None), "[MASKED]")


class Dry(NothingSecretIsPrinted, unittest.TestCase):
    def test_dry_sends_one_get_and_prints_the_body_with_every_secret_masked(self):
        account = Account([row("other", "some-provider", OTHER_KEY, config={"api_key": OTHER_KEY, "organization": "org-private-0001"})],
                          {"m-1": "other"})
        env = {**ALL, "OPENAI_BASE_URL": "https://user:pw@llm.example/v1?sig=abcdef0123456789"}
        lines: list[str] = []
        code = connections.main(["--dry", "--route", "m-9=anthropic"], env=env, transport=httpx.MockTransport(account), out=lines.append)
        text = "\n".join(lines)
        self.assertEqual(code, 0)
        self.assertEqual(account.calls, [("GET", "/v1/integrations")], "a dry run sends no PUT")
        self.assertEqual(account.bodies, [])
        self.assertIn("WARNING: PUT /v1/integrations replaces the account's whole connection list", text)
        self.assertEqual(lines[-1], "dry: nothing was sent")
        body = json.loads(next(line for line in lines if line.startswith("{")))
        self.assertEqual(body, {
            "integrations": [
                {"name": "other", "provider": "some-provider", "config": {"api_key": "[MASKED]", "organization": "[MASKED]"}, "models": []},
                {"name": "default", "provider": "tokenrouter",
                 "config": {"base_url": "https://api.tokenrouter.com/v1", "api_key": "[MASKED]"}, "models": []},
                {"name": "anthropic", "provider": "anthropic", "config": {"api_key": "[MASKED]"}, "models": []},
                {"name": "openai", "provider": "openai", "config": {"api_key": "[MASKED]", "base_url": "[MASKED]"}, "models": []}],
            "model_map": {"m-1": "other", "m-9": "anthropic"}})
        self.assertIn("other (some-provider): kept as the service returned it; its key is not in the environment, "
                      "so the masked value goes back", text)
        self.assertIn("default (tokenrouter): new, from TR_API_KEY", text)
        self.assertIn("openai (openai): new, from OPENAI_API_KEY and OPENAI_BASE_URL", text)
        self.assertIn("was not confirmed with the service; --openai-provider ID sets another", text)
        self.assertIn("routes moved: {'m-9': 'anthropic'}", text)
        self.clean(text)
        for private in ("org-private-0001", "user:pw", "abcdef0123456789", "llm.example"):
            self.assertNotIn(private, text)

    def test_show_urls_prints_base_urls_without_credentials_or_query(self):
        """A base URL's host can name a private resource, so it is masked unless asked for; TokenRouter's public default is not."""
        account = Account([row("default", "tokenrouter", TR_KEY, config={"base_url": "https://api.tokenrouter.com/v1", "api_key": TR_KEY}),
                           row("other", "some-provider", OTHER_KEY, config={"base_url": "https://private-resource.example/v1", "api_key": OTHER_KEY})])
        env = {**ENV, "OPENAI_API_KEY": OPENAI_KEY, "OPENAI_BASE_URL": "https://user:pw@llm.example/v1?sig=abcdef0123456789"}
        for argv, shown in ((["--dry"], False), (["--dry", "--show-urls"], True), ([], False), (["--show-urls"], True)):
            with self.subTest(argv=argv):
                code, text = run(account, *argv, env=env)
                self.assertEqual(code, 0)
                self.assertEqual(account.bodies, [])
                self.assertIn("base_url https://api.tokenrouter.com/v1;", text)
                self.assertEqual("https://private-resource.example/v1" in text, shown)
                self.assertEqual("private-resource" in text, shown)
                if "--dry" in argv:
                    self.assertEqual("https://[MASKED]@llm.example/v1?[MASKED]" in text, shown)
                    self.assertEqual("llm.example" in text, shown)
                for private in ("user:pw", "abcdef0123456789"):
                    self.assertNotIn(private, text)
                self.clean(text)

    def test_dry_and_set_exclude_each_other(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            connections.main(["--dry", "--set"], env=ALL, transport=httpx.MockTransport(Account()), out=lambda line: None)


class Setting(NothingSecretIsPrinted, unittest.TestCase):
    def test_an_empty_account_gets_the_three_connections_in_the_shapes_the_checks_sent(self):
        account = Account()
        env = {**ALL, "OPENAI_BASE_URL": "https://llm.example/v1"}
        code, text = run(account, "--set", env=env)
        self.assertEqual(code, 0)
        self.assertEqual(account.calls, [("GET", "/v1/integrations"), ("PUT", "/v1/integrations"), ("GET", "/v1/integrations")])
        self.assertEqual(account.bodies, [{
            "integrations": [
                {"name": "default", "provider": "tokenrouter",
                 "config": {"base_url": "https://api.tokenrouter.com/v1", "api_key": TR_KEY}, "models": []},
                {"name": "anthropic", "provider": "anthropic", "config": {"api_key": ANTHROPIC_KEY}, "models": []},
                {"name": "openai", "provider": "openai", "config": {"api_key": OPENAI_KEY, "base_url": "https://llm.example/v1"}, "models": []}],
            "model_map": {}}])
        self.assertEqual(account.requests[1].headers["authorization"], "Bearer " + HR_KEY)
        self.assertIn("WARNING: PUT /v1/integrations replaces the account's whole connection list", text)
        self.assertIn("PUT /v1/integrations: HTTP 200", text)
        self.assertIn("the account now holds 3 connection(s)", text)
        self.assertIn("its key hint fits ANTHROPIC_API_KEY: True", text)
        self.assertIn("a full key in any reply: False", text)
        self.clean(text)

    def test_only_the_keys_that_are_present_are_set(self):
        account = Account()
        code, _ = run(account, "--set", env={**ENV, "ANTHROPIC_API_KEY": ANTHROPIC_KEY})
        self.assertEqual(code, 0)
        self.assertEqual(account.bodies[0]["integrations"],
                         [{"name": "anthropic", "provider": "anthropic", "config": {"api_key": ANTHROPIC_KEY}, "models": []}])

    def test_every_other_connection_goes_back_as_the_service_returned_it(self):
        """The PUT replaces the whole list, so what is not being replaced is sent back: name, provider, config with
        the masked key, models and image models. `key_hint` is the service's own field and is not sent."""
        models = [{"canonical": "m-1", "provider_id": "name-at-provider-1"}]
        account = Account([row("default", "tokenrouter", "tr-stored-old-key-0123456789abcdef", image_models=[{"canonical": "img-1", "provider_id": "img-1"}],
                               config={"base_url": "https://api.tokenrouter.com/v1", "api_key": "tr-stored-old-key-0123456789abcdef"}),
                           row("other", "some-provider", OTHER_KEY, models=models, config={"base_url": "https://llm.example/v1", "api_key": OTHER_KEY})],
                          {"m-1": "other", "m-2": "default"})
        code, text = run(account, "--set", "--route", "m-2=other", env={**ENV, "TR_API_KEY": TR_KEY})
        self.assertEqual(code, 0)
        self.assertEqual(account.bodies, [{
            "integrations": [
                {"name": "default", "provider": "tokenrouter", "config": {"base_url": "https://api.tokenrouter.com/v1", "api_key": TR_KEY},
                 "models": [], "image_models": [{"canonical": "img-1", "provider_id": "img-1"}]},
                {"name": "other", "provider": "some-provider", "config": {"base_url": "https://llm.example/v1", "api_key": SENTINEL},
                 "models": models}],
            "model_map": {"m-1": "other", "m-2": "other"}}])
        self.assertEqual([r["config"]["api_key"] for r in account.rows], [TR_KEY, OTHER_KEY])
        self.assertIn("default (tokenrouter): key replaced, from TR_API_KEY", text)
        self.assertIn("routes changed by this write: [('m-2', 'default', 'other')]", text)
        self.assertIn("connections gone: none", text)
        self.clean(text, "tr-stored-old-key-0123456789abcdef")

    def test_the_tokenrouter_key_goes_into_the_tokenrouter_connection_whatever_its_name(self):
        account = Account([row("my-router", "tokenrouter", "tr-stored-old-key-0123456789abcdef")])
        code, _ = run(account, "--set", env={**ENV, "TR_API_KEY": TR_KEY})
        self.assertEqual(code, 0)
        self.assertEqual(account.bodies[0]["integrations"],
                         [{"name": "my-router", "provider": "tokenrouter", "config": {"api_key": TR_KEY}, "models": []}])

    def test_base_urls_are_optional_and_sent_only_when_set(self):
        account = Account([row("default", "tokenrouter", TR_KEY, config={"base_url": "https://api.tokenrouter.com/v1", "api_key": TR_KEY})])
        env = {**ALL, "TR_BASE_URL": "https://router.example/v1", "ANTHROPIC_BASE_URL": "https://claude.example"}
        code, _ = run(account, "--set", env=env)
        self.assertEqual(code, 0)
        self.assertEqual([r["config"] for r in account.bodies[0]["integrations"]],
                         [{"base_url": "https://router.example/v1", "api_key": TR_KEY},
                          {"api_key": ANTHROPIC_KEY, "base_url": "https://claude.example"},
                          {"api_key": OPENAI_KEY}])

    def test_models_can_be_listed_on_a_connection_and_the_openai_provider_id_named(self):
        account = Account()
        env = {**ENV, "OPENAI_API_KEY": OPENAI_KEY, "OPENAI_BASE_URL": "https://llm.example/v1"}
        code, text = run(account, "--set", "--openai-provider", "openai-compatible", "--model", "openai=gpt-x", "--model", "openai=gpt-y:name-y",
                         "--route", "gpt-x=openai", env=env)
        self.assertEqual(code, 0)
        self.assertEqual(account.bodies, [{
            "integrations": [{"name": "openai", "provider": "openai-compatible", "config": {"api_key": OPENAI_KEY, "base_url": "https://llm.example/v1"},
                              "models": [{"canonical": "gpt-x", "provider_id": "gpt-x"}, {"canonical": "gpt-y", "provider_id": "name-y"}]}],
            "model_map": {"gpt-x": "openai"}}])
        self.assertNotIn("was not confirmed with the service", text, "the note is for the default id only")

    def test_an_openai_connection_that_exists_keeps_the_provider_id_it_has(self):
        account = Account([row("openai", "vendor-specific-id", "sk-stored-old-key-0123456789abcdef", models=[{"canonical": "gpt-x", "provider_id": "gpt-x"}])])
        code, _ = run(account, "--set", env={**ENV, "OPENAI_API_KEY": OPENAI_KEY})
        self.assertEqual(code, 0)
        self.assertEqual(account.bodies[0]["integrations"], [{"name": "openai", "provider": "vendor-specific-id", "config": {"api_key": OPENAI_KEY},
                                                              "models": [{"canonical": "gpt-x", "provider_id": "gpt-x"}]}])

    def test_an_account_that_returned_no_route_map_gets_none_back(self):
        """The plan's first write sends no route map (section 3, "By hand, once"); an empty one in its place is another request."""
        for doc in ({"integrations": []}, {"integrations": [], "model_map": None}):
            with self.subTest(doc=doc):
                account = Account(get=(200, doc))
                code, text = run(account, "--set", env={**ENV, "TR_API_KEY": TR_KEY})
                self.assertEqual(code, 0, text)
                self.assertEqual(sorted(account.bodies[0]), ["integrations"])
                self.assertIn("sending 1 connection(s) and no route map (the account returned none)", text)
                account = Account(get=(200, doc))
                code, text = run(account, "--set", "--route", "m-1=default", env={**ENV, "TR_API_KEY": TR_KEY})
                self.assertEqual(code, 0, text)
                self.assertEqual(account.bodies[0]["model_map"], {"m-1": "default"})
        account = Account(get=(200, {"integrations": []}))
        code, text = run(account, "--dry", env={**ENV, "TR_API_KEY": TR_KEY})
        self.assertNotIn("model_map", text, "the printed body is the body that would go")

    def test_the_openai_key_goes_into_the_connection_openai_name_names(self):
        """An account's OpenAI-compatible connection may have any name; without the option a second one would be added
        beside it and the first sent back with its masked key."""
        old = "sk-stored-old-key-0123456789abcdef"
        models = [{"canonical": "gpt-x", "provider_id": "name-x"}]
        account = Account([row("default", "tokenrouter", TR_KEY),
                           row("my-endpoint", "vendor-specific-id", old, models=models, config={"base_url": "https://llm.example/v1", "api_key": old})],
                          {"gpt-x": "my-endpoint"})
        code, text = run(account, "--set", "--openai-name", "my-endpoint", env={**ENV, "OPENAI_API_KEY": OPENAI_KEY})
        self.assertEqual(code, 0, text)
        self.assertEqual(account.bodies[0]["integrations"], [
            {"name": "default", "provider": "tokenrouter", "config": {"api_key": SENTINEL}, "models": []},
            {"name": "my-endpoint", "provider": "vendor-specific-id", "config": {"base_url": "https://llm.example/v1", "api_key": OPENAI_KEY},
             "models": models}])
        self.assertIn("my-endpoint (vendor-specific-id): key replaced, from OPENAI_API_KEY", text)
        self.assertIn("its key hint fits OPENAI_API_KEY: True", text)
        self.clean(text, old)

        account = Account()
        code, text = run(account, "--set", "--openai-name", "my-endpoint", env={**ENV, "OPENAI_API_KEY": OPENAI_KEY})
        self.assertEqual(code, 0, text)
        self.assertEqual(account.bodies[0]["integrations"], [{"name": "my-endpoint", "provider": "openai", "config": {"api_key": OPENAI_KEY}, "models": []}])
        self.assertIn("was not confirmed with the service; --openai-provider ID sets another", text)

    def test_a_refused_put_that_echoes_the_request_prints_no_stored_value(self):
        """A connection that is kept goes back as the service returned it. Should the service return a stored key in
        full, a refusal that echoes the request must bring neither that key nor a base URL to the screen."""
        stored = "stored-full-key-0123456789abcdef-not-a-real-key"
        doc = {"integrations": [{"name": "other", "provider": "some-provider", "models": [], "key_hint": "stor...-key",
                                 "config": {"api_key": stored, "base_url": "https://user:pw@private-host.example/v1?sig=abcdef", "pin": "1234"}}],
               "model_map": {}}
        env = {**ENV, "OPENAI_API_KEY": OPENAI_KEY, "OPENAI_BASE_URL": "https://llm.example/v1"}
        echoes = {"as sent": lambda sent: sent,
                  "written again, slashes escaped": lambda sent: json.dumps(json.loads(sent), indent=1).replace("/", "\\/"),
                  "in prose, the key cut short": lambda sent: "no route to private-host.example or llm.example; key " + stored[:20] + ", hint stor...-key"}
        for name, echo in echoes.items():
            with self.subTest(echo=name):
                calls = []

                def service(request: httpx.Request) -> httpx.Response:
                    calls.append(request.method)
                    if request.method == "GET":
                        return httpx.Response(200, json=doc)
                    return httpx.Response(400, text="invalid request: " + echo(request.content.decode()))
                lines: list[str] = []
                code = connections.main(["--set"], env=env, transport=httpx.MockTransport(service), out=lines.append)
                text = "\n".join(lines)
                self.assertEqual((code, calls), (1, ["GET", "PUT"]))
                self.assertIn("PUT /v1/integrations: HTTP 400", text)
                self.assertIn("body: invalid request: ", text)
                for private in (stored, stored[:16], "stor...-key", "private-host", "user:pw", "sig=abcdef", "llm.example", '"1234"'):
                    self.assertNotIn(private, text)
                self.clean(text)

    def test_a_refused_put_is_reported_without_the_keys_it_echoes(self):
        account = Account(put=(422, "unknown provider; you sent: "))
        code, text = run(account, "--set")
        self.assertEqual(code, 1)
        self.assertEqual(account.calls, [("GET", "/v1/integrations"), ("PUT", "/v1/integrations")])
        self.assertIn("PUT /v1/integrations: HTTP 422", text)
        self.assertIn("unknown provider; you sent:", text)
        self.assertIn("[KEY]", text)
        self.clean(text)


class Refusals(NothingSecretIsPrinted, unittest.TestCase):
    def refused(self, account: Account, *argv: str, env: dict = ALL, says: str) -> str:
        for mode in ("--dry", "--set"):
            before = len(account.requests)
            code, text = run(account, mode, *argv, env=env)
            self.assertEqual(code, 2, text)
            self.assertEqual([(r.method, r.url.path) for r in account.requests[before:]], [("GET", "/v1/integrations")], "nothing but the read")
            self.assertIn("refused, nothing was sent", text)
            self.assertIn(says, text)
            self.clean(text)
        self.assertEqual(account.bodies, [])
        return text

    def test_no_key_and_no_route(self):
        self.refused(Account([row("default", "tokenrouter", TR_KEY)]), env=ENV, says="nothing to set")

    def test_a_connection_of_the_same_name_and_another_provider(self):
        self.refused(Account([row("default", "some-provider", OTHER_KEY)]), env={**ENV, "TR_API_KEY": TR_KEY},
                     says="a connection named 'default' of provider 'some-provider', not 'tokenrouter'")
        self.refused(Account([row("anthropic", "some-provider", OTHER_KEY)]), env={**ENV, "ANTHROPIC_API_KEY": ANTHROPIC_KEY},
                     says="a connection named 'anthropic' of provider 'some-provider', not 'anthropic'")

    def test_a_route_to_a_connection_that_is_not_in_the_list(self):
        self.refused(Account(), "--route", "m-1=nowhere", says="no connection named 'nowhere'")
        self.refused(Account(), "--route", "m-1", says="--route takes MODEL=CONNECTION")
        self.refused(Account(), "--model", "nowhere=m-1", says="--model: no connection named 'nowhere'")

    def test_a_base_url_on_this_machine_is_not_written_into_a_remote_account(self):
        """The Makefile exports the stub's address as TR_BASE_URL; left in the shell, it would reach the account."""
        self.refused(Account(), env={**ENV, "TR_API_KEY": TR_KEY, "TR_BASE_URL": "http://127.0.0.1:8089/tr"},
                     says="TR_BASE_URL points at this machine and HR_BASE_URL does not")
        self.refused(Account(), env={**ENV, "OPENAI_API_KEY": OPENAI_KEY, "OPENAI_BASE_URL": "llm.example/v1"}, says="OPENAI_BASE_URL is no http(s) URL")

    def test_a_placeholder_or_a_key_too_short_to_be_one_is_not_written(self):
        """`cp .env.example .env` leaves TR_API_KEY=your-tokenrouter-key until it is filled in; --set would store it in
        place of the working key. A value under 8 characters is no key, and too short to be kept out of the output."""
        self.refused(Account([row("default", "tokenrouter", TR_KEY)]), env={**ENV, "TR_API_KEY": "your-tokenrouter-key"},
                     says="TR_API_KEY still holds the placeholder of .env.example")
        text = self.refused(Account(), env={**ENV, "TR_API_KEY": TR_KEY, "ANTHROPIC_API_KEY": "abc1234"},
                            says="ANTHROPIC_API_KEY is shorter than 8 characters")
        self.assertNotIn("abc1234", text)

    def test_the_openai_key_is_not_put_into_a_connection_of_another_kind(self):
        self.refused(Account([row("default", "tokenrouter", TR_KEY)]), "--openai-name", "default", env={**ENV, "OPENAI_API_KEY": OPENAI_KEY},
                     says="connection 'default' is of provider 'tokenrouter'")
        self.refused(Account([row("default", "tokenrouter", TR_KEY)]), "--openai-name", "default", env={**ENV, "TR_API_KEY": TR_KEY, "OPENAI_API_KEY": OPENAI_KEY},
                     says="connection 'default' is of provider 'tokenrouter'")
        self.refused(Account([row("openai", "anthropic", OTHER_KEY)]), env={**ENV, "OPENAI_API_KEY": OPENAI_KEY},
                     says="connection 'openai' is of provider 'anthropic'")

    def test_a_base_url_without_its_key_sets_nothing(self):
        account = Account()
        code, text = run(account, "--dry", env={**ENV, "TR_API_KEY": TR_KEY, "TR_BASE_URL": "", "OPENAI_BASE_URL": "https://llm.example/v1"})
        self.assertEqual(code, 0)
        self.assertIn("note: OPENAI_BASE_URL is set and OPENAI_API_KEY is not; no connection is set from it", text)
        self.assertNotIn('"openai"', text)


class FailedReads(NothingSecretIsPrinted, unittest.TestCase):
    def test_a_read_that_fails_sends_no_put(self):
        """An error reply read as an empty list, then sent back, would empty the account."""
        for get, says in (((500, {"error": "down, key " + HR_KEY}), "GET /v1/integrations: HTTP 500"),
                          ((401, {"error": {"message": "invalid key"}}), "GET /v1/integrations: HTTP 401"),
                          ((200, {"error": "no list here"}), "not the shape this helper knows (no list under `integrations`)"),
                          ((200, {"integrations": [{"name": "x"}]}), "a connection without a `name` and a `provider`"),
                          ((200, {"integrations": [row("a", "p", OTHER_KEY), row("a", "p", OTHER_KEY)]}), "two connections of one name"),
                          ((200, "<html>not json</html>"), "not the shape this helper knows")):
            with self.subTest(says=says):
                account = Account(get=get)
                code, text = run(account, "--set")
                self.assertEqual(code, 1)
                self.assertEqual(account.calls, [("GET", "/v1/integrations")])
                self.assertIn(says, text)
                self.assertIn("nothing was sent", text)
                self.clean(text)

    def test_a_failed_read_that_names_a_key_the_helper_never_saw(self):
        """Before a read has succeeded the helper knows no stored value; a long key-like run in an error text is cut anyway."""
        account = Account(get=(500, {"error": "cannot decrypt " + OTHER_KEY + " of connection other"}))
        code, text = run(account, "--set")
        self.assertEqual((code, account.calls), (1, [("GET", "/v1/integrations")]))
        self.assertIn("cannot decrypt [MASKED] of connection other", text)
        self.clean(text)

    def test_an_address_the_http_layer_refuses_ends_without_a_traceback(self):
        account = Account()
        code, text = run(account, "--set", env={**ALL, "HR_BASE_URL": HR + "/a\x7fb"})
        self.assertEqual((code, account.calls), (1, []))
        self.assertIn("GET /v1/integrations: no reply (InvalidURL)", text)
        self.assertIn("nothing was sent", text)

    def test_no_reply_at_all(self):
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused, Authorization: Bearer " + HR_KEY)
        lines: list[str] = []
        code = connections.main(["--set"], env=ALL, transport=httpx.MockTransport(down), out=lines.append)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-2:], ["GET /v1/integrations: no reply (ConnectError)", "nothing was sent"])
        self.clean("\n".join(lines))


class Environment(NothingSecretIsPrinted, unittest.TestCase):
    def test_a_given_environment_reads_no_file_and_no_process_variable(self):
        with mock.patch("rj.cli.load_env", side_effect=AssertionError("the env file was read")), \
                mock.patch.dict(os.environ, {"OPENAI_API_KEY": OPENAI_KEY}):
            account = Account()
            code, _ = run(account, "--set", env={**ENV, "TR_API_KEY": TR_KEY})
        self.assertEqual(code, 0)
        self.assertEqual([r["name"] for r in account.bodies[0]["integrations"]], ["default"])

    def test_without_one_it_reads_what_rj_reads_and_a_variable_already_set_wins(self):
        """RJ_ENV_FILE names the file; a variable of the shell keeps its value, and the output says so by name."""
        extra = set(preflight.EXTRA_SECRETS)
        self.addCleanup(lambda: (preflight.EXTRA_SECRETS.clear(), preflight.EXTRA_SECRETS.update(extra)))
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "rj.env"
            env_file.write_text(f"HR_BASE_URL={HR}\nHR_API_KEY={HR_KEY}\nTR_API_KEY={TR_KEY}\n"
                                f"ANTHROPIC_API_KEY=sk-ant-from-the-file-0123456789abcdef\n", encoding="utf-8")
            clear = {n: "" for n in connections.NAMES}
            with mock.patch.dict(os.environ, {**clear, "RJ_ENV_FILE": str(env_file), "ANTHROPIC_API_KEY": ANTHROPIC_KEY}):
                account = Account()
                lines: list[str] = []
                code = connections.main(["--set"], transport=httpx.MockTransport(account), out=lines.append)
        text = "\n".join(lines)
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "environment: 3 of this helper's variables come from the env file; "
                                   "already set in the shell, so the file's value is not used: ANTHROPIC_API_KEY")
        self.assertEqual([(r["name"], r["config"]["api_key"]) for r in account.bodies[0]["integrations"]],
                         [("default", TR_KEY), ("anthropic", ANTHROPIC_KEY)])
        self.clean(text, "sk-ant-from-the-file-0123456789abcdef")

    def test_the_hint_test(self):
        self.assertTrue(connections.hint_matches("sk-ab...wxyz", "sk-abcdefghijklmnopqrstuvwxyz"))
        self.assertFalse(connections.hint_matches("sk-ab...wxyz", "sk-abcdefghijklmnopqrstuvwxy0"))
        self.assertFalse(connections.hint_matches("", "sk-abcdefghijklmnopqrstuvwxyz"))
        self.assertFalse(connections.hint_matches("***", "sk-abcdefghijklmnopqrstuvwxyz"))


class Documented(NothingSecretIsPrinted, unittest.TestCase):
    def test_every_command_line_the_readme_and_the_plan_give_is_one_the_helper_takes(self):
        """The helper is never run against a real account here, so the documented commands are run against the fake."""
        commands = []
        readme = ROOT / "README.md"
        if readme.exists():
            commands += [shlex.split(line, comments=True)[2:] for line in readme.read_text(encoding="utf-8").splitlines()
                         if line.startswith(".venv/bin/python tools/connections.py")]
            self.assertIn(["--dry"], commands, "the README shows the dry mode")
            self.assertIn("replaces the account's whole connection list", readme.read_text(encoding="utf-8"))
        plan = (ROOT / "fairness-study" / "CODING_PLAN.md").read_text(encoding="utf-8")
        commands += [shlex.split(rest) for rest in re.findall(r"`tools/connections\.py([^`]*)`", plan)]
        self.assertIn(["--set"], commands)
        for argv in commands:
            with self.subTest(argv=argv):
                account = Account([row("default", "tokenrouter", TR_KEY)], {"claude-haiku-4.5": "default"})
                code, text = run(account, *argv)
                self.assertEqual(code, 0, text)
                self.assertEqual(("PUT", "/v1/integrations") in account.calls, "--set" in argv)
                self.clean(text)

    def test_the_example_env_file_names_the_variables_the_helper_reads(self):
        example = ROOT / ".env.example"
        if not example.exists():
            self.skipTest("no .env.example in this tree")
        named = set(re.findall(r"^#? ?([A-Z][A-Z0-9_]*)=", example.read_text(encoding="utf-8"), flags=re.M))
        self.assertEqual(named, set(connections.NAMES))
        keys = re.findall(r"^#? ?([A-Z][A-Z0-9_]*_KEY)=(\S+)", example.read_text(encoding="utf-8"), flags=re.M)
        self.assertEqual(len(keys), 4)
        for name, value in keys:
            self.assertTrue(connections.is_placeholder(value), f"{name}: --set would write the template's value as a key")


if __name__ == "__main__":
    unittest.main()
