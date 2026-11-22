"""The cost guard and the one ledger. Plan section 11 ("Cost guard") and section 14 ("The rule").

Files, all beside each other in the runs directory and shared by every run:
    ledger.jsonl    one line per paid thing, appended and fsynced, never rewritten
    measured.json   {row: {session_usd, minutes, input_includes_cache, ...}}: what the row's first neutral
                    session cost and took. It overrides prices.yaml's `measured`; prices.yaml is never written.

Ledger lines:
    {"kind": "turn", "run", "session", "attempt", "ep", "send", "response_id", "row", "model",
     "payer", "provider", "usage", "price", "input_includes_cache", "cost_usd", "flag", "service", "ts"}
    {"kind": "tr_usage", "run", "where", "session", "cents", "base", "service", "ts"}      a reading of TokenRouter's usage figure
A line of any other kind (a preflight call, a judge call, a call of the adaptive writer: `Ledger.append`) counts
through its `cost_usd`, and is taken as paid on TokenRouter when its `provider` is "tokenrouter". The adaptive
writer's line (machine.Session.adapt) is
    {"kind": "adaptive", "run", "session", "attempt", "ep", "send", "model", "slug", "provider": "tokenrouter",
     "usage", "cost_usd", "flag", "written", "service", "ts"}
one per call, appended as the call returns. `written` says whether the call gave a text that passed its checks.
It does not say that the text was sent: that is in the session's journal (a FALLBACK_SENT line whose
`message_kind` is "adaptive"), and a call made twice for one send, after a kill, is two lines and one answer.

`service` is the HarnessRouter base URL the line was written for (`service_of`). A line stamped with another
service than the one this process talks to is not this account's money and counts for nothing here: a run
against the local stub never moves the real run's guard, even if both were given one runs directory. A line
with no stamp (written by hand, or before stamps) counts. measured.json entries, <runs>/agent_work.json and
<runs>/preflight.json carry the same stamp and are read the same way (`same_service`).

`payer` is the connection name the model maps to in GET /v1/integrations `model_map`, read once per run;
`provider` is that connection's provider. `flag` is "unpriced" (prices.yaml has no entry for the model: no
cost; the guard refuses such a row, so this is only seen when a price was removed mid-run), "no_usage" (the
turn reported none, or its reply could never be had: costed at the row's mean turn cost), "orphan" (a seed
POST that got no reply may have opened a session nobody will read: one seed turn is assumed) or null.

What has been spent (measured 17 Nov 2026: for TokenRouter the usage figure is the truth, the ledger an
estimate that overstated Codex nearly threefold; for the other payers only the estimate exists):
    TokenRouter payers   the rise of the usage figure from the ledger's first reading to its last, plus the
                         ledger's estimate for what was ledgered before the first reading and after the last;
    every other payer    the ledger's estimate.
With no reading at all (the route is missing or fails) TokenRouter spending is the ledger's estimate too. The
same holds when the figure went down, or stood still while the ledger shows TokenRouter turns: then the
figure is not tracking the key that pays, and the dearer estimate is the safe one. Only readings taken at this
run's TR_BASE_URL are compared, so a run against the stub and a live run can share a ledger.

The guard: a session's projection is measured[row].session_usd (est_session_usd before the row has one) times
rerun_factor, plus the judge's share (`Budget.judge_usd`): 0.35, an estimate for a transcript at the cap, until
this service's ledger holds a judged session, and from then on what the dearest judged session has cost (on
17 Nov 2026 the two judged sessions cost 0.06 and 0.03 USD). The share is that of the judge the profile asks
(`Budget(judge=...)`): only the sessions that judge has judged count, by the model and provider on their
ledger lines, and for a judge asked at Anthropic the estimate is worked out from its prices (`judge_estimate`;
2.48 USD for Claude Opus 5.5 at the demo profile's cap). What a cheaper judge cost says nothing about a dearer
one. remaining = total_usd - spent, or if lower the wallet figure passed in
minus TokenRouter spending since this run started; less floor_usd. A stage starts only if the projections of
its unfinished sessions sum to at most `remaining`; each session start, and each infra rerun, rechecks its
own, with what the sessions in flight are still expected to cost held back. A session that has already
started is asked only for what its projection still leaves open (`outstanding`): what its running attempt
has cost is in `spent`. A row whose model has no entry in prices.yaml is refused outright: its turns could
not be costed, and for a payer that is not TokenRouter nothing else would count them.

Between the turns of a session the guard is asked again (`exhausted`, from machine.Session.post): once what
has been spent reaches total_usd, or the wallet figure is used up, no further message is posted and the
session is left as its journal stands. floor_usd is what a session in flight may still eat; total_usd is
where posting ends.

Check 11's rule ("ledger within 20% of the wallet's fall under one reading of input_tokens, else stop") is
applied as a warning, not a stop: `measure` records whether the closer reading of the token counts is within
20% of the usage figure's rise (`ledger_within_20pct`), and the runner says so loudly. It does not stop the
row, because for a row measured this way the usage figure is what the guard goes by from then on (17 Nov
2026: the ledger overstated Codex nearly threefold while the figure was right to the hundredth of a cent).
"""
from __future__ import annotations

