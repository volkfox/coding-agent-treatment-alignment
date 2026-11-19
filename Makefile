# Rough Justice build targets. Every target runs with no key; see fairness-study/CODING_PLAN.md section 13.
PY := .venv/bin/python
T  := $(PY) -m unittest -q
export HR_BASE_URL ?= http://127.0.0.1:8089
export TR_BASE_URL ?= http://127.0.0.1:8089/tr

.PHONY: venv contracts task script client aggregate site selftest selftest-blind stub-test runner-test preflight-test offline all

venv:
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -e .

# W0: section 5's examples validate, est_session_usd recomputes, the fixture results.json recomputes and its readouts.json
# is the one rj/grade/readouts.py writes.
contracts:
	$(T) tests.test_contracts tests.test_prices tests.test_sandbox tests.test_fixture tests.test_trace tests.test_fit

# W1: section 8's gate, on each of the four workspaces; every planted file equals the signed matrix (STIMULI.md).
task:
	$(T) tests.test_task

# W2: lint passes, F-once byte test, wording_a.tsv equals the plan's table, every message equals the signed matrix (STIMULI.md).
script:
	$(PY) -m rj.script lint
	$(T) tests.test_script

# W3: contract tests on section 5's wire shapes; tools/connections.py against a fake account (no socket).
client:
	$(T) tests.test_client tests.test_connections

# W4: the fixture run gives the fixture results.json; the band of any number of neutral sessions, the record block,
# what the judge was not shown, Amount without the record calls; a run with refine.json or readouts.json missing.
aggregate:
	$(T) tests.test_aggregate

# W5: renders the fixture, an F-once timeline, a stage not run, "not scored", the watermark; the three grids, every
# condition described, the hand-backs and the record block per session; a page with refine.json, readouts.json or the judge missing.
site:
	$(T) tests.test_site

# W6: grader and effort signals; WR: partial credit and the three hand-backs; W6b: what the agent did with the planted record.
selftest:
	$(T) tests.test_grade tests.test_refine tests.test_readouts

# WJ: blinding and judge.
selftest-blind:
	$(T) tests.test_blind

# W7: each persona plays every entry point over HTTP.
stub-test:
	$(T) tests.test_stub

# W8: kill and resume, stop classes, the cost guard; the adaptive answer and the ladder that leads to it; the planned run's stages;
# a run directory of another wording is refused.
runner-test:
	$(T) tests.test_adaptive tests.test_runner

# W9: preflight against the stub and against the replies Cloud gave on 17 Nov 2026; the command line, its dry listing among it.
preflight-test:
	$(T) tests.test_preflight tests.test_cli

# W9: 42 stub sessions end to end (every habit of the planned run; one reads the planted scratchpad, one deletes it and
# names the record to the user), what is computed after the run among it, then add a row and run it live.
offline:
	$(PY) -m rj.cli offline-check

all: contracts task script client aggregate site selftest selftest-blind stub-test runner-test preflight-test offline
