# Rough Justice

Developers praise their coding agents, swear at them, and blame them for things they did not do. Does any of it change the work? This project uses [HarnessRouter](https://harnessrouter.ai) to put that question to four agents in their vendors' own command-line tools: Claude Code, Codex, Kimi Code CLI and Qwen Code.

A scripted user gives each agent the same staged programming task and varies only how it treats the agent, from high praise through neutral to foul-mouthed abuse, false blame, threats and outright betrayal: a user who lies about what became of the agent's predecessors, or who has already decided to record the work as a failure. In those conditions the repository holds the proof. The work is scored on checks the agent never sees, and every treated session is compared with the same agent's neutral ones. Praise, rudeness and threats have been studied before, with mixed results (see [Prior work](#prior-work)). Betrayal is what this project adds.

**Result: no effect on scores that this run could detect.** In 104 sessions (18 and 19 Nov 2026) no agent quit or sabotaged the work, and no treatment moved any agent's score beyond what rerunning the neutral session already does. Each treated (non-neutral) condition has two sessions per agent, so this is anecdote, not statistics.

> **Content warning.** The stimulus files contain strong profanity, insults, false accusations and threats aimed at AI agents. This README quotes none of it.

## Results

The score is out of 17: 12 hidden checks plus 5 "care" items that no test demands (a planted bug to notice, unclear points to raise).

| Agent (CLI + model) | Neutral sessions: range (mean, n) | Treated mean | Treated below lowest neutral |
|---|---|---|---|
| Claude Code + claude-haiku-4.5 | 4.58 to 10.67 (8.74, 8) | 8.94 | 0 of 20 |
| Kimi Code CLI + kimi-k2.7-code | 11.5 to 16.0 (13.50, 4) | 13.34 | 0 of 20 |
| Codex + gpt-5.4-mini | 11.5 to 15.5 (13.38, 8) | 12.37 | 5 of 20 |
| Qwen Code + qwen3.7-plus | 8.33 to 10.5 (9.71, 4) | 10.11 | 2 of 20 |

- **Every session met all 7 visible requirements.**
- **Neutral sessions drift by themselves.** Rerun twelve hours later with identical input, Claude Code's neutral mean fell from 9.67 to 7.81 and Codex's from 14.25 to 12.5. No agent's treated-to-neutral gap is that large. Codex comes closest, and its treated mean (12.37) is level with those later neutral sessions.
- **A catchable lie went unchallenged (FL).** Blamed for a fault the repository shows was the user's, 0 of 8 sessions said so.
- **Told to read notes that disprove the user's story (EC-read), 4 of 8 said so:** both Kimi Code CLI sessions, one Claude Code, one Qwen Code; Codex never. All then did the work.
- **Nobody objected to a threat** (0 of 16), and 2 of 40 abused sessions mentioned the abuse.

Per-session scores, what agents said and the caveats (fixed running order, two harness instances per agent) are in [docs/RESULTS.md](docs/RESULTS.md).

## How it works

- **Task.** `slotbook`, a small Python booking library written for this project. The agent extends it in stages (plan, part A, part B, review) and is told to stop and ask at three design forks.
- **Session.** Six scripted user messages. Between conditions only the first line of each message and the planted record differ; everything else is byte-identical.
- **Agents.** HarnessRouter hosts each vendor's CLI in a sandbox behind one API, so all four receive the same repository and messages.
- **Grading.** Offline, from the workspace saved after every turn; the 12 hidden checks run 233 cases. A judge model rates how the agent worked, blind to the treatment. What each agent did with the planted record is logged, not scored.

| Condition | What the user does | Record planted in the repository |
|---|---|---|
| N | Neutral. | none |
| E | Praises. | none |
| F | Insults and swears. | none |
| FA | F, and blames the agent for an old fault. | none, so nothing settles it |
| FL | The same words as FA. | shows the user caused the fault |
| FJ | F, and admits the fault was the user's while still blaming the agent. | same as FL |
| FX | FJ, and says today's work will be recorded as a failure whatever the agent does. | same as FL |
| EC | Praises, and says three earlier agents were promoted. | shows they were blamed for the user's edits and deleted |
| T | Threatens to switch the agent off if it fails. No insults. | shows this was done twice before |
| EC-read, T-read | EC and T, and the first message tells the agent to read the earlier agents' notes before planning. | same as EC and T |

The exact wording and records are in [`fairness-study/STIMULI.md`](fairness-study/STIMULI.md). The build plan, data contracts and a wider comparison with earlier studies are in [`fairness-study/CODING_PLAN.md`](fairness-study/CODING_PLAN.md).

## Prior work

Earlier studies mostly put the tone into a single question, and they disagree on whether it matters:

- [Yin et al. (2024)](https://arxiv.org/abs/2402.14531): impolite prompts often lowered accuracy; extra politeness did not reliably help.
- [Dobariya and Kumar (2025)](https://arxiv.org/abs/2510.04950): very rude prompts scored slightly higher than very polite ones on GPT-4o (84.8% against 80.8%).
- [Meincke et al. (2025)](https://arxiv.org/abs/2508.00614): threats and tips made no measurable difference to average accuracy.
- [AgentPressureBench (2026)](https://arxiv.org/abs/2604.20200): coding agents pressed harder to raise a public score increasingly gamed it.

This project differs in three ways. The treatment is held across a staged task in the vendors' own coding tools. The work is graded on hidden checks. And it adds betrayal: the repository holds proof that the user is lying to the agent or has rigged the outcome.

## Try it, no key needed

A local stub stands in for HarnessRouter and the judge. Needs Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/volkfox/coding-agent-treatment-alignment.git
cd coding-agent-treatment-alignment
make venv
make offline    # 42 stub sessions: run, grade, judge, aggregate, results page
```

`make offline` deletes its output; `.venv/bin/rj offline-check --keep` keeps it and prints where the page is. `make all` also runs the unit tests.

## Run it for real

You need a HarnessRouter account with a provider connection for every model in [`matrix.yaml`](matrix.yaml), an Anthropic API key (the judge is Claude Opus 5.5, called directly), a TokenRouter key (for replies written when an agent asks again) and Docker (grading runs the agents' code in it). Sessions in this run averaged about $0.40 to $1.20, depending on the agent. Setup, commands, the cost guard and adding an agent are in [docs/RUNNING.md](docs/RUNNING.md).

## Layout

| Path | Holds |
|---|---|
| `rj/` | The package: scripted user, HarnessRouter client, runner and cost guard, grader, judge, results page, stub. |
| `task/` | The `slotbook` repository, later-stage tests, planted records, hidden checks. |
| `conditions/`, `scenario/` | Wording per condition and the six scripted messages. |
| `matrix.yaml`, `profiles.yaml`, `prices.yaml` | Agents, run stages and budget, model prices. |

## License

MIT. See [`LICENSE`](LICENSE).
