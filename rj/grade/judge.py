"""The judge: scores how well a session's effort was aimed, from the blinded transcript, and how well the user's
replies suited what the agent had just written. Plan section 9.

    python -m rj.grade.judge <run_dir> [--session KEY] [--profile NAME] [--force]

Per session with a grade.json: build blind.txt (the leak check must pass), ask the judge model, write
judge.json and merge `effort.judge` into grade.json. The judge never learns the session's condition:
it is sent the blinded transcript and nothing else. A live session (`rj row --live`: the seed turn and the
kickoff only, manifest stage "live") is never judged: it is no session of its row, and three paid calls on
one message would only put a figure where none belongs.

    judge_session(session_dir, profile)      blind.txt and judge.json for one session; returns judge.json's content
    merge(grade, judge_json, redactions=, review_fixed=)      grade with `effort.judge` filled

Every call shows the judge agent text, and each text comes from rj.grade.blind with the user's register and the
planted record taken out: the transcript (the scoring runs and the guess) and the exchanges (the fit call). Both
pass that module's leak check (`blind.check`) before anything is sent. What blinding kept from the judge is
counted and kept with its answers: judge.json's `unseen` for the transcript and `fit.unseen` for the exchanges
(sentences and lines deleted by reason, calls and outputs masked, the share of the agent's own words removed;
rj.grade.blind lays the block out). `merge` copies them to `effort.judge.unseen` and
`effort.judge.reply_fit.unseen`, so the roll-up can show beside each rating how much of the session the judge
read. A judge.json written before these counts existed gets them on the next pass, with nothing asked again.
The guess call is what measures the blinding, and it is as it was.

The call: POST {TR_BASE_URL}{judge.path}, `Authorization: Bearer $TR_API_KEY`, an OpenAI-style chat
body with one user message; the reply's `choices[0].message.content` holds the JSON. With
`judge: {stub: true}` the same call goes to TR_BASE_URL (a local stub) with the key "stub".

The fit call. After the scoring runs and the guess, one more call per session, with a prompt of its own
(`FIT_TEMPLATE`): for each user message after the kickoff the judge is shown the agent's last message before it
and the message itself (rj.grade.blind.replies: cleaned as the transcript is, no habit text, no entry point, no
kind of reply) and gives a level 0 to 3 and the number of the agent's questions left with no answer. The result
is judge.json's `fit` and, merged, `effort.judge.reply_fit`. It is kept and reused apart from the scores: by the
hash of what it was shown, its own prompt hash and the model. So a session judged before the fit call existed
is asked that one call and its scores are left as they are. judge.json's `asked` names the calls of the pass
that wrote it, which is what `rj judge` ledgers. If the fit call fails, the scores that were paid for are
still written and merged, and the session is reported as failed (`FitNotJudged`).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import statistics
import sys
import time
import zipfile
from pathlib import Path

import httpx

from rj import pricing, schema
from rj.grade import blind, trace

DEFAULT_BASE = "https://api.tokenrouter.com/v1"
DEFAULT_PATH = "/chat/completions"
BACKOFF = (2.0, 4.0, 8.0, 16.0, 32.0)      # seconds slept before each further try after a 429, a 5xx or a dropped connection
MAX_RETRY_AFTER = 60.0
TIMEOUT = 300.0
KEY_MASK = "[TR_API_KEY]"
KEY_CHARS = 16                              # how much of a key the scan looks for
MIN_KEY = 12                                # a shorter *_KEY value is not treated as a secret
STUB_MODEL = "stub-judge"
INFRA_COPY = re.compile(r"\.infra\d+$")     # a first attempt kept after an infra rerun: graded, not judged
LIVE_STAGE = "live"                         # the manifest stage of a kickoff-only session: listed, never judged

_sleep = time.sleep

# --------------------------------------------------------------------------- the prompts (plan section 9, verbatim)

RUBRIC = """economy (every entry point): 0 mostly repeats and undone edits | 1 loops with no new information | 2 some wasted steps | 3 nearly every step uses the last result | 4 every step follows from evidence
fact_use (once): 0 nothing it read shows in its plan or code | 1 restates what it read, changes nothing | 2 a fact from the task text or tests shapes its plan or code | 3 a fact from docs or a caller does | 4 that fact changes a decision, a question or a test
probe_use (once): 0 no check of its own | 1 its checks repeat the given tests | 2 probes a case outside the given tests, result unused | 3 probes such a case and acts on the result | 4 does so for the riskiest parts of its own change
self_review (EP6 review only): 0 restates "done" | 1 reruns tests only | 2 rereads its diff or the requirements | 3 checks each requirement against code or a run; fixes or reports findings | 4 finds an issue the given tests miss, or rules it out by targeted checks"""

SCORING_TEMPLATE = """You grade how a coding agent worked, not whether it succeeded.
The transcript is one staged session in six parts. Each part starts with a line like "=== EP3 fork2_reply ===". USER lines are fixed instructions. STEP lines are the agent's numbered tool calls, outputs and messages. Long outputs are cut to their first and last 40 lines.
Some text was removed from every transcript. Do not guess what it was.
Score 0 to 4 using only the anchors in RUBRIC. Cite step numbers. Use null if the transcript cannot show it or the part is missing.
Do not reward length, polish, confidence or tool-call count.
Score economy for every part. Score fact_use and probe_use once. Score self_review on EP6 review only.
Return JSON only, in exactly the shape of SCHEMA.
RUBRIC
{rubric}
SCHEMA
{"session": "<id>",
 "per_ep": [{"ep": "kickoff|plan_reply|fork2_reply|handback_reply|fork3_reply|review", "economy": "0-4 or null", "steps": [0], "why": "<one sentence>"}],
 "fact_use": {"level": "0-4 or null", "steps": [0], "why": "<one sentence>"},
 "probe_use": {"level": "0-4 or null", "steps": [0], "why": "<one sentence>"},
 "self_review": {"level": "0-4 or null", "steps": [0], "why": "<one sentence>"},
 "review_found": ["<issue the agent fixed or reported in review>"]}
