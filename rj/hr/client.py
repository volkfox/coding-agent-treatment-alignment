"""HarnessRouter HTTP client. Plan section 6: the table, the session sequence, the rules.

One class, `HRClient`, and one error, `HRError`. Every route and request body is the one in
section 6's table. The rules the client itself keeps:

  - plain text only: `send` posts a string as `input`, never `instructions`, `stream`,
    attachments, a typed input or `max_cost_usd`;
  - `max_step` and `timeout_seconds` go with every turn;
  - `model` goes with the first turn of a session only;
  - the key lives in the Authorization header and nowhere else: not in an error, a log line or a repr.

The caller keeps the rule the client cannot see: never `send` while a turn of that session is live.

Retries, and nothing else is retried:
  - `poll`: HTTP 503 is asked again, inside the overall time limit;
  - `put`: HTTP 409 (`session_busy`) backs off and tries again, a bounded number of times;
  - any call: HTTP 503 naming "idempotency store unavailable" stops this client sending
    `Idempotency-Key` for the rest of its life and repeats that one request once.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping
from urllib.parse import quote

import httpx

from rj import schema

log = logging.getLogger(__name__)

PUT_LIMIT_BYTES = 4 * 1024 * 1024          # section 6: 4 MiB cap on a PUT file
EXCERPT_CHARS = 300
IDEMPOTENCY_DOWN = "idempotency store unavailable"
KEY_MASK = "[HR_API_KEY]"
EDITIONS = ("cloud", "ce")


class HRError(Exception):
    """Anything the service refused, answered wrongly, or did not answer.

    status   the HTTP status of the reply; None when there was no reply to read (a transport
             failure, a refusal before sending, a poll that ran out of time)
    route    "METHOD /v1/..." as sent, without the host
    excerpt  the first characters of the reply body
    detail   what was wrong when the status does not say it: the validator's message, the reason
    body     the reply body as bytes, or None
    """

    def __init__(self, status: int | None, route: str, excerpt: str = "", *,
                 detail: str | None = None, body: bytes | None = None):
        self.status, self.route, self.excerpt, self.detail, self.body = status, route, excerpt, detail, body
        head = f"{route} -> {'no reply' if status is None else f'HTTP {status}'}"
        super().__init__(": ".join(p for p in (head, detail, excerpt) if p))


def is_rejected_followup(exc: BaseException) -> bool:
    """True for a 4xx other than 409 and 429: the service turned the POST down for good.

    The runner classes the session `ended` on this for a follow-up `send`. It also holds for
    HTTP 402, which section 11 counts as `infra`, so test for `infra` first (section 7's order).
    """
    return (isinstance(exc, HRError) and exc.status is not None
            and 400 <= exc.status < 500 and exc.status not in (409, 429))


class HRClient:
    """Async client for one HarnessRouter account. Use as `async with HRClient() as hr:` or call `aclose()`.

    `raw` holds the last decoded JSON reply of each route, keyed like "GET /v1/balance" or
    "GET /v1/responses/{id}": day one saves one raw reply per route from it.
    `idempotency` turns False once the service has said its idempotency store is unavailable.
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None, *, edition: str = "cloud",
                 transport: httpx.AsyncBaseTransport | None = None, poll_seconds: float = 2.5,
                 timeout: float = 60.0, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        base_url = base_url or os.environ.get("HR_BASE_URL")
        if not base_url:
            raise ValueError("no base URL: pass base_url or set HR_BASE_URL")
        if edition not in EDITIONS:
            raise ValueError(f"edition must be one of {EDITIONS}, got {edition!r}")
        key = (api_key if api_key is not None else os.environ.get("HR_API_KEY") or "").strip()
        if not (key.isascii() and key.isprintable()):
            # Refused here, unquoted: the HTTP layer would name the whole header in its own error.
            raise ValueError("the API key holds characters a header cannot carry")
        self.base_url = base_url.rstrip("/")
        self.edition = edition
        self.poll_seconds = poll_seconds
        self.idempotency = True
        self.raw: dict[str, Any] = {}
        self._key = key or None      # the stub needs none
        self._sleep = sleep
        self._timeout = timeout
        self._harness_ids: dict[tuple, str] = {}
        headers = {"Accept": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        self._http = httpx.AsyncClient(transport=transport, headers=headers, timeout=timeout)

    def __repr__(self) -> str:
        return (f"HRClient(base_url={self.base_url!r}, edition={self.edition!r}, "
                f"key={'set' if self._key else 'unset'}, idempotency={'on' if self.idempotency else 'off'})")

    async def __aenter__(self) -> "HRClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ----------------------------------------------------------------------- the table

    async def bases(self) -> dict:
        """GET /v1/bases: `{"bases": [...]}`. Model availability is read here."""
        return await self._get_json("/v1/bases", "GET /v1/bases", lambda o: schema.validate("BASES", o))

    async def models(self) -> Any:
        """GET /v1/models, as decoded: its envelope is unknown on Cloud, so nothing is checked."""
        return await self._get_json("/v1/models", "GET /v1/models")

    async def balance(self) -> schema.Balance | None:
        """GET /v1/balance. Community Edition has no billing: None, and the server is not asked."""
        if self.edition == "ce":
            return None
        return await self._get_json("/v1/balance", "GET /v1/balance", schema.Balance.from_dict)

    async def integrations(self) -> dict:
        """GET /v1/integrations; on Community Edition GET /v1/admin/integrations."""
        path = "/v1/admin/integrations" if self.edition == "ce" else "/v1/integrations"
        return await self._get_json(path, f"GET {path}", lambda o: schema.validate("INTEGRATIONS", o))

    async def ensure_harness(self, row: Mapping[str, Any], *, key: str | None = None,
                             instead_of: str | None = None) -> str:
        """POST /v1/harnesses `{name: "rj-<id>", base, default_model}`; returns the harness id.

        `row` is a matrix row (`id`, `base`, `model`). No system prompt is sent. The id is looked
        for under `id`, then `harness_id`; the whole reply is in `raw["POST /v1/harnesses"]`.
        The default `Idempotency-Key` is a hash of the body, and one client asks once per row.
        The service honours that key for about a day and then makes a second harness of the same
        name (seen on 18 and 19 Nov 2026), so the id is not the row's for good: the runner keeps
        the first one per run directory and asks here only for a row that has none.

        `instead_of` is the id of a harness of this row that the service no longer knows (`harness`
        said 404 `harness_not_found`): one more is asked for. What this client remembers of the row
        is passed over, and the default key is a hash of the body and that id: a service that still
        honours the first key would hand the lost harness back. The same lost id gives the same key.
        """
        body = {"name": f"rj-{row['id']}", "base": row["base"], "default_model": row["model"]}
        cached = (body["name"], body["base"], body["default_model"])
        if instead_of is None and cached in self._harness_ids:
            return self._harness_ids[cached]
        if key is None:
            digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
            if instead_of is not None:
                digest = hashlib.sha256(f"{digest}|{instead_of}".encode("utf-8")).hexdigest()
            key = f"rj-harness-{digest[:32]}"
        route = "POST /v1/harnesses"
        resp = await self._call("POST", "/v1/harnesses", body=body, key=key)
        reply = self._json(resp, route, route)
        found = (reply.get("id") or reply.get("harness_id")) if isinstance(reply, dict) else None
        if not isinstance(found, (str, int)) or isinstance(found, bool):
            raise self._error(resp.status_code, route, detail="reply holds no harness id under `id` or `harness_id`",
                              body=resp.content)
        self._harness_ids[cached] = str(found)
        return str(found)

    async def harness(self, harness_id: str) -> dict:
        """GET /v1/harnesses/{id}: the harness as the service holds it; also in `raw["GET /v1/harnesses/{id}"]`.

        An id the service does not know is an HRError with status 404 whose body names
        `harness_not_found`; a 404 that does not name it says nothing about the harness. The
        reply must be an object and, where it names an id (under `id`, then `harness_id`), name
        the one asked for; nothing else of it is checked.
        """
        def fit(reply: Any) -> dict:
            if not isinstance(reply, dict):
                raise schema.SchemaError(f"harness: expected an object, got {type(reply).__name__}")
            found = reply.get("id") or reply.get("harness_id")
            if found is not None and str(found) != str(harness_id):
                raise schema.SchemaError(f"harness: the reply names {str(found)!r}, not the id asked for")
            return reply

        return await self._get_json(f"/v1/harnesses/{_seg(harness_id)}", "GET /v1/harnesses/{id}", fit)

    async def send(self, text: str, *, harness_id: str, model: str | None = None, prev: str | None = None,
                   limits: Mapping[str, Any], key: str | None) -> schema.Response:
        """POST /v1/responses: one user message, in the background. Returns the (usually queued) Response.

        The first turn of a session passes `model`; a later turn passes `prev`, the id of the turn
        before it, and `model` is then left out whatever was passed. `limits` holds `max_step` and
        `timeout_seconds` (the profile's `caps`). `key` becomes the `Idempotency-Key` header.
        """
        body = self.request_body(text, harness_id=harness_id, model=model, prev=prev, limits=limits)
        route = "POST /v1/responses"
        resp = await self._call("POST", "/v1/responses", body=body, key=key)
        return self._fit(schema.Response.from_dict, self._json(resp, route, route), resp, route)

    @staticmethod
    def request_body(text: str, *, harness_id: str, model: str | None = None, prev: str | None = None,
                     limits: Mapping[str, Any]) -> dict:
        """The JSON `send` posts for these arguments: what the runner saves as `request.json`."""
        if not isinstance(text, str):
            raise TypeError("send takes plain text only")
        caps = {}
        for name in ("max_step", "timeout_seconds"):       # always sent: unset is unlimited on some bases
            value = limits.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"limits[{name!r}] must be a positive integer, got {value!r}")
            caps[name] = value
        body: dict[str, Any] = {"input": text, "background": True, "input_requests": False, **caps,
                                "metadata": {"harness_id": harness_id}}
        if prev is not None:
            body["previous_response_id"] = prev
        elif model:
            body["model"] = model
        else:
            raise ValueError("the first turn of a session needs a model; a later turn needs prev")
        return body

    async def poll(self, response_id: str, *, timeout: float = 3600.0,
                   sleep: Callable[[float], Awaitable[None]] | None = None) -> schema.Response:
        """GET /v1/responses/{id} every `poll_seconds` until it is neither `queued` nor `in_progress`.

        HTTP 503 is asked again. `timeout` bounds the whole wait in seconds; when it runs out an
        HRError with status None is raised and the turn may still be live. `sleep` replaces the
        client's sleep for this call.
        """
        sleep = sleep or self._sleep
        path = f"/v1/responses/{_seg(response_id)}"
        route = f"GET {path}"
        started, slept, last = time.monotonic(), 0.0, "no reply"
        while True:
            try:
                reply = await self._get_json(path, "GET /v1/responses/{id}", schema.Response.from_dict)
            except HRError as e:
                if e.status != 503:
                    raise
                last = "HTTP 503"
                log.debug("%s: 503, asking again", route)
            else:
                if not reply.live:
                    return reply
                last = reply.status
            # Slept seconds count as well as the clock, so an injected sleep still reaches the limit.
            waited = max(time.monotonic() - started, slept)
            if waited >= timeout:
                raise self._error(None, route, detail=f"gave up after {waited:.0f} s, last answer: {last}")
            await sleep(self.poll_seconds)
            slept += self.poll_seconds

    async def put(self, sid: str, path: str, data: bytes | str, *, tries: int = 6, backoff: float = 0.5) -> dict:
        """PUT /v1/sessions/{sid}/files/{path} `{content_b64}`; returns `{session_id, path, bytes, written}`.

        `path` is relative to the workspace root. Data over 4 MiB is refused before anything is
        sent. HTTP 409 (`session_busy`) is tried again after `backoff` seconds, doubling up to 8,
        at most `tries` times in all; the last 409 is then raised.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        parts = path.split("/")
        if not path or any(p in ("", ".", "..") for p in parts) or "\\" in path:
            raise ValueError(f"put needs a plain relative path, got {path!r}")
        url_path = f"/v1/sessions/{_seg(sid)}/files/{quote(path, safe='/')}"
        route = f"PUT {url_path}"
        if len(data) > PUT_LIMIT_BYTES:
            raise self._error(None, route, detail=f"refused before sending: {len(data)} bytes is over the 4 MiB cap")
        body = {"content_b64": base64.b64encode(data).decode("ascii")}
        attempt = 1
        while True:
            try:
                resp = await self._call("PUT", url_path, body=body)
                break
            except HRError as e:
                if e.status != 409 or attempt >= tries:
                    raise
                delay = min(backoff * 2 ** (attempt - 1), 8.0)
                log.debug("%s: 409, try %d of %d, waiting %.1f s", route, attempt, tries, delay)
                await self._sleep(delay)
                attempt += 1
        reply = self._fit(lambda o: schema.validate("PUT_RESULT", o),
                          self._json(resp, route, "PUT /v1/sessions/{sid}/files/{path}"), resp, route)
        if not reply["written"]:
            raise self._error(resp.status_code, route, detail="the service says the file was not written",
                              body=resp.content)
        return reply

    async def turns(self, sid: str) -> list[schema.TurnRow]:
        """GET /v1/sessions/{sid}/turns: the session's turn rows, oldest first.

        The envelope (`session_id`, `last_response_id`) is in `raw["GET /v1/sessions/{sid}/turns"]`.
        """
        reply = await self._get_json(f"/v1/sessions/{_seg(sid)}/turns", "GET /v1/sessions/{sid}/turns",
                                     lambda o: schema.validate("TURNS", o))
        return [schema.TurnRow.from_dict(row) for row in reply["turns"]]

    async def trace(self, sid: str, *, max_chunks: int = 5000) -> list[bytes]:
        """GET /v1/traces/{sid}/events?chunk=n for n = 0, 1, 2, ... until a 404; the chunk bodies, unparsed.

        Each chunk is the CLI's raw stream-json lines. No trace at all gives an empty list.
        """
        chunks: list[bytes] = []
        while True:
            path = f"/v1/traces/{_seg(sid)}/events?chunk={len(chunks)}"
            if len(chunks) >= max_chunks:
                raise self._error(None, f"GET {path}", detail=f"no 404 after {max_chunks} chunks")
            try:
                resp = await self._call("GET", path)
            except HRError as e:
                if e.status == 404:
                    return chunks
                raise
            chunks.append(resp.content)

    async def archive(self, sid: str) -> bytes:
        """GET /v1/sessions/{sid}/files/archive: the workspace as zip bytes, as served."""
        return (await self._call("GET", f"/v1/sessions/{_seg(sid)}/files/archive")).content

    async def watch(self, harness_id: str) -> AsyncIterator[Any]:
        """GET /v1/harnesses/{id}/events: yields the decoded JSON of each server-sent event's `data`.

        Comment and `event:` lines are skipped; `data: [DONE]` ends the stream. Data that is not
        JSON raises HRError, so the live tail can fall back to polling `turns`.
        """
        path = f"/v1/harnesses/{_seg(harness_id)}/events"
        route = f"GET {path}"
        try:
            async with self._http.stream("GET", self.base_url + path, headers={"Accept": "text/event-stream"},
                                         timeout=httpx.Timeout(self._timeout, read=None)) as resp:
                if not resp.is_success:
                    raise self._error(resp.status_code, route, body=await resp.aread())
                data: list[str] = []
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data.append(line[5:].removeprefix(" "))
                    elif line == "" and data:
                        events = self._events(data, route)
                        if events is None:
                            return
                        for event in events:
                            yield event
                        data = []
                if data:        # the stream ended without a closing blank line
                    for event in self._events(data, route) or []:
                        yield event
        except httpx.HTTPError as e:
            raise self._error(None, route, detail=f"{type(e).__name__}: {e}") from None

    # ----------------------------------------------------------------------- plumbing

    async def _call(self, method: str, path: str, *, body: dict | None = None, key: str | None = None) -> httpx.Response:
        """Send one request; return the reply if it is 2xx, else raise HRError."""
        route = f"{method} {path}"
        content = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        for attempt in (1, 2):
            headers = {}
            if content is not None:
                headers["Content-Type"] = "application/json"
            if key and self.idempotency:
                headers["Idempotency-Key"] = key
            try:
                resp = await self._http.request(method, self.base_url + path, content=content, headers=headers)
            except httpx.HTTPError as e:
                # `from None`: the httpx error can quote a header value, and a chained traceback would print it.
                raise self._error(None, route, detail=f"{type(e).__name__}: {e}") from None
            if resp.status_code == 503 and attempt == 1 and IDEMPOTENCY_DOWN in resp.text.lower():
                if self.idempotency:
                    log.warning("%s: idempotency store unavailable; no Idempotency-Key from here on", route)
                self.idempotency = False
                continue
            if resp.is_success:
                return resp
            raise self._error(resp.status_code, route, body=resp.content)
        raise AssertionError("unreachable")

    async def _get_json(self, path: str, template: str, fit: Callable[[Any], Any] | None = None) -> Any:
        route = f"GET {path}"
        resp = await self._call("GET", path)
        reply = self._json(resp, route, template)
        return self._fit(fit, reply, resp, route) if fit else reply

    def _json(self, resp: httpx.Response, route: str, template: str) -> Any:
        try:
            reply = resp.json()
        except ValueError:
            raise self._error(resp.status_code, route, detail="the reply is not JSON", body=resp.content) from None
        self.raw[template] = reply
        return reply

    def _fit(self, fit: Callable[[Any], Any], reply: Any, resp: httpx.Response, route: str) -> Any:
        """Run a schema check or a typed view over a reply; a reply that does not fit is an HRError."""
        try:
            return fit(reply)
        except schema.SchemaError as e:
            raise self._error(resp.status_code, route, detail=f"the reply does not fit: {e}", body=resp.content) from None

    def _events(self, data: list[str], route: str) -> list[Any] | None:
        """One event's `data` lines as JSON values; None for the `[DONE]` mark."""
        text = "\n".join(data)
        if text.strip() == "[DONE]":
            return None
        try:
            return [json.loads(text)]
        except ValueError:
            pass
        try:        # a server that writes one JSON document per data line, with no blank line between
            return [json.loads(line) for line in data if line.strip()]
        except ValueError:
            raise self._error(200, route, detail="event data is not JSON", body=text.encode("utf-8")) from None

    def _error(self, status: int | None, route: str, *, detail: str | None = None, body: bytes | None = None) -> HRError:
        """Build an HRError with the key masked out of everything it carries."""
        if body is not None and self._key:
            for secret in self._secrets():
                body = body.replace(secret.encode("utf-8"), KEY_MASK.encode("ascii"))
        excerpt = "" if body is None else " ".join(body.decode("utf-8", "replace").split())[:EXCERPT_CHARS]
        return HRError(status, self._scrub(route), self._scrub(excerpt),
                       detail=None if detail is None else self._scrub(detail), body=body)

    def _secrets(self) -> list[str]:
        """The key, and its first 16 characters: the prefix the leak scan of section 10 looks for."""
        if not self._key:
            return []
        return [self._key] + ([self._key[:16]] if len(self._key) > 16 else [])

    def _scrub(self, text: str) -> str:
        for secret in self._secrets():
            text = text.replace(secret, KEY_MASK)
        return text


def _seg(value: str) -> str:
    """One URL path segment."""
    return quote(str(value), safe="")
