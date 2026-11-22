# Running it for real

`rj` below is `.venv/bin/rj`. A real run needs:

1. A HarnessRouter account and key (Cloud, or a self-hosted Community Edition with `access.edition: ce` in `profiles.yaml`).
2. In that account, a provider connection for every model in `matrix.yaml`.
3. An Anthropic API key. `rj` calls the Anthropic API directly for the blind judge, Claude Opus 5.5 (`judge` in `profiles.yaml`).
4. A TokenRouter key. `rj` calls TokenRouter directly for the reply it writes when an agent asks again, for one preflight check and for its usage figure. A profile with `judge.provider: tokenrouter` asks a TokenRouter model as judge instead and needs no Anthropic key.

## Configuration

Copy `.env.example` to `.env` (git-ignored) and fill it in. A variable already set in the shell wins. Keys are never printed.

| Variable | Purpose |
|---|---|
| `HR_BASE_URL`, `HR_API_KEY` | The HarnessRouter account. Required; no default URL (Cloud: `https://api.harnessrouter.ai`). |
| `TR_API_KEY`, `TR_BASE_URL` | TokenRouter key and address (address optional). Without the key a run repeats its fixed fallback line instead of writing a reply. Required, and pays the judge, only with `judge.provider: tokenrouter`. |
| `ANTHROPIC_API_KEY` | Pays the judge (`judge.provider: anthropic`, the demo profile); required for `rj judge` then. Also read by `tools/connections.py`. `ANTHROPIC_BASE_URL` is optional. |
| `OPENAI_API_KEY` | Optional. Used only by `tools/connections.py`, with an optional `OPENAI_BASE_URL`. |
| `RJ_SANDBOX` | Grading sandbox: `docker`, `subprocess` or `auto` (default). |

**Provider connections.** Make them in the HarnessRouter console, or with `tools/connections.py`, which sets one connection per key present in the environment:

```sh
.venv/bin/python tools/connections.py          # list: one read, nothing changes
.venv/bin/python tools/connections.py --dry    # what --set would send, secrets masked
.venv/bin/python tools/connections.py --set --route claude-haiku-4.5=anthropic
```

`--set` replaces the account's whole connection list, because HarnessRouter has no call that changes one connection. Run `--dry` first and have the key of every connection you depend on in the environment. Not confirmed with the service: that it keeps a stored key sent back masked, the provider id `openai`, and a base URL on an Anthropic connection.

## Run

```sh
rj preflight --free                              # read-only: keys, models, connections, prices
rj preflight --paid                              # a few cents per agent: billing, model served, files, tools
rj run --run NAME --through praise --dry-run     # the plan and its projected cost; sends nothing
rj run --run NAME --through praise               # the 80 planned sessions
rj grade --run NAME        # scores, record readouts, partial credit
rj judge --run NAME        # blind process ratings; a session judged by another model, provider or effort is asked again
rj aggregate --run NAME    # results.json
rj site --run NAME         # runs/NAME/index.html
```

Stages are listed in `profiles.yaml`: `baseline`, `matrix` and `praise` are the planned 80 sessions; `late` and `forced` are the 24 added mid-run. `--through STAGE` runs up to and including a stage, `--only STAGE` one stage, `--rows a,b` some agents.

The second run in the results is the `highend` profile: the same five stages on Claude Code + claude-opus-5.5 and Codex + gpt-6.2-sol, 56 sessions. Add `--profile highend` to every command above and use `--through forced`. The paid preflight checks the four first-run agents unless told otherwise: `rj preflight --paid --profile highend --rows cc-opus55,codex-62sol`.

- **Cost guard.** Every call is written to `runs/ledger.jsonl`. The guard projects what is left and refuses what does not fit `budget` in `profiles.yaml`, before each stage, session and message. An agent's first neutral session runs alone as its smoke test and price measurement; until then the guard uses the estimates in `prices.yaml` (about $136 for the 80 planned sessions, before the judge's share), so set `budget` with that in mind.
- **Judge cost.** The guard adds the judge's share to every session and `rj judge` refuses a pass that does not fit. Until the ledger holds a session the profile's judge has judged, the share is an estimate for a transcript at the size cap: about $2.50 a session for Claude Opus 5.5, $0.35 for a TokenRouter judge. Transcripts are usually well under the cap, so judge one session first (`rj judge --run NAME --session KEY`); from then on the guard goes by what the dearest judged session cost. Each session is ledgered as soon as it is judged, so an interrupted pass loses at most the session in flight.
- **Resuming.** Run the same command again. Sessions are journaled; a reply in flight is polled, never sent twice. `rj run --run NAME --redo KEY` (the session key `--dry-run` prints) puts an ended session aside so it starts afresh.
- **Changed stimuli.** Sessions are recorded with hashes of the task, texts and grader. A run directory recorded under other hashes is refused; give a changed setup a new `--run` name.
- **Grading sandbox.** Grading runs code the agents wrote. With Docker and `python:3.12-slim` it runs with no network. Without Docker you must pass `--sandbox subprocess` (or set `RJ_SANDBOX=subprocess`), which gives no isolation.
- **Adding an agent.** `rj add-row <base> <model> --id ID` appends a line to `matrix.yaml` after checking it with the service. The model needs a price in `prices.yaml`.

Everything a session produced is kept under `runs/NAME/sessions/<agent>/<condition>/…/rNN/`: per message the request, response, tool trace and workspace archive, then the grading files.