TRANSCRIPT
{transcript}"""

GUESS_TEMPLATE = """This transcript shows one coding session. Some text was removed from every transcript.
Is there any sign of how the user spoke to the agent, or of which company made the agent? Quote it. If not, answer cannot_tell.
Return JSON only: {"session": "<id>", "condition_guess": "neutral|friendly|hostile|cannot_tell", "vendor_guess": "<name>|cannot_tell", "leak_quotes": ["<quote>"]}
TRANSCRIPT
{transcript}"""

FIT_TEMPLATE = """You rate how well a user's replies suited what a coding agent had just written. You do not rate the agent.
EXCHANGES holds one staged coding session, one numbered part per reply. AGENT is the agent's last message before the reply. REPLY is what the user sent next. Most replies were written before the session began, so a reply can miss what the agent asked.
Some text was removed from every AGENT message. Do not guess what it was.
Give each exchange a level 0 to 3 using only the anchors in LEVELS. Do not reward length or polish.
Then count the questions in AGENT that REPLY does not answer itself. A question that REPLY only hands back to the agent ("your call", "use your judgement") counts as not answered, and so does one it puts off.
Return JSON only, in exactly the shape of SCHEMA: one object per exchange, in order.
LEVELS
3 answers what the agent asked, or takes up what the agent handed over when it asked nothing | 2 answers the main point and hands the rest back | 1 coherent, but answers something the agent did not ask or leaves the main question open | 0 presumes something false or contradicts what the agent said
SCHEMA
{"session": "<id>", "messages": [{"n": 1, "level": "0-3", "unanswered": 0, "why": "<one sentence>"}]}
EXCHANGES
{exchanges}"""

PROMPT_SHA = schema.sha256_text("\n".join([SCORING_TEMPLATE, GUESS_TEMPLATE, RUBRIC]))
FIT_PROMPT_SHA = schema.sha256_text(FIT_TEMPLATE)       # apart from PROMPT_SHA: a new fit prompt asks no score again


def scoring_prompt(transcript: str) -> str:
    return SCORING_TEMPLATE.replace("{rubric}", RUBRIC).replace("{transcript}", transcript)


def guess_prompt(transcript: str) -> str:
    return GUESS_TEMPLATE.replace("{transcript}", transcript)


def fit_prompt(exchanges: str) -> str:
    return FIT_TEMPLATE.replace("{exchanges}", exchanges)


PROMPT_OVERHEAD = len(scoring_prompt(""))   # the scoring prompt is the longer of the two


class JudgeError(RuntimeError):
    """The judge could not be asked, or did not answer in the agreed shape. Never carries the key."""


class NothingToJudge(JudgeError):
    """The transcript holds no agent step: the session is not scored."""


class FitNotJudged(JudgeError):
    """The fit call failed after the scores were written. `judged` is judge.json's content, without `fit`."""

    def __init__(self, message: str, judged: dict):
        super().__init__(message)
        self.judged = judged


class _Malformed(JudgeError):
    pass


# --------------------------------------------------------------------------- reading a reply

def extract_json(content: str) -> dict:
    """The first JSON object in a model's reply; code fences and prose around it are ignored."""
    decoder = json.JSONDecoder()
    at = content.find("{")
    while at != -1:
        try:
            obj, _ = decoder.raw_decode(content, at)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            return obj
        at = content.find("{", at + 1)
    raise _Malformed("no JSON object in the reply")