import ipaddress
import os
import time
from pathlib import Path
from urllib.parse import urlsplit
from typing import Awaitable, Callable

import httpx

from rj import pricing, schema
from rj.grade.judge import ANTHROPIC, DEFAULT_MAX_TOKENS, JudgeError, judge_model, judge_route
from rj.hr import HRError
from rj.runner.manifest import append_lines, read_json, read_lines, write_json

LEDGER, MEASURED = "ledger.jsonl", "measured.json"
JUDGE_USD = 0.35                            # per session, the judge's calls on a transcript at the cap (plan section 9, an estimate
                                            # made for TokenRouter's judge; `judge_usd` goes by the ledger once a session is judged)
JUDGE_CHARS, JUDGE_CHARS_PER_TOKEN = 600_000, 3     # `judge_estimate`: the cap where the profile sets none; a token, read the dearer way
JUDGE_OUTPUT_TOKENS = 8_000                 # `judge_estimate`: per call of an Anthropic judge, its reply and the thinking billed with it
TOKENROUTER = "tokenrouter"
TR_DEFAULT_BASE = "https://api.tokenrouter.com/v1"
TR_USAGE_PATH = "/dashboard/billing/usage"  # {"total_usage": 9.47}, in cents, for the key that asks
DISTRUST_USD, DISTRUST_RATIO = 0.05, 10     # the figure is not trusted when it moved less than a tenth of the ledger
LEDGER_TOLERANCE = 0.20                     # plan check 11: the ledger within 20% of what the usage figure says
TURNS_PER_SESSION = 8                       # seed, six entry points, canary: for a turn's share of an estimate
EPS = 1e-9


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tr_base() -> str:
    return (os.environ.get("TR_BASE_URL") or TR_DEFAULT_BASE).rstrip("/")


def is_loopback(url: str | None) -> bool:
    """True when a base URL points at this machine (the local stub)."""
    host = urlsplit(url or "").hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def service_of(url: str | None) -> str | None:
    """A HarnessRouter base URL as the name of a service: scheme, host, port and path, lower case, `localhost`
    as 127.0.0.1. Two stubs on two ports are two services; so are the stub and the real one."""
    if not url or not str(url).strip():
        return None
    parts = urlsplit(str(url).strip())
    host = (parts.hostname or "").lower()
    host = "127.0.0.1" if host == "localhost" else host
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        port = ""
    return f"{parts.scheme.lower()}://{host}{port}{parts.path.rstrip('/')}"


def current_service() -> str | None:
    """The service this process talks to, by HR_BASE_URL."""
    return service_of(os.environ.get("HR_BASE_URL"))


def same_service(entry, service: str | None) -> bool:
    """True for an entry stamped with this service, and for one that carries no stamp."""
    stamp = entry.get("service") if isinstance(entry, dict) else None
    return stamp is None or service is None or stamp == service


