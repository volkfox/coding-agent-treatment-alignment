"""List the provider connections of a HarnessRouter account and, on request, set them from the environment.

    .venv/bin/python tools/connections.py                    list the account's connections (one GET, nothing changes)
    .venv/bin/python tools/connections.py --dry [options]    print what --set would send, every secret masked (one GET)
    .venv/bin/python tools/connections.py --set [options]    send it (GET, PUT, GET)

    options   --route MODEL=CONNECTION        route a model to a connection; every other route stays (repeatable)
              --model CONNECTION=MODEL[:ID]   list a model on a connection, ID being its name at the provider
                                              (default MODEL), for an endpoint whose models the service does
                                              not find by itself (repeatable)
              --openai-name NAME              the connection OPENAI_API_KEY goes into (default `openai`)
              --openai-provider ID            the provider id of that connection (see below)
              --show-urls                     print base URLs (without credentials or query) in place of [MASKED]

`rj` never writes connections and needs only HR_BASE_URL, HR_API_KEY and TR_API_KEY. A real run also needs every
model of matrix.yaml mapped to a provider connection in the HarnessRouter account; this helper sets those up, as
the HarnessRouter console does by hand. It reads the environment as `rj` does: a variable that is already set
wins, then the lines of .env at the repository root (or of the file RJ_ENV_FILE names).

    HR_BASE_URL, HR_API_KEY                    the account; always needed
    TR_API_KEY          [TR_BASE_URL]          connection `default`,   provider `tokenrouter`
    ANTHROPIC_API_KEY   [ANTHROPIC_BASE_URL]   connection `anthropic`, provider `anthropic`
    OPENAI_API_KEY      [OPENAI_BASE_URL]      connection `openai`,    provider `openai`; any OpenAI-compatible endpoint

A connection is set only if its key is present; the base URL in brackets is optional. The TokenRouter key goes
into the account's first `tokenrouter` connection whatever its name, and into a new one named `default` if there
is none. The OpenAI key goes into the connection named `openai`, or the one --openai-name names. A key that is
shorter than 8 characters, or still a placeholder of .env.example, is refused and nothing is sent.

WARNING: SETTING REPLACES THE ACCOUNT'S WHOLE CONNECTION LIST. HarnessRouter has no call that changes one
connection: PUT /v1/integrations takes the full list, and a connection that is not in it is gone. So --set reads
the list first and sends every connection back, with the keys of the environment put in. A connection whose key
is NOT in the environment goes back as the service returned it, its key masked. This project never confirmed that
the service then keeps the stored key, so have the key of every connection you depend on in the environment, and
run --dry first. The route map is sent back whole as well, with only the --route models moved; an account that
returned no route map gets none back unless a --route is given. A field of the reply that this helper does not
know is not sent back, so it may be lost: the listing names every such field. The scripts this helper replaces
noted that the service keeps the list it replaced for one generation (POST /v1/integrations/restore); this
helper never calls that route, and it was not tried.

What the shapes rest on. Sent to HarnessRouter Cloud by this project's checks of 17 Nov 2026: the two calls; a
`tokenrouter` row with config {base_url, api_key}; an `anthropic` row with config {api_key}; a row for an
OpenAI-compatible endpoint with config {base_url, api_key} and its models listed as {canonical, provider_id}.
Not confirmed with the service: the provider id `openai` (that row was sent under a vendor-specific id; the id
of a connection that already exists is kept, and --openai-provider names another), `base_url` on an `anthropic`
row, a masked key sent back, and a write without a route map by this helper (the plan's first write, by hand,
sent none). Community Edition lists connections under another route and is not covered.

No key is printed, whole or in part: output shows names, booleans and counts, and every `config` value reads
[MASKED]. That includes a connection's base URL, whose host can name a private resource; --show-urls prints
base URLs, still without credentials or query. TokenRouter's public default address is always shown. The one
text of the service that is printed is an excerpt of an error reply, and these are cut from it first: every
secret of the environment, every `config` value the account returned or this run would send (a refusal may echo
the request), and any other run of 32 or more key-like characters.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rj.runner.budget import TR_DEFAULT_BASE, is_loopback  # noqa: E402

PATH = "/v1/integrations"
UA = "rj-preflight/0.1"             # as the checks of 17 Nov 2026 sent it
MASK = "[MASKED]"
ACCOUNT = ("HR_BASE_URL", "HR_API_KEY")
# (the key's variable, the optional base URL's variable, the connection's name, its provider id)
KINDS = (("TR_API_KEY", "TR_BASE_URL", "default", "tokenrouter"),
         ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "anthropic", "anthropic"),
         ("OPENAI_API_KEY", "OPENAI_BASE_URL", "openai", "openai"))
OPENAI = "openai"
ROW_FIELDS = ("name", "provider", "config", "models", "image_models")   # what a connection is sent back with (echo)
READ_ONLY = ("key_hint",)                                               # the service's own; known, and not sent
DOC_FIELDS = ("integrations", "model_map")
KEY_LIKE = re.compile(r"[A-Za-z0-9_-]{32,}")                            # in an error text: cut, whatever it is
NAMES = ACCOUNT + tuple(v for kind in KINDS for v in kind[:2])
SECRET_NAME = re.compile(r"(?i)KEY|TOKEN|SECRET|PASSWORD|PASSWD")       # as rj/cli.py names a secret
MIN_SECRET, KEY_CHARS = 8, 16                                           # as rj/preflight.py scrubs
REPLACES = ("WARNING: PUT /v1/integrations replaces the account's whole connection list and route map. "
            "A connection that is not in the list below is gone after it. One that is kept without its key in the "
            "environment goes back with the masked key the service returned; that the service then keeps the stored "
            "key was not confirmed.")


class Refused(Exception):
    """Nothing was sent; the message says why. Exit code 2."""


def hint_matches(hint: str, key: str) -> bool:
    """Whether a `key_hint` (a masked form the service shows, such as the key's two ends) fits the key."""
    runs = [x for x in re.split(r"[^A-Za-z0-9_-]+", hint or "") if x]
    return bool(runs) and key.startswith(runs[0]) and key.endswith(runs[-1])


def secrets(env: Mapping[str, str]) -> list[str]:
    return sorted({v.strip() for k, v in env.items() if SECRET_NAME.search(k) and len(v.strip()) >= MIN_SECRET}, key=len, reverse=True)


def scrub(text: str, env: Mapping[str, str]) -> str:
    """text without any secret of the environment, whole or by its first 16 characters."""
    for value in secrets(env):
        text = text.replace(value, "[KEY]")
        if len(value) > KEY_CHARS:
            text = text.replace(value[:KEY_CHARS], "[KEY-PREFIX]")
    return text


def strings(value) -> Iterable[str]:
    """Every string in a JSON value, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


def stored_values(rows: list[dict]) -> set[str]:
    """What the connections hold that must not be printed: every string under `config`, and the key hints."""
    out = {s for row in rows for s in strings(row.get("config"))}
    out.update(row["key_hint"] for row in rows if isinstance(row.get("key_hint"), str))
    return {s for s in out if s and s != TR_DEFAULT_BASE}


def written(value: str) -> set[str]:
    """value as a text may carry it: as it is, and as JSON writes it (non-ASCII and `/` escaped or not)."""
    forms = {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
    return forms | {f.replace("/", "\\/") for f in forms}


def hide(text: str, stored: Iterable[str]) -> str:
    """text without the values in `stored`, for the text of an error reply, which may echo the request.

    A value of 8 characters or more is cut wherever it stands, and so are its first 16 characters; of a URL, the
    host and the host with its credentials are cut too. A shorter value is cut only where it stands as a whole
    JSON string. Then every run of 32 or more key-like characters is cut, known or not."""
    whole, short = set(), set()
    for value in stored:
        if not re.match(r"(?i)https?://", value):
            (whole if len(value) >= MIN_SECRET else short).add(value)
            if len(value) > KEY_CHARS:
                whole.add(value[:KEY_CHARS])
            continue
        whole.add(value)
        try:
            parts = urlsplit(value)
            whole.update(piece for piece in (parts.netloc, parts.hostname or "") if len(piece) >= 4)
        except ValueError:
            pass
    for value in sorted(whole, key=len, reverse=True):
        for form in sorted(written(value), key=len, reverse=True):
            text = text.replace(form, MASK)
    for value in short:
        text = text.replace(json.dumps(value), json.dumps(MASK)).replace(json.dumps(value, ensure_ascii=False), json.dumps(MASK))
    return KEY_LIKE.sub(MASK, text)


def http_url(url: str) -> bool:
    """Whether url is an http(s) URL with a host and, where it names a port, a number for it."""
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def is_placeholder(key: str) -> bool:
    """Whether key is one of .env.example's values (`your-tokenrouter-key`), left as `cp .env.example .env` made it."""
    return key.startswith("your-") and key.endswith("-key")


def check_key(var: str, key: str) -> None:
    if is_placeholder(key):
        raise Refused(f"{var} still holds the placeholder of .env.example")
    if len(key) < MIN_SECRET:
        raise Refused(f"{var} is shorter than {MIN_SECRET} characters: that is no key, and too short to be kept out of the output")


def shown_url(url) -> str:
    """A base URL fit to print: scheme, host, port and path; credentials and a query are masked."""
    if not isinstance(url, str):
        return MASK
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return MASK
    if not (parts.scheme and host):
        return MASK
    return (f"{parts.scheme}://{MASK + '@' if '@' in parts.netloc else ''}{host}{':' + str(port) if port else ''}{parts.path}"
            f"{'?' + MASK if parts.query else ''}{'#' + MASK if parts.fragment else ''}")


def shown_base(url, show_urls: bool) -> str:
    """A connection's base URL as printed: masked, unless --show-urls asks for it or it is TokenRouter's public default."""
    return shown_url(url) if show_urls or url == TR_DEFAULT_BASE else MASK


def masked(body: dict, show_urls: bool = False) -> dict:
    """The PUT body fit to print: every `config` value is masked, whatever the field is called (`base_url`: shown_base)."""
    out = copy.deepcopy(body)
    for row in out["integrations"]:
        row["config"] = {k: (shown_base(v, show_urls) if k == "base_url" else MASK) for k, v in row["config"].items()}
    return out


def echo(row: dict) -> dict:
    """A connection as it is sent back: the fields the service takes, as it returned them (`key_hint` is read-only)."""
    out = {"name": row["name"], "provider": row["provider"], "config": dict(row.get("config") or {}), "models": list(row.get("models") or [])}
    if row.get("image_models"):
        out["image_models"] = row["image_models"]
    return out


def check_doc(doc) -> tuple[list[dict], dict | None]:
    """(connections, routes) of a GET /v1/integrations reply, routes being None where the reply holds no route map;
    ValueError when it is not the shape this helper knows."""
    if not isinstance(doc, dict) or not isinstance(doc.get("integrations"), list):
        raise ValueError("no list under `integrations`")
    rows = doc["integrations"]
    for row in rows:
        if not (isinstance(row, dict) and isinstance(row.get("name"), str) and isinstance(row.get("provider"), str)):
            raise ValueError("a connection without a `name` and a `provider`")
        if not isinstance(row.get("config") or {}, dict) or not isinstance(row.get("models") or [], list):
            raise ValueError(f"connection {row['name']!r}: `config` is no object or `models` no list")
    if len({row["name"] for row in rows}) != len(rows):
        raise ValueError("two connections of one name")
    routes = doc.get("model_map")
    if routes is not None and not (isinstance(routes, dict) and all(isinstance(v, str) for v in routes.values())):
        raise ValueError("`model_map` is no map of model to connection name")
    return rows, (None if routes is None else dict(routes))


def unsent(doc: dict) -> list[str]:
    """One line per place where a checked reply holds a field that --set does not send back; names only."""
    lines = []
    top = sorted(k for k in doc if k not in DOC_FIELDS)
    if top:
        lines.append(f"  note: the reply's field(s) {top} are not sent back by --set: this helper does not know them")
    for row in doc["integrations"]:
        extra = sorted(k for k in row if k not in ROW_FIELDS + READ_ONLY)
        if extra:
            lines.append(f"  note: connection {row['name']!r} has field(s) {extra} that --set does not send back: this helper does not know them")
    return lines


def check_base(var: str, url: str, hr_base: str) -> None:
    if not http_url(url):
        raise Refused(f"{var} is no http(s) URL")
    if is_loopback(url) and not is_loopback(hr_base):
        raise Refused(f"{var} points at this machine and HR_BASE_URL does not: a stub address left over from offline work "
                      f"would be written into the account. Unset {var} or correct it.")


def pairs(items: list[str], flag: str) -> list[tuple[str, str]]:
    out = []
    for item in items:
        left, eq, right = item.partition("=")
        if not (eq and left.strip() and right.strip()):
            raise Refused(f"{flag} takes {'MODEL=CONNECTION' if flag == '--route' else 'CONNECTION=MODEL[:ID]'}, got {item!r}")
        out.append((left.strip(), right.strip()))
    return out


def target(rows: list[dict], kind: tuple, openai_provider: str | None, openai_name: str | None = None) -> tuple[dict, bool]:
    """(the connection a key goes into, whether it is new). A new one is appended to rows."""
    _, _, name, provider = kind
    if kind[2] == OPENAI:
        name = openai_name or name
    named = next((r for r in rows if r["name"] == name), None)
    if provider == "tokenrouter":
        row = next((r for r in rows if r["provider"] == provider), None)
        if row is not None:
            return row, False
    elif kind[2] == OPENAI:
        if named is not None and named["provider"] in [k[3] for k in KINDS if k is not kind]:
            raise Refused(f"connection {name!r} is of provider {named['provider']!r}: the key of {kind[0]} does not go into it")
        provider = openai_provider or (named["provider"] if named else provider)
        if named is not None:
            named["provider"] = provider
            return named, False
    elif named is not None and named["provider"] == provider:
        return named, False
    if named is not None:
        raise Refused(f"the account has a connection named {name!r} of provider {named['provider']!r}, not {provider!r}: "
                      f"rename or remove it in the HarnessRouter console first")
    row = {"name": name, "provider": provider, "config": {"base_url": TR_DEFAULT_BASE} if provider == "tokenrouter" else {}, "models": []}
    rows.append(row)
    return row, True


def plan(existing: list[dict], routes: dict | None, env: Mapping[str, str], *, route: list[str] = (), model: list[str] = (),
         openai_provider: str | None = None, openai_name: str | None = None) -> tuple[dict, list[str]]:
    """(the PUT body, one line per connection saying what happens to it). Raises Refused; sends nothing.
    `routes`: the route map as read, None where the reply held none; the body then carries one only for a --route."""
    rows = [echo(r) for r in existing]
    hr_base = env.get("HR_BASE_URL", "")
    source: dict[str, str] = {}
    notes: list[str] = []
    for kind in KINDS:
        key_var, base_var, _, _ = kind
        key, base = env.get(key_var, "").strip(), env.get(base_var, "").strip()
        if not key:
            if base and base_var != "TR_BASE_URL":          # `rj` reads TR_BASE_URL too, so alone it means nothing here
                notes.append(f"  note: {base_var} is set and {key_var} is not; no connection is set from it")
            continue
        check_key(key_var, key)
        if base:
            check_base(base_var, base, hr_base)
        row, new = target(rows, kind, openai_provider, openai_name)
        if row["name"] in source:
            raise Refused(f"{key_var} would go into connection {row['name']!r}, which another key of the environment already sets")
        row["config"]["api_key"] = key
        if base:
            row["config"]["base_url"] = base
        source[row["name"]] = (f"{'new' if new else 'key replaced'}, from {key_var}"
                               + (f" and {base_var}" if base else ""))
        if new and kind[2] == OPENAI and not openai_provider:
            notes.append(f"  note: provider id {row['provider']!r} for the new connection {row['name']!r} is this helper's default and "
                         f"was not confirmed with the service; --openai-provider ID sets another")
    by = {r["name"]: r for r in rows}
    for name, spec in pairs(list(model), "--model"):
        if name not in by:
            raise Refused(f"--model: no connection named {name!r} (have {sorted(by)})")
        canonical, _, provider_id = spec.partition(":")
        entry = {"canonical": canonical, "provider_id": provider_id or canonical}
        by[name]["models"] = [m for m in by[name]["models"] if not (isinstance(m, dict) and m.get("canonical") == canonical)] + [entry]
    moves = dict(pairs(list(route), "--route"))
    for m, name in moves.items():
        if name not in by:
            raise Refused(f"--route: cannot route {m}: no connection named {name!r} (have {sorted(by)})")
    if not (source or moves or model):
        raise Refused("nothing to set: none of " + ", ".join(k[0] for k in KINDS) + " is in the environment, and no --route or --model")
    kept = "kept as the service returned it; its key is not in the environment, so the masked value goes back"
    lines = [f"  {r['name']} ({r['provider']}): {source.get(r['name'], kept)}; {len(r['models'])} model(s) listed" for r in rows]
    body: dict = {"integrations": rows}
    if routes is not None or moves:         # a reply without a route map gets none back, as the plan's first write sent none
        body["model_map"] = {**(routes or {}), **moves}
    return body, lines + notes


def describe(rows: list[dict], routes: dict, env: Mapping[str, str], show_urls: bool = False, openai_name: str | None = None) -> list[str]:
    """One line per connection of a reply: names, counts and booleans only."""
    mine = {}
    for key_var, _, name, provider in KINDS:
        name = (openai_name or name) if name == OPENAI else name
        row = (next((r for r in rows if r["provider"] == provider), None) if provider == "tokenrouter"
               else next((r for r in rows if r["name"] == name), None))
        if row is not None:
            mine.setdefault(row["name"], key_var)
    lines = []
    for r in rows:
        config = r.get("config") or {}
        var = mine.get(r["name"])
        key = env.get(var, "").strip() if var else ""
        holds = ("no variable of this helper belongs to it" if not var else f"{var} is not set" if not key
                 else f"its key hint fits {var}: {hint_matches(str(r.get('key_hint') or ''), key)}")
        lines.append(f"  {r['name']} ({r['provider']}): config fields {sorted(config)}"
                     + (f", base_url {shown_base(config['base_url'], show_urls)}" if "base_url" in config else "")
                     + f"; {len(r.get('models') or [])} model(s) listed, {sum(1 for v in routes.values() if v == r['name'])} routed to it; {holds}")
    return lines


def main(argv: list[str] | None = None, *, env: Mapping[str, str] | None = None,
         transport: httpx.BaseTransport | None = None, out: Callable[[str], None] = print) -> int:
    """`env`: the environment to use in place of the process's and the .env file (tests). `transport`: the HTTP layer."""
    ap = argparse.ArgumentParser(description="List the HarnessRouter account's provider connections; with --set, replace the "
                                             "whole list with one that holds the keys of the environment.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry", action="store_true", help="print what --set would send, every secret masked; sends one GET")
    mode.add_argument("--set", action="store_true", help="replace the account's connection list (GET, PUT, GET)")
    ap.add_argument("--route", action="append", default=[], metavar="MODEL=CONNECTION",
                    help="route a model to a connection; every other route stays (repeatable)")
    ap.add_argument("--model", action="append", default=[], metavar="CONNECTION=MODEL[:ID]",
                    help="list a model on a connection, ID being its name at the provider (default: MODEL; repeatable)")
    ap.add_argument("--openai-name", metavar="NAME", help="the connection OPENAI_API_KEY goes into (default: openai)")
    ap.add_argument("--openai-provider", metavar="ID", help="provider id of that connection (default: the id it has, else openai)")
    ap.add_argument("--show-urls", action="store_true", help="print base URLs, without credentials or query, in place of [MASKED]")
    args = ap.parse_args(argv)

    loaded = None
    if env is None:
        from rj import cli
        loaded = cli.load_env()
        env = os.environ

    def say(text: str) -> None:
        out(scrub(text, env))

    stored: set[str] = set()        # every config value the account returned or this run would send: a reply may echo one

    def excerpt(text: str, limit: int) -> str:      # scrubbed before it is cut, so that no cut leaves a part of a key
        return hide(scrub(" ".join(text.split()), env), stored)[:limit]

    if loaded is not None:
        kept = [n for n in NAMES if n in loaded["kept"]]
        say(f"environment: {len([n for n in NAMES if n in loaded['set']])} of this helper's variables come from the env file"
            + (f"; already set in the shell, so the file's value is not used: {', '.join(kept)}" if kept else ""))
    hr, hr_key = env.get("HR_BASE_URL", "").strip().rstrip("/"), env.get("HR_API_KEY", "").strip()
    if not (hr and hr_key):
        say("HR_BASE_URL and HR_API_KEY are both needed; missing: " + ", ".join(n for n in ACCOUNT if not env.get(n, "").strip()))
        return 2
    if not http_url(hr):
        say("HR_BASE_URL is no http(s) URL")
        return 2
    if not (hr_key.isascii() and hr_key.isprintable()):
        say("HR_API_KEY holds characters a header cannot carry")
        return 2
    try:
        check_key("HR_API_KEY", hr_key)
    except Refused as e:
        say(f"refused, nothing was sent: {e}")
        return 2
    if (args.route or args.model or args.openai_provider) and not (args.dry or args.set):
        say("--route, --model and --openai-provider go with --dry or --set")
        return 2
    say("keys in the environment: " + ", ".join(f"{k[0]} {'set' if env.get(k[0], '').strip() else 'not set'}" for k in KINDS))

    with httpx.Client(transport=transport, headers={"Authorization": f"Bearer {hr_key}", "User-Agent": UA}) as http:
        def read() -> tuple[list[dict], dict | None, list[str], str] | None:
            try:
                r = http.get(hr + PATH, timeout=40)
            except (httpx.HTTPError, httpx.InvalidURL) as e:
                say(f"GET {PATH}: no reply ({type(e).__name__})")
                return None
            if r.status_code != 200:
                say(f"GET {PATH}: HTTP {r.status_code}: {excerpt(r.text, 300)}")
                return None
            try:
                doc = r.json()
                found, found_routes = check_doc(doc)
            except ValueError as e:
                say(f"GET {PATH}: not the shape this helper knows ({e})")
                return None
            stored.update(stored_values(found))
            return found, found_routes, unsent(doc), r.text

        before = read()
        if before is None:
            say("nothing was sent")
            return 1
        rows, routes_read, not_sent, _ = before
        routes = routes_read or {}
        say(f"the account at {shown_url(hr)} holds {len(rows)} connection(s) and {len(routes)} route(s):")
        for line in describe(rows, routes, env, args.show_urls, args.openai_name) + not_sent:
            say(line)
        if not (args.dry or args.set):
            return 0

        try:
            body, lines = plan(rows, routes_read, env, route=args.route, model=args.model, openai_provider=args.openai_provider,
                               openai_name=args.openai_name)
        except Refused as e:
            say(f"refused, nothing was sent: {e}")
            return 2
        stored.update(stored_values(body["integrations"]))
        moved = {m: c for m, c in body.get("model_map", {}).items() if routes.get(m) != c}
        say(REPLACES)
        say(f"{'would send' if args.dry else 'sending'} {len(body['integrations'])} connection(s) and "
            + (f"{len(body['model_map'])} route(s):" if "model_map" in body else "no route map (the account returned none):"))
        for line in lines:
            say(line)
        say(f"  routes moved: {moved or 'none'}")
        if args.dry:
            say(f"PUT {PATH} would carry this body (config values masked):")
            say(json.dumps(masked(body, args.show_urls), indent=1, ensure_ascii=False))
            say("dry: nothing was sent")
            return 0

        try:
            put = http.put(hr + PATH, json=body, timeout=60)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            say(f"PUT {PATH}: no reply ({type(e).__name__}); read the list again before anything else")
            return 1
        say(f"PUT {PATH}: HTTP {put.status_code}")
        if put.status_code >= 300:
            say(f"  body: {excerpt(put.text, 500)}")
            return 1
        after = read()
        if after is None:
            say("the list was sent, but reading it back failed")
            return 1
        rows_after, routes_after, _, text_after = after
        routes_after = routes_after or {}
        say(f"the account now holds {len(rows_after)} connection(s) and {len(routes_after)} route(s):")
        for line in describe(rows_after, routes_after, env, args.show_urls, args.openai_name):
            say(line)
        gone = sorted({r["name"] for r in rows} - {r["name"] for r in rows_after})
        changed = sorted(m for m in set(routes) | set(routes_after) if routes.get(m) != routes_after.get(m))
        say(f"  connections gone: {gone or 'none'}; routes changed by this write: {[(m, routes.get(m), routes_after.get(m)) for m in changed] or 'none'}")
        say(f"  a full key in any reply: {any(s in put.text or s in text_after for s in secrets(env))}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