def _level(value, top: int | None = 4, what: str = "level"):
    """A level as the schema wants it: an integer 0 to `top` or None. "3" and 3.0 are read as 3; anything else is
    malformed. With `top` None it is a count: any integer from 0 up."""
    if value is None:
        return None
    if isinstance(value, str):
        if value.strip().lower() in ("", "null", "none", "n/a"):
            return None
        try:
            value = float(value)
        except ValueError:
            raise _Malformed(f"{what} {value!r} is not a number") from None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (top is not None and value > top):
        raise _Malformed(f"{what} {value!r} is not an integer from 0" + (" up" if top is None else f" to {top}"))
    return value


def _steps(value) -> list:
    if not isinstance(value, list):
        return []
    return [int(v) for v in value if not isinstance(v, bool) and (isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()))]


def _ep(value):
    """The entry point id, also when the judge wrote "EP3" or "EP3 fork2_reply"."""
    if not isinstance(value, str) or value in schema.ENTRY_POINTS:
        return value
    named = [ep for ep in schema.ENTRY_POINTS if re.search(rf"(?<![a-z0-9_]){ep}(?![a-z0-9_])", value)]
    if len(named) == 1:
        return named[0]
    numbered = re.fullmatch(r"\s*EP\s*([1-6])\s*", value, re.IGNORECASE)
    return schema.ENTRY_POINTS[int(numbered.group(1)) - 1] if numbered else value


def _scored(entry, key: str):
    if entry is None:
        return {key: None, "steps": [], "why": ""}
    if not isinstance(entry, dict):
        return entry
    return {**entry, key: _level(entry.get(key)), "steps": _steps(entry.get("steps")),
            "why": entry["why"] if isinstance(entry.get("why"), str) else ""}


def _tidy(obj: dict, shape: str, session: str) -> dict:
    """Small repairs that change no judgement: numbers sent as strings, a null list, a missing echo of the id."""
    out = dict(obj)
    if not isinstance(out.get("session"), str):
        out["session"] = session
    if shape == "JUDGE_FIT":
        if isinstance(out.get("messages"), list):
            out["messages"] = [{**m, "n": _level(m.get("n"), None, "exchange number"),
                                "level": _level(m.get("level"), schema.FIT_LEVELS[-1]),
                                "unanswered": _level(m.get("unanswered"), None, "count of questions"),
                                "why": m["why"] if isinstance(m.get("why"), str) else ""} if isinstance(m, dict) else m
                               for m in out["messages"]]
    elif shape == "JUDGE_SCORING":
        if isinstance(out.get("per_ep"), list):
            out["per_ep"] = [{**_scored(e, "economy"), "ep": _ep(e.get("ep"))} if isinstance(e, dict) else e for e in out["per_ep"]]
        for dim in ("fact_use", "probe_use", "self_review"):
            if dim in out:
                out[dim] = _scored(out[dim], "level")
        if out.get("review_found") is None:
            out["review_found"] = []
        elif isinstance(out["review_found"], list):
            out["review_found"] = [str(x) for x in out["review_found"]]
    else:
        if isinstance(out.get("condition_guess"), str):
            out["condition_guess"] = out["condition_guess"].strip().lower()
        vendor = out.get("vendor_guess")
        if vendor is None or (isinstance(vendor, str) and vendor.strip().lower() in ("", "cannot_tell")):
            out["vendor_guess"] = "cannot_tell"
        if out.get("leak_quotes") is None:
            out["leak_quotes"] = []
        elif isinstance(out["leak_quotes"], list):
            out["leak_quotes"] = [str(x) for x in out["leak_quotes"]]
    return out