def judge_estimate(judge: dict | None, prices: dict) -> float:
    """What the judge's calls on one session are expected to cost, in USD, before the ledger holds a session that
    judge has judged. `judge` is the profile's judge block.

    JUDGE_USD for TokenRouter's judge and the stub's, and where no block is given. For a judge asked at Anthropic
    the figure is worked out from its prices.yaml entry and the block, for a transcript at the cap
    (`max_input_chars`, three characters to a token): the first scoring run writes its prompt to the cache where
    a second run follows (else it is plain input), every further run reads it, the guess call sends it once more
    as plain input, and every call, the fit call among them, is allowed JUDGE_OUTPUT_TOKENS of output (the
    thinking is billed as output), or `max_tokens` where that is less. The fit call's own prompt is short and is
    not counted. An estimate on the dear side: a transcript is rarely near the cap."""
    judge = judge or {}
    try:
        if judge_route(judge)[0] != ANTHROPIC:
            return JUDGE_USD
        price = pricing.price_from_yaml(judge_model(judge, prices)[1])
    except JudgeError:                                  # a judge that cannot be asked as the block stands
        return JUDGE_USD
    runs, guess = max(1, int(judge.get("runs", 2))), bool(judge.get("guess_call"))
    tokens = int(judge.get("max_input_chars") or JUDGE_CHARS) / JUDGE_CHARS_PER_TOKEN
    sent = tokens * ((price.cache_write if runs > 1 else price.input) + (runs - 1) * price.cache_read + (price.input if guess else 0.0))
    got = (runs + guess + 1) * min(JUDGE_OUTPUT_TOKENS, int(judge.get("max_tokens") or DEFAULT_MAX_TOKENS)) * price.output
    return (sent + got) / 1_000_000


def judge_lines(judge: dict | None, prices: dict) -> tuple[str, str] | None:
    """(model, provider) as the ledger's judge lines name the judge a judge block asks for. None where no block is
    given, for the stub's judge (it is ledgered nowhere) and for a block that names no judge that can be asked:
    then every judge line counts, as it did when there was one judge."""
    if not judge or judge.get("stub"):
        return None
    try:
        return judge_model(judge, prices)[0], judge_route(judge)[0]
    except JudgeError:
        return None


async def no_tr_usage() -> None:
    """Stands in for the usage route where it must not be asked (a stub run with the real TR_BASE_URL)."""
    return None


def prices_with_measured(runs_dir: str | Path, prices: dict | None = None, service: str | None = None) -> dict:
    """The price book with <runs>/measured.json merged into `measured`: what `rj.aggregate.aggregate(prices=...)`
    needs to say which rows were measured, and from what. prices.yaml itself is never written. An entry
    stamped with another service than `service` (default: HR_BASE_URL's) is left out."""
    book = dict(prices if prices is not None else schema.load_prices())
    own = read_json(Path(runs_dir) / MEASURED)
    service = service if service is not None else current_service()
    mine = {row: e for row, e in own.items() if same_service(e, service)} if isinstance(own, dict) else {}
    book["measured"] = {**(book.get("measured") or {}), **mine}
    return book


async def fetch_tr_usage(base_url: str | None = None, api_key: str | None = None, *,
                         transport: httpx.AsyncBaseTransport | None = None, timeout: float = 20.0) -> float | None:
    """TokenRouter's usage figure for the key, in cents; None when it cannot be read. Never raises.

    `TR_BASE_URL` and `TR_API_KEY` are read from the environment when not passed. Without a key the real
    service is not asked at all.
    """
    base = base_url.rstrip("/") if base_url else tr_base()
    key = (api_key if api_key is not None else os.environ.get("TR_API_KEY") or "").strip()
    if not key and base == TR_DEFAULT_BASE:
        return None
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as http:
            reply = await http.get(base + TR_USAGE_PATH, headers={"Authorization": f"Bearer {key}"} if key else {})
        if reply.status_code != 200:
            return None
        value = reply.json().get("total_usage")
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _usd(entry: dict) -> float:
    value = entry.get("cost_usd")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


class Ledger:
    """<runs>/ledger.jsonl. Lines are appended whole and fsynced; nothing is ever rewritten.

    service: the HarnessRouter base URL as `service_of` names it (default: HR_BASE_URL's). Every line appended
    is stamped with it; `mine()` gives the lines that are this service's, or carry no stamp."""

    def __init__(self, path: str | Path, service: str | None = None):
        self.path = Path(path)
        self.service = service if service is not None else current_service()
        self._turns = {e["response_id"]: e for e in self.entries() if e.get("kind") == "turn" and e.get("response_id")}

    def entries(self) -> list[dict]:
        return read_lines(self.path)

    def mine(self) -> list[dict]:
        return [e for e in self.entries() if same_service(e, self.service)]

    def append(self, entry: dict) -> dict:
        """Write one line. Any package may call this for a paid call of its own (kind "judge", "preflight", ...):
        give it `cost_usd`, and `provider: "tokenrouter"` when TokenRouter's usage figure covers it."""
        entry = {**entry, "ts": entry.get("ts") or now()}
        if self.service is not None:
            entry.setdefault("service", self.service)
        append_lines(self.path, [entry])
        if entry.get("kind") == "turn" and entry.get("response_id"):
            self._turns[entry["response_id"]] = entry
        return entry

    def turn(self, response_id: str) -> dict | None:
        """The line already written for a response, so a turn classified twice (a resume) is ledgered once."""
        return self._turns.get(response_id)