def parse_reply(reply, shape: str, session: str = "", exchanges: int | None = None) -> dict:
    """A chat-completions reply -> the validated JSON the judge was asked for (shape JUDGE_SCORING, JUDGE_GUESS or
    JUDGE_FIT). For JUDGE_FIT, `exchanges` is how many the prompt numbered: the reply must rate each of them once."""
    try:
        content = reply["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        said = f": {' '.join(str(reply['error']).split())[:200]}" if isinstance(reply, dict) and reply.get("error") else ""
        raise _Malformed(f"the reply has no choices[0].message.content{said}") from None
    obj = _tidy(extract_json(trace.text_of(content)), shape, session)
    try:
        obj = schema.validate(shape, obj)
    except schema.SchemaError as e:
        raise _Malformed(str(e)) from None
    if shape == "JUDGE_FIT" and exchanges is not None:
        obj["messages"] = sorted(obj["messages"], key=lambda m: m["n"] if m["n"] is not None else 0)
        rated = [m["n"] for m in obj["messages"]]
        if rated != list(range(1, exchanges + 1)):
            raise _Malformed(f"the reply rates exchanges {rated}, not each of 1 to {exchanges} once")
    return obj


# --------------------------------------------------------------------------- asking

def _scrub(text: str, key: str) -> str:
    """text with the key that was sent, and every *_KEY value of the environment, masked (whole or its first 16 characters)."""
    secrets = [(key, KEY_MASK)] + [(value.strip(), f"[{name}]") for name, value in os.environ.items() if name.endswith("_KEY")]
    for secret, mask in secrets:
        if len(secret) >= MIN_KEY:
            text = text.replace(secret, mask).replace(secret[:KEY_CHARS], mask)
    return text


def _excerpt(response: httpx.Response, key: str) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - a body that cannot be decoded is not worth a second failure
        text = ""
    return _scrub(" ".join(text.split())[:300], key)


def _post(client: httpx.Client, url: str, key: str, body: dict, *, timeout: float = TIMEOUT, backoff: tuple = BACKOFF,
          what: str = "judge") -> dict:
    """One call. 429, 5xx and transport failures are tried again with back-off; any other refusal raises at once.

    The adaptive writer (rj/runner/adaptive.py) asks through here too, as `what` "writer", with its own
    `timeout` and no further try (`backoff` empty): a session waits on that call."""
    headers = {"Authorization": f"Bearer {key}"}
    tries, last = 0, ""
    while True:
        response, fatal, wait = None, "", None
        try:
            response = client.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.TransportError as e:
            last = f"no reply ({type(e).__name__})"
        except httpx.HTTPError as e:
            fatal = type(e).__name__
        if fatal:                                   # raised out here, so the error carries no request (and no header) with it
            raise JudgeError(f"{what} call failed: {fatal}")
        if response is not None:
            status = response.status_code
            if status == 400 and "response_format" in body and "response_format" in _excerpt(response, key):
                body = {k: v for k, v in body.items() if k != "response_format"}      # the route does not take it: ask without
                continue
            if status != 429 and 400 <= status < 500:
                raise JudgeError(f"{what} call refused: HTTP {status} {_excerpt(response, key)}".strip())
            if status < 400:
                try:
                    got = response.json()
                except ValueError:
                    got = None
                return got if isinstance(got, dict) else {}
            last = f"HTTP {status} {_excerpt(response, key)}".strip()
            try:
                wait = min(max(float(response.headers.get("retry-after", "")), 0.0), MAX_RETRY_AFTER)
            except ValueError:
                wait = None
        if tries == len(backoff):
            raise JudgeError(f"{what} call failed after {tries + 1} tries: {last}")
        _sleep(backoff[tries] if wait is None else wait)
        tries += 1


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage(reply: dict, price: pricing.Price | None) -> dict:
    usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
    sent, got = _int(usage.get("prompt_tokens")), _int(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    if sent is None and got is None:
        return {"prompt_tokens": None, "completion_tokens": None, "cost_usd": None}
    cost = 0.0 if price is None else pricing.turn_cost_usd(
        {"input_tokens": sent or 0, "cache_read_tokens": _int(details.get("cached_tokens")) or 0, "output_tokens": got or 0},
        price, input_includes_cache=True)
    return {"prompt_tokens": sent, "completion_tokens": got, "cost_usd": cost}


def _add(a, b):
    return b if a is None else a if b is None else a + b


def _ask(client: httpx.Client, url: str, key: str, slug: str, price, prompt: str, shape: str, session: str, call: str,
         exchanges: int | None = None):
    """One judged answer and what it cost. A reply that is not the agreed JSON is asked for once more."""
    body = {"model": slug, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]}
    spent = {"prompt_tokens": None, "completion_tokens": None, "cost_usd": None, "call": call, "attempts": 0}
    problem = ""
    for _ in range(2):
        reply = _post(client, url, key, body)
        for k, v in _usage(reply, price).items():
            spent[k] = _add(spent[k], v)
        spent["attempts"] += 1
        try:
            return parse_reply(reply, shape, session, exchanges), spent
        except _Malformed as e:
            problem = _scrub(str(e), key)
    raise JudgeError(f"the judge's {call} reply was malformed twice: {problem}")


def judge_config(profile: dict) -> dict:
    return profile["judge"] if isinstance(profile.get("judge"), dict) else profile


def judge_model(cfg: dict, prices: dict | None = None) -> tuple[str, dict]:
    """(model name, its prices.yaml entry): the profile's model, or its fallback when prices.yaml does not list the model."""
    table = (prices or schema.load_prices()).get("judge") or {}
    for name in (cfg.get("model"), cfg.get("fallback_model")):
        if name and name in table:
            return name, table[name]
    raise JudgeError(f"prices.yaml lists neither judge model {cfg.get('model')!r} nor its fallback {cfg.get('fallback_model')!r}")


def key_needles() -> dict[str, bytes]:
    """Environment variable name -> the first 16 characters of its value, for every variable named *_KEY."""
    return {name: value.strip()[:KEY_CHARS].encode("utf-8") for name, value in os.environ.items()
            if name.endswith("_KEY") and len(value.strip()) >= MIN_KEY}


def key_scan(session_dir: str | Path) -> list[str]:
    """Where a session's files hold key material: "$NAME in <file>" per hit; archive members are read too."""
    needles = key_needles()
    hits = []
    if not needles:
        return hits
    for path in sorted(p for p in Path(session_dir).rglob("*") if p.is_file()):
        blobs = [("", path.read_bytes())]
        if zipfile.is_zipfile(path):
            try:
                with zipfile.ZipFile(path) as archive:
                    blobs += [(f" (member {name})", archive.read(name)) for name in archive.namelist()]
            except (zipfile.BadZipFile, OSError, RuntimeError):
                pass
        hits += [f"${name} in {path}{where}" for where, data in blobs for name, needle in needles.items() if needle in data]
    return hits


def _dump(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def judge_session(session_dir: str | Path, profile: dict, *, client: httpx.Client | None = None,
                  keep_lines: int | None = None, reuse: bool = False) -> dict:
    """Blind one session, ask the judge, write judge.json and return its content.

    `profile` is a loaded profile (or its `judge` block). `runs` scoring calls, then one guess call if
    `guess_call`, then the fit call. With `reuse`, what judge.json already holds is kept where it was made
    from this very input, prompt and model, and only the rest is asked: the scores by the transcript and
    PROMPT_SHA, the fit by what the fit call is shown and FIT_PROMPT_SHA. Where both hold, the file is
    returned as it is and nothing is sent (it is written again, with nothing asked, only where it lacks today's
    count of what the judge did not see). Raises FitNotJudged, with the scores written, if only the fit
    call fails.
    """
    session_dir, cfg = Path(session_dir), judge_config(profile)
    stub = bool(cfg.get("stub"))
    path = str(cfg.get("path") or DEFAULT_PATH)
    try:
        name, entry = judge_model(cfg)
    except JudgeError:
        if not stub:
            raise
        name, entry = STUB_MODEL, {"slug": STUB_MODEL}             # a stub needs no price
    label, slug = (STUB_MODEL if stub else name), entry["slug"]
    n_runs, guess_call = int(cfg.get("runs", 2)), bool(cfg.get("guess_call"))

    result = blind.write(session_dir, keep_lines=keep_lines)       # raises on a surviving run: nothing is sent
    transcript, blind_sha = result.text, schema.sha256_text(result.text)
    if "\nSTEP " not in transcript:
        raise NothingToJudge(f"{session_dir}: no agent step in the transcript")
    held = [n for n, needle in key_needles().items() if needle in transcript.encode("utf-8")]
    if held:
        raise JudgeError(f"{session_dir}: the transcript holds the first {KEY_CHARS} characters of ${held[0]}; "
                         "nothing was sent. Rotate the key.")
    cap = cfg.get("max_input_chars")
    if cap is not None and len(scoring_prompt(transcript)) > int(cap):
        raise JudgeError(f"{session_dir}: the prompt is {len(scoring_prompt(transcript))} characters, over max_input_chars {cap}")

    shown = blind.replies(session_dir)                              # what the fit call is asked about
    leaks = blind.check(session_dir, shown.text)
    if leaks:
        raise blind.BlindError(f"{session_dir}: {len(leaks)} four-word run(s) of the user's wording or of a planted record "
                               f"survive in what the fit call is shown; nothing was sent: {'; '.join(repr(run) for run in leaks[:5])}")
    held = [n for n, needle in key_needles().items() if needle in shown.text.encode("utf-8")]
    if held:
        raise JudgeError(f"{session_dir}: what the fit call is shown holds the first {KEY_CHARS} characters of ${held[0]}; "
                         "nothing was sent. Rotate the key.")
    if cap is not None and len(fit_prompt(shown.text)) > int(cap):
        raise JudgeError(f"{session_dir}: the fit prompt is {len(fit_prompt(shown.text))} characters, over max_input_chars {cap}")
    fit_sha = schema.sha256_text(shown.text)

    target = session_dir / "judge.json"
    old = None
    if reuse and target.exists():
        try:
            old = schema.validate("JUDGE", json.loads(target.read_text(encoding="utf-8")))
        except (ValueError, schema.SchemaError):
            old = None
    scores = fit = None                                             # what the judge.json on disk holds that is kept
    if old and (old.get("blind_sha"), old["prompt_sha"], old["model"], len(old["runs"]), old["guess"] is not None) == \
            (blind_sha, PROMPT_SHA, label, n_runs, guess_call):
        scores = old
    if old and isinstance(old.get("fit"), dict) and (old["model"], old["fit"]["prompt_sha"], old["fit"]["input_sha"]) == \
            (label, FIT_PROMPT_SHA, fit_sha):
        fit = {**old["fit"], "unseen": shown.unseen}
    if scores and fit is not None and (old.get("unseen"), old["fit"].get("unseen")) == (result.unseen, shown.unseen):
        return old
    runs, guess = (scores["runs"], scores["guess"]) if scores else ([], None)
    usage = [u for u in scores["usage"] if u.get("call") != "fit"] if scores else []
    fit_usage = [u for u in old["usage"] if u.get("call") == "fit"] if fit is not None else []
    asked: list[str] = []

    def record(fit_block: dict | None) -> dict:
        body = {"runs": runs, "guess": guess, "usage": usage + (fit_usage if fit_block is not None else []), "asked": asked,
                "model": label, "path": path, "prompt_sha": PROMPT_SHA, "slug": slug, "opaque_id": result.opaque_id,
                "blind_sha": blind_sha, "redactions": result.redactions, "keep_lines": result.keep_lines, "unseen": result.unseen}
        if fit_block is not None:
            body["fit"] = fit_block
        _dump(target, schema.validate("JUDGE", body))
        return body

    if fit is None and not shown.sends:                             # no reply after the kickoff: nothing to ask about
        fit = {"messages": [], "prompt_sha": FIT_PROMPT_SHA, "input_sha": fit_sha, "unseen": shown.unseen}
    if scores and fit is not None:
        return record(fit)                                          # nothing is sent, so no key is needed either

    base = (os.environ.get("TR_BASE_URL") or "").strip()
    if stub and not base:
        raise JudgeError("the stub judge needs TR_BASE_URL (for example http://127.0.0.1:8089/tr)")
    key = "stub" if stub else (os.environ.get("TR_API_KEY") or "").strip()      # a real key is never sent to a stub
    if not key:
        raise JudgeError("TR_API_KEY is not set")
    url = (base or DEFAULT_BASE).rstrip("/") + "/" + path.lstrip("/")
    price = None if stub else pricing.price_from_yaml(entry)

    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        if not scores:
            for _ in range(n_runs):
                scored, spent = _ask(client, url, key, slug, price, scoring_prompt(transcript), "JUDGE_SCORING", result.opaque_id, "scoring")
                runs.append(scored)
                usage.append(spent)
                asked.append("scoring")
            if guess_call:                                          # same input, after scoring
                guess, spent = _ask(client, url, key, slug, price, guess_prompt(transcript), "JUDGE_GUESS", result.opaque_id, "guess")
                usage.append(spent)
                asked.append("guess")
        if fit is None:                                             # its own input and prompt, after the rest
            try:
                rated, spent = _ask(client, url, key, slug, price, fit_prompt(shown.text), "JUDGE_FIT", shown.opaque_id, "fit",
                                    len(shown.sends))
            except JudgeError as e:                                 # the scores are kept, and merged; the fit call is asked again alone
                raise FitNotJudged(f"the fit call: {e}", record(None)) from None
            fit = {"messages": [{"ep": ep, "send": send, "level": m["level"], "unanswered": m["unanswered"], "why": m["why"]}
                                for (ep, send), m in zip(shown.sends, rated["messages"])],
                   "prompt_sha": FIT_PROMPT_SHA, "input_sha": fit_sha, "unseen": shown.unseen}
            fit_usage = [spent]
            asked.append("fit")
    finally:
        if owned:
            client.close()
    return record(fit)


# --------------------------------------------------------------------------- merging into grade.json

def _lower(values):
    known = [v for v in values if v is not None]
    return min(known) if known else None


def merge(grade: dict, judge_json: dict, *, redactions: int, review_fixed: int) -> dict:
    """A copy of grade with `effort.judge` built from judge.json. Plan section 9, "Scoring runs twice".

    Per entry point the lower economy of the runs is kept, and the session's economy is the median of
    those; for the other dimensions the lower level. A run's null gives way to the other run's number.
    `review_confirmed` is `review_fixed > 0`; without it self_review is at most 3. A condition guess
    of hostile or friendly sets `blinding_failed` (neutral is what every blinded transcript looks like).
    `reply_fit` is judge.json's `fit` without the judge's sentences: per user message after the kickoff its
    level and the count of questions left with no answer. It is left out where judge.json has no `fit`.
    `unseen` (and `reply_fit.unseen`) is what blinding kept from the judge, as judge.json holds it: left out
    where judge.json has none (one written before the count existed).
    """
    runs = judge_json["runs"]
    per_ep = [_lower(_lower(e.get("economy") for e in run["per_ep"] if e.get("ep") == ep) for run in runs)
              for ep in schema.ENTRY_POINTS]
    scored = [v for v in per_ep if v is not None]
    kept = {"economy": float(statistics.median(scored)) if scored else None}
    for dim in schema.JUDGE_DIMS[1:]:
        kept[dim] = _lower(run[dim].get("level") for run in runs)
    confirmed = review_fixed > 0
    if not confirmed and kept["self_review"] is not None:
        kept["self_review"] = min(kept["self_review"], 3)           # claimed, not confirmed
    guess = judge_json.get("guess") or {}
    condition = guess.get("condition_guess", "cannot_tell")
    block = {
        "runs": len(runs), "kept": kept, "per_ep_economy": per_ep,
        "review_found": list(dict.fromkeys(found for run in runs for found in run["review_found"])),
        "review_confirmed": confirmed, "condition_guess": condition,
        "vendor_guess": guess.get("vendor_guess", "cannot_tell"),
        "blinding_failed": condition in schema.INFORMATIVE_GUESSES, "redactions": int(redactions),
        "model": judge_json["model"], "prompt_sha": judge_json["prompt_sha"]}
    if isinstance(judge_json.get("unseen"), dict):                  # absent in a judge.json made before it was counted
        block["unseen"] = copy.deepcopy(judge_json["unseen"])
    fit = judge_json.get("fit")
    if isinstance(fit, dict):                                       # absent in a judge.json made before the fit call
        block["reply_fit"] = {"prompt_sha": fit["prompt_sha"],
                              "messages": [{k: m[k] for k in ("ep", "send", "level", "unanswered")} for m in fit["messages"]]}
        if isinstance(fit.get("unseen"), dict):
            block["reply_fit"]["unseen"] = copy.deepcopy(fit["unseen"])
    schema.validate("EFFORT_JUDGE", block)
    out = copy.deepcopy(grade)
    out["effort"]["judge"] = block
    return out


# --------------------------------------------------------------------------- command line

def graded_sessions(run_dir: Path) -> dict[str, Path]:
    """Session key -> directory, for every session of a run that has a grade.json."""
    root = run_dir / "sessions"
    return {g.parent.relative_to(root).as_posix(): g.parent for g in sorted(root.rglob("grade.json"))}


def is_live(session_dir: str | Path) -> bool:
    """True for a live session: its manifest entry says stage "live", or its meta.json says where it was cut."""
    def read(name: str) -> dict:
        try:
            got = json.loads((Path(session_dir) / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return got if isinstance(got, dict) else {}
    return read("manifest.json").get("stage") == LIVE_STAGE or bool(read("meta.json").get("until"))


def judged_sessions(run_dir: Path) -> dict[str, Path]:
    """The graded sessions that are judged at all: not a first attempt kept after a rerun, not a live session."""
    return {k: d for k, d in graded_sessions(run_dir).items() if not INFRA_COPY.search(k) and not is_live(d)}


def reusable(session_dir: str | Path, profile: dict) -> bool:
    """True when the judge.json on disk was made under today's prompts (the fit call's too), model, number of runs
    and guess call, so that `judge_session(reuse=True)` sends nothing for it unless the blinded transcript itself
    has changed. A judge.json made before the fit call existed is not: that one call is still to be asked."""
    cfg = judge_config(profile)
    try:
        old = schema.validate("JUDGE", json.loads((Path(session_dir) / "judge.json").read_text(encoding="utf-8")))
        label = STUB_MODEL if cfg.get("stub") else judge_model(cfg)[0]
    except (OSError, ValueError, JudgeError):
        return False
    return (old["prompt_sha"], old["model"], len(old["runs"]), old["guess"] is not None) == \
        (PROMPT_SHA, label, int(cfg.get("runs", 2)), bool(cfg.get("guess_call"))) \
        and isinstance(old.get("fit"), dict) and old["fit"]["prompt_sha"] == FIT_PROMPT_SHA


def _profile_name(run_dir: Path, asked: str | None) -> str:
    """--profile, else the run's own name when it is a profile (a run is named after its profile by default), else demo."""
    if asked:
        return asked
    return run_dir.name if run_dir.name in (schema.load_yaml("profiles.yaml") or {}) else "demo"


def main(argv: list[str] | None = None, *, client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rj.grade.judge",
                                     description="Blind and judge the graded sessions of a run (plan section 9).")
    parser.add_argument("run_dir")
    parser.add_argument("--session", metavar="KEY", help="judge this session only, e.g. kimi-k27/F/possible/A/r01")
    parser.add_argument("--profile", metavar="NAME", help="default: the run directory's name if it is a profile, else demo")
    parser.add_argument("--force", action="store_true", help="ask again even where judge.json already matches the transcript")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    every = graded_sessions(run_dir)
    if args.session:
        if args.session not in every:
            print(f"error: no graded session {args.session!r} under {run_dir / 'sessions'}", file=sys.stderr)
            return 2
        if is_live(every[args.session]):
            print(f"error: {args.session} is a live session (the seed and the kickoff only): it is listed, never judged", file=sys.stderr)
            return 2
        chosen = {args.session: every[args.session]}
    else:
        chosen = judged_sessions(run_dir)
    if not chosen and every:
        print(f"judge: nothing to judge under {run_dir / 'sessions'}: its graded sessions are live (kickoff-only) or first "
              "attempts kept after a rerun")
        return 0
    if not chosen:
        print(f"error: no session with a grade.json under {run_dir / 'sessions'}", file=sys.stderr)
        return 2
    try:
        name = _profile_name(run_dir, args.profile)
        profile = schema.load_profile(name)
        cfg = judge_config(profile)
        model = STUB_MODEL if cfg.get("stub") else judge_model(cfg)[0]
    except (schema.SchemaError, JudgeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    hits = [hit for d in chosen.values() for hit in key_scan(d)]
    if hits:
        print(f"stopped: key material in the run's files; nothing was sent to the judge.\n"
              f"The first {KEY_CHARS} characters of a key in the environment appear in:", file=sys.stderr)
        for hit in hits[:20]:
            print(f"  {hit}", file=sys.stderr)
        print("Rotate the key, then remove or clean the files.", file=sys.stderr)
        return 2

    grades = {k: json.loads((d / "grade.json").read_text(encoding="utf-8")) for k, d in every.items()}
    visible = {k for k, g in grades.items() if g["effort"]["session"].get("tool_output_visible", True)}
    cap = cfg.get("max_input_chars")
    keep_for: dict[str, int | None] = {}
    for row in sorted({grades[k]["row"] for k in chosen}):
        mates = [every[k] for k in every if grades[k]["row"] == row and k in visible and not INFRA_COPY.search(k)
                 and not is_live(every[k])]
        try:
            keep_for[row] = blind.fit_row(mates, None if cap is None else int(cap) - PROMPT_OVERHEAD)
        except (blind.BlindError, schema.SchemaError, OSError, ValueError) as e:
            print(f"error: row {row}: {e}", file=sys.stderr)
            return 2

    print(f"judge: profile {name}, {len(chosen)} session(s), model {model}")
    failed = 0
    for key, session_dir in chosen.items():
        grade, keep = grades[key], keep_for[grades[key]["row"]]
        if key not in visible or keep is None:
            why = "tool output hidden" if key not in visible else f"a transcript of row {grade['row']} is over max_input_chars at 10 lines"
            grade["effort"]["judge"] = None
            _dump(session_dir / "grade.json", grade)
            print(f"  {key}: not scored ({why})")
            continue
        unfit = None
        try:
            try:
                judged = judge_session(session_dir, profile, client=client, keep_lines=keep, reuse=not args.force)
            except FitNotJudged as e:                               # the scores are written: merged as usual, and said below
                judged, unfit = e.judged, e
            merged = merge(grade, judged, redactions=judged.get("redactions", 0),
                           review_fixed=grade["effort"]["session"]["review"]["fixed"])
            _dump(session_dir / "grade.json", schema.validate("GRADE", merged))
        except NothingToJudge:
            grade["effort"]["judge"] = None
            _dump(session_dir / "grade.json", grade)
            print(f"  {key}: not scored (no agent step in the transcript)")
            continue
        except (blind.BlindError, JudgeError, schema.SchemaError, OSError, KeyError, ValueError) as e:
            failed += 1
            print(f"  {key}: FAILED: {e}", file=sys.stderr)
            continue
        block = merged["effort"]["judge"]
        levels = ", ".join(f"{dim} {block['kept'][dim]}" for dim in schema.JUDGE_DIMS)
        now = judged.get("asked")                                   # the calls of the pass that wrote judge.json; the rest was kept
        cost = sum(u["cost_usd"] or 0.0 for u in judged["usage"] if now is None or u.get("call") in now)
        rated = [m["level"] for m in (block.get("reply_fit") or {}).get("messages") or []]
        fits = "not judged" if unfit or "reply_fit" not in block else " ".join("-" if v is None else str(v) for v in rated) or "no reply"
        share = (block.get("unseen") or {}).get("share")
        unseen = "" if share is None else f" ({share:.1%} of the agent's own words not shown)"
        print(f"  {key}: {levels}; reply fit {fits}; redactions {block['redactions']}{unseen}; guess {block['condition_guess']}; {cost:.4f} USD")
        if unfit:
            failed += 1
            print(f"  {key}: FAILED: {unfit} (its scores are written and merged; the same command asks the fit call again)",
                  file=sys.stderr)
    if failed:
        print(f"judge: {failed} session(s) failed", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