class Budget:
    def __init__(self, cfg: dict, prices: dict, runs_dir: str | Path, *, run: str, wallet_usd: float | None = None,
                 tr_usage: Callable[[], Awaitable[float | None]] | None = None, log: Callable[[str], None] | None = None,
                 judge: dict | None = None):
        self.total, self.floor, self.rerun = float(cfg["total_usd"]), float(cfg["floor_usd"]), float(cfg["rerun_factor"])
        self.prices, self.runs_dir, self.run = prices, Path(runs_dir), run
        self.judge = judge                          # the profile's judge block: whose share `judge_usd` gives
        self.wallet = None if wallet_usd is None else float(wallet_usd)
        self.ledger = Ledger(self.runs_dir / LEDGER)
        self.model_map: dict[str, str] = {}
        self.providers: dict[str, str] = {}
        self.unpriced: set[str] = set()
        self._tr_usage = tr_usage or fetch_tr_usage
        self.tr_base = tr_base()                    # readings of another base are another key's figure
        self._log = log or (lambda message: None)
        self._mark = len(self.ledger.mine())        # where this run's part of the ledger starts
        self._route_down = False
        self._judge: tuple[float, str] | None = None        # `judge_usd`, read off the ledger once per process

    @property
    def service(self) -> str | None:
        """The service whose money this guard counts (the ledger's stamp)."""
        return self.ledger.service

    # ----------------------------------------------------------------------- once per run

    async def start(self, client) -> None:
        """Read who pays for which model, and take the run's first reading of TokenRouter's usage figure."""
        try:
            doc = await client.integrations()
        except HRError as e:
            self._log(f"budget: integrations not read ({e}); every payer is unknown and costed from the ledger")
            doc = {}
        self.model_map = {str(k): str(v) for k, v in (doc.get("model_map") or {}).items()}
        self.providers = {i["name"]: i["provider"] for i in doc.get("integrations") or []}
        self.ledger.service = service_of(getattr(client, "base_url", None)) or self.ledger.service
        self._mark = len(self.ledger.mine())
        await self.read_usage("run_start")

    def payer(self, model: str) -> tuple[str | None, str | None]:
        """(connection name, its provider) for a model; (None, None) when the map does not name it."""
        name = self.model_map.get(model)
        return name, self.providers.get(name) if name else None

    async def read_usage(self, where: str, session: str | None = None) -> float | None:
        """Read TokenRouter's usage figure (cents) and ledger it. None, and no line, when it cannot be read."""
        cents = await self._tr_usage()
        if cents is None:
            if not self._route_down:
                self._log("budget: TokenRouter's usage figure is not readable; spending is costed from the ledger")
            self._route_down = True
            return None
        self._route_down = False
        self.ledger.append({"kind": "tr_usage", "run": self.run, "where": where, "session": session, "cents": cents,
                            "base": self.tr_base})
        return cents

    # ----------------------------------------------------------------------- prices

    def measured(self) -> dict:
        """Per row what its first neutral session cost: prices.yaml's `measured`, overridden by measured.json."""
        own = read_json(self.runs_dir / MEASURED)
        mine = {row: e for row, e in own.items() if same_service(e, self.service)} if isinstance(own, dict) else {}
        return {**(self.prices.get("measured") or {}), **mine}

    def price_entry(self, model: str) -> dict | None:
        return (self.prices.get("tokenrouter") or {}).get(model)

    def includes_cache(self, row_id: str) -> bool:
        return bool((self.measured().get(row_id) or {}).get("input_includes_cache"))

    def estimate(self, model: str) -> float | None:
        """prices.yaml's estimate of one session of a model; None when the model has no entry."""
        entry = self.price_entry(model)
        if entry is None:
            return None
        return float(entry.get("est_session_usd") or pricing.est_session_usd(pricing.price_from_yaml(entry)))

    def priced(self, row: dict) -> bool:
        """False for a row whose model has no entry in prices.yaml: the guard refuses it (`check`)."""
        if self.price_entry(row["model"]) is not None:
            return True
        if row["model"] not in self.unpriced:
            self._log(f"budget: prices.yaml has no entry for {row['model']!r}: row {row['id']} is refused until it has one")
        self.unpriced.add(row["model"])
        return False

    def judge_usd(self) -> tuple[float, str]:
        """(the judge's share of a session's projection in USD, where the figure is from).

        "estimate": `judge_estimate` (JUDGE_USD for TokenRouter's judge), while this service's ledger holds no
        session judged by the judge the profile asks. "ledger": what the dearest session that judge has judged
        cost, all its judge lines together (a session judged in two passes has two). A line of another judge
        (another model, or another provider) is left out: a session that TokenRouter's judge read for 0.06 USD
        says nothing of what Claude Opus 5.5 costs. So are lines that name no session (calls that left no
        judge.json). Without a judge block (`Budget(judge=...)`), and for the stub's judge, every judge line
        counts. Read once per process: `rj judge` is another process, and the next call of the runner sees what
        it ledgered."""
        if self._judge is None:
            who = judge_lines(self.judge, self.prices)
            per: dict[tuple, float] = {}
            for e in self.ledger.mine():
                if e.get("kind") == "judge" and e.get("session") and not e.get("flag") \
                        and (who is None or (e.get("model"), e.get("provider")) == who):
                    per[e.get("run"), e["session"]] = per.get((e.get("run"), e["session"]), 0.0) + _usd(e)
            dearest = max(per.values(), default=0.0)
            self._judge = (dearest, "ledger") if dearest > 0 else (judge_estimate(self.judge, self.prices), "estimate")
        return self._judge

    def projection(self, row: dict) -> float:
        """What one more session of a row is expected to cost, reruns and the judge included. For a row
        that is not `priced` this is the judge's cost alone, and `check` refuses the row."""
        measured = self.measured().get(row["id"])
        if measured and self.priced(row):
            base = float(measured["session_usd"])
        else:
            base = (self.estimate(row["model"]) or 0.0) if self.priced(row) else 0.0
        return base * self.rerun + self.judge_usd()[0]

    def outstanding(self, row: dict, session: str, attempt: int | None = None) -> float:
        """What a session that has started is still expected to cost: its projection less what its running
        attempt has cost by the ledger (that part is in `spent` already)."""
        return max(0.0, self.projection(row) - self.session_cost(session, attempt))

    # ----------------------------------------------------------------------- the ledger

    def record_turn(self, *, session: str, attempt: int, ep: str, send: int, response_id: str | None, row: dict,
                    usage: dict | None, flag: str | None = None) -> dict:
        """Ledger one turn (once per response id) and return its line; `cost_usd` is None only for a model
        without a price. A turn without usage, or whose usage does not say how many tokens went in and came
        out (a null count), is costed at the row's mean turn cost (an "orphan": the mean of the row's turns
        at the same entry point, where it has any; before the row has a costed turn: an eighth of its
        session estimate) and flagged "no_usage", or `flag` when given."""
        seen = self.ledger.turn(response_id) if response_id else None
        if seen is not None:
            return seen
        payer, provider = self.payer(row["model"])
        entry, includes = self.price_entry(row["model"]), self.includes_cache(row["id"])
        cost = None
        if entry is None:
            flag = "unpriced"
        elif not schema.usage_counted(usage):
            flag = flag or "no_usage"
            costed = [e for e in self.ledger.mine()
                      if e.get("kind") == "turn" and e.get("row") == row["id"] and e.get("cost_usd") is not None and not e.get("flag")]
            alike = [e for e in costed if e.get("ep") == ep]
            known = [_usd(e) for e in (alike if flag == "orphan" and alike else costed)]
            cost = sum(known) / len(known) if known else (self.estimate(row["model"]) or 0.0) / TURNS_PER_SESSION
        else:
            cost = pricing.turn_cost_usd(usage, pricing.price_from_yaml(entry), includes)
        return self.ledger.append({
            "kind": "turn", "run": self.run, "session": session, "attempt": attempt, "ep": ep, "send": send,
            "response_id": response_id, "row": row["id"], "model": row["model"], "payer": payer, "provider": provider,
            "usage": usage or None, "price": entry, "input_includes_cache": includes, "cost_usd": cost, "flag": flag})

    def session_turns(self, session: str, attempt: int | None = None) -> list[dict]:
        return [e for e in self.ledger.mine() if e.get("kind") == "turn" and e.get("session") == session
                and e.get("run") == self.run and (attempt is None or e.get("attempt") == attempt)]

    def session_cost(self, session: str, attempt: int | None = None) -> float:
        return sum(_usd(e) for e in self.session_turns(session, attempt))

    # ----------------------------------------------------------------------- what has been spent

    def _tokenrouter(self, entries: list[dict], lo: int = 0) -> tuple[float, str]:
        """TokenRouter spending over entries[lo:], and where the figure came from ("tr_usage" or "ledger")."""
        def ledgered(a: int, b: int) -> float:
            return sum(_usd(e) for e in entries[a:b] if e.get("provider") == TOKENROUTER)

        readings = [i for i in range(lo, len(entries)) if entries[i].get("kind") == "tr_usage"
                    and isinstance(entries[i].get("cents"), (int, float)) and entries[i].get("base") == self.tr_base]
        if readings:
            first, last = readings[0], readings[-1]
            rise = (entries[last]["cents"] - entries[first]["cents"]) / 100
            between = ledgered(first, last)
            if rise >= 0 and not (between > DISTRUST_USD and rise < between / DISTRUST_RATIO):
                return ledgered(lo, first) + rise + ledgered(last, len(entries)), "tr_usage"
        return ledgered(lo, len(entries)), "ledger"

    def spent(self) -> dict:
        """{"usd", "tokenrouter_usd", "other_usd", "source", "since_start_tokenrouter_usd"} over this service's
        part of the ledger (every run of it)."""
        entries = self.ledger.mine()
        tokenrouter, source = self._tokenrouter(entries)
        other = sum(_usd(e) for e in entries if e.get("provider") != TOKENROUTER)
        since, _ = self._tokenrouter(entries, self._mark)
        return {"usd": tokenrouter + other, "tokenrouter_usd": tokenrouter, "other_usd": other, "source": source,
                "since_start_tokenrouter_usd": since}

    def left(self) -> float:
        """What is left of total_usd, or of the wallet figure if that is less; the floor not taken off."""
        spent = self.spent()
        left = self.total - spent["usd"]
        if self.wallet is not None:
            left = min(left, self.wallet - spent["since_start_tokenrouter_usd"])
        return left

    def remaining(self) -> float:
        return self.left() - self.floor

    def exhausted(self) -> dict | None:
        """None while something is left of total_usd (and of the wallet figure); else the figures to say so.
        Asked before every message of a session in flight: the floor is a session's to eat, the total is not."""
        left = self.left()
        if left > EPS:
            return None
        spent = self.spent()
        return {"left_usd": round(left, 4), "spent_usd": round(spent["usd"], 4), "total_usd": self.total,
                "wallet_usd": self.wallet, "source": spent["source"]}

    def check(self, rows: list[dict], *, reserved: float = 0.0, what: str = "", need: float | None = None) -> dict:
        """Do the projections of these sessions (one matrix row per session) fit in what remains?

        need: what the sessions are expected to cost when that is not the plain sum of their projections
        (sessions that have started: `outstanding`). A row without a price is never ok; the verdict then
        names its model under "unpriced".
        """
        unpriced = sorted({r["model"] for r in rows if not self.priced(r)})
        need = sum(self.projection(r) for r in rows) if need is None else need
        remaining = self.remaining()
        verdict = {"ok": need + reserved <= remaining + EPS and not unpriced, "what": what, "sessions": len(rows),
                   "need_usd": round(need, 4), "reserved_usd": round(reserved, 4), "remaining_usd": round(remaining, 4)}
        if unpriced:
            verdict["unpriced"] = unpriced
        return verdict

    def state(self) -> dict:
        spent = self.spent()
        return {"total_usd": self.total, "floor_usd": self.floor, "wallet_usd": self.wallet,
                "spent_usd": round(spent["usd"], 4), "spent_tokenrouter_usd": round(spent["tokenrouter_usd"], 4),
                "spent_other_usd": round(spent["other_usd"], 4), "spent_source": spent["source"],
                "remaining_usd": round(self.remaining(), 4), "left_usd": round(self.left(), 4),
                "unpriced_models": sorted(self.unpriced), "service": self.service}

    # ----------------------------------------------------------------------- section 14's rule

    def measure(self, row: dict, session: str, attempt: int, minutes: float, *, tr_before: float | None = None,
                tr_after: float | None = None, alone: bool = False) -> dict:
        """Write a row's measured cost and minutes from its first neutral session, where `projection` reads them.

        The cost is the ledger's estimate for the session's last attempt. When the row is paid on TokenRouter,
        no other TokenRouter session of this run overlapped it (`alone`) and the two readings span the whole
        session, the rise of the usage figure across it is used instead, and `input_includes_cache` becomes
        the reading of the token counts that comes closer to it. The caller passes `alone` False for a
        session that was resumed (the first reading was taken mid-session); a session that took two attempts
        never uses the rise, which would set both attempts against one attempt's ledger.
        """
        turns = self.session_turns(session, attempt)
        estimate, includes, source = sum(_usd(e) for e in turns), self.includes_cache(row["id"]), "ledger"
        usd, within, readings = estimate, None, None
        payer, provider = self.payer(row["model"])
        if alone and attempt == 1 and provider == TOKENROUTER and tr_before is not None and tr_after is not None and estimate > 0:
            rise = (tr_after - tr_before) / 100
            if rise > 0 and rise >= estimate / DISTRUST_RATIO:
                def reading(flag: bool) -> float:
                    return sum(pricing.turn_cost_usd(e["usage"], pricing.price_from_yaml(e["price"]), flag) or 0.0
                               for e in turns if e.get("usage") and e.get("price"))
                usd, source = rise, "tr_usage"
                readings = {"input_includes_cache": round(reading(True), 6), "input_excludes_cache": round(reading(False), 6)}
                includes = abs(reading(True) - rise) < abs(reading(False) - rise)
                within = abs(reading(includes) - rise) <= LEDGER_TOLERANCE * rise + EPS     # check 11's 20% rule
        entry = {"session_usd": round(usd, 6), "minutes": round(minutes, 3), "input_includes_cache": includes,
                 "ledger_usd": round(estimate, 6), "source": source, "ledger_within_20pct": within, "ledger_readings_usd": readings,
                 "payer": payer, "provider": provider, "session": session, "run": self.run, "service": self.service, "at": now()}
        schema.check(entry, schema.PRICES["measured"].spec, f"measured.{row['id']}")
        path = self.runs_dir / MEASURED
        own = read_json(path)
        write_json(path, {**(own if isinstance(own, dict) else {}), row["id"]: entry})
        return entry
