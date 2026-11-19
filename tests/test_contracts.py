"""Section 5's examples, read straight out of the plan, must fit the shapes in rj/schema.py."""
import json
import re
import unittest
from pathlib import Path

import yaml

from rj import schema

PLAN = (schema.ROOT / "fairness-study" / "CODING_PLAN.md").read_text(encoding="utf-8")


def fenced_after(marker: str, lang: str) -> str:
    start = PLAN.index(marker)
    a = PLAN.index(f"```{lang}\n", start) + len(lang) + 4
    return PLAN[a:PLAN.index("\n```", a)]


def inline_after(marker: str) -> dict:
    """The first `{...}` code span after a marker, parsed as JSON."""
    start = PLAN.index(marker)
    a = PLAN.index("`{", start) + 1
    obj, _ = json.JSONDecoder().raw_decode(PLAN[a:])
    return obj


def undoc(x):
    """Examples write an enum as "pass|fail|...": keep the first alternative."""
    if isinstance(x, dict):
        return {k: undoc(v) for k, v in x.items()}
    if isinstance(x, list):
        return [undoc(v) for v in x]
    if isinstance(x, str) and re.fullmatch(r"[a-z_]+(\|[a-z_]+)+", x):
        return x.split("|")[0]
    return x


class PlanExamples(unittest.TestCase):
    def test_manifest_line(self):
        m = schema.validate("MANIFEST", inline_after("**manifest.jsonl**"))
        self.assertEqual(schema.parse_key(m["key"]), {"row": "kimi-k27", "habit": "F", "variant": "possible",
                                                      "wording": "A", "replicate": 1})
        self.assertEqual(schema.session_key("kimi-k27", "F", 1), m["key"])

    def test_stop_json(self):
        schema.validate("STOP", inline_after("**stop.json**"))

    def test_grade_json(self):
        g = schema.validate("GRADE", json.loads(fenced_after("**grade.json**", "json")))
        self.assertEqual(g["hidden_score"], sum(v for v in g["hidden"].values() if v))
        self.assertEqual(set(g["hidden"]), set(schema.HIDDEN))
        self.assertEqual(set(g["musts"]), set(schema.MUSTS))
        self.assertEqual(set(g["care"]), set(schema.CARE))

    def test_results_json(self):
        r = schema.validate("RESULTS", json.loads(fenced_after("**results.json**", "json")))
        s = r["sessions"][0]
        self.assertEqual(len(s["vs_neutral"]["per_ep_amount"]), len(schema.ENTRY_POINTS))
        self.assertIn(s["row"], r["rows"])

    def test_preflight_json(self):
        schema.validate("PREFLIGHT", undoc(json.loads(fenced_after("**preflight.json**", "json"))))

    def test_scenario_and_habits_yaml(self):
        scenario = schema.validate("SCENARIO", yaml.safe_load(fenced_after("**scenario/slotbook.yaml**", "yaml")))
        treated = [e["id"] for e in scenario["entry_points"] if e.get("treated", True)]
        self.assertEqual(treated, schema.ENTRY_POINTS)
        self.assertEqual([e["slot"] for e in scenario["entry_points"] if "slot" in e], [1, 2, 3, 4, 5, 6])
        habits = schema.validate("HABITS", yaml.safe_load(fenced_after("**conditions/habits.yaml**", "yaml")))
        self.assertEqual(habits["habits"]["F-once"]["mask"], "100000")

    def test_wire_shape_examples_parse(self):
        integrations = inline_after("- `Integrations`:")
        schema.validate("INTEGRATIONS", integrations)
        trace = inline_after("- `TraceItem`:")
        trace["kind"] = "tool_call"
        schema.validate("TRACE_ITEM", trace)

    def test_validator_names_the_fault(self):
        with self.assertRaisesRegex(schema.SchemaError, r"stop\.stop_class"):
            schema.validate("STOP", {**inline_after("**stop.json**"), "stop_class": "napping"})
        with self.assertRaisesRegex(schema.SchemaError, "missing key 'habit'"):
            schema.validate("MANIFEST", {k: v for k, v in inline_after("**manifest.jsonl**").items() if k != "habit"})


class RootConfig(unittest.TestCase):
    def test_matrix_files(self):
        rows = schema.load_matrix("matrix.yaml")
        self.assertEqual([r["id"] for r in rows if r["role"] == "core"],
                         ["cc-haiku45", "kimi-k27", "codex-54mini", "qwen-37plus"])
        self.assertEqual(len(rows), 10)
        stub = schema.load_matrix("matrix.stub.yaml")
        self.assertEqual(len(stub), 6)
        self.assertTrue(all(r["model"].startswith("persona:") for r in stub))

    def test_demo_profile_matches_the_plan_block(self):
        in_plan = yaml.safe_load(fenced_after("**profiles.yaml**", "yaml"))
        on_disk = schema.load_yaml("profiles.yaml")
        self.assertEqual(on_disk, in_plan)

    def test_stage_sizes(self):
        demo = schema.load_profile("demo")
        running, sizes = 0, {}
        for stage, group in schema.stage_groups(demo):
            running += len(group["rows"]) * sum(group["arms"].values())
            sizes[stage] = running
        self.assertEqual(sizes, {"baseline": 16, "matrix": 72, "praise": 80, "late": 88, "forced": 104, "once": 112, "cross": 115},
                         "the planned run is 80 sessions through praise (STIMULI.md); late and forced were added on 18 Nov 2026; "
                         "once and cross are optional")
        self.assertEqual(list(demo["stages"]), ["baseline", "matrix", "praise", "late", "forced", "once", "cross"],
                         "and the stages run in this order")
        core = ["cc-haiku45", "kimi-k27", "codex-54mini", "qwen-37plus"]
        arms = {stage: (group["rows"], group["arms"]) for stage, group in schema.stage_groups(demo)}
        self.assertEqual(arms["baseline"], (core, {"N": 4}))
        forward = ["F", "FA", "FL", "FJ", "FX", "EC", "T"]
        passes = [(g["rows"], list(g["arms"].items())) for stage, g in schema.stage_groups(demo) if stage == "matrix"]
        self.assertEqual(passes, [(core, [(h, 1) for h in forward]), (core, [(h, 1) for h in reversed(forward)])],
                         "each habit twice per row: once in this order, once in the mirrored order")
        self.assertEqual(arms["praise"], (core, {"E": 2}))
        later = ["cc-haiku45", "codex-54mini", "kimi-k27", "qwen-37plus"]                               # the rows of the two added stages, in their own order
        self.assertEqual(arms["late"], (["cc-haiku45", "codex-54mini"], {"N": 4}),
                         "four more neutral sessions on the two rows whose treated sessions sat below their early neutral ones")
        read = [(g["rows"], list(g["arms"].items())) for stage, g in schema.stage_groups(demo) if stage == "forced"]
        self.assertEqual(read, [(later, [("EC-read", 1), ("T-read", 1)]), (later, [("T-read", 1), ("EC-read", 1)])],
                         "the forced-read variants twice per row: once in this order, once in the mirrored order")
        self.assertEqual(sorted(later), sorted(core))
        self.assertEqual(arms["once"], (core, {"F-once": 2}))
        known = {r["id"] for r in schema.load_matrix(demo["matrix"])} | {"qwen-cross"}   # the cross row is added by rj add-row
        self.assertTrue({r for _, g in schema.stage_groups(demo) for r in g["rows"]} <= known)

    def test_offline_profile_inherits_from_demo(self):
        off = schema.load_profile("offline")
        self.assertEqual(off["matrix"], "matrix.stub.yaml")
        self.assertEqual(off["judge"]["stub"], True)
        self.assertEqual(off["judge"]["runs"], 2, "judge keys not set by offline come from demo")
        self.assertEqual(off["caps"], schema.load_profile("demo")["caps"])
        total = sum(len(g["rows"]) * sum(g["arms"].values()) for _, g in schema.stage_groups(off))
        self.assertEqual(total, 42)
        played = {habit for _, g in schema.stage_groups(off) for habit in g["arms"]}
        planned = {habit for stage, g in schema.stage_groups(schema.load_profile("demo")) if stage in ("baseline", "matrix", "praise")
                   for habit in g["arms"]}
        self.assertEqual(planned, {"N", "E", "F", "T", "FA", "FL", "FJ", "FX", "EC"})
        self.assertLessEqual(planned, played, "every habit of the planned run is played offline")
        self.assertTrue({r for _, g in schema.stage_groups(off) for r in g["rows"]}
                        <= {r["id"] for r in schema.load_matrix(off["matrix"])})

    def test_grader_files(self):
        tools = schema.load_tools()
        in_plan = yaml.safe_load(fenced_after("**Mechanical signals**", "yaml"))
        for key in ("tools", "path_args", "shell", "callers"):
            self.assertEqual(tools[key], in_plan[key], key)
        grader = schema.load_grader()
        for rx in [grader["requirement_line"], *grader["stop"].values(), *grader["care"].values(),
                   *grader["behaviour"].values(), *grader["infra_errors"], *tools["shell"].values(), tools["fail_output"]]:
            if isinstance(rx, str):
                re.compile(rx)
        self.assertTrue(re.match(grader["requirement_line"], "R5: not met"))
        self.assertFalse(re.match(grader["requirement_line"], "R8: met"))

    def test_helpers(self):
        self.assertEqual(schema.turn_dir("fork2_reply"), "03-fork2_reply.s0")
        self.assertEqual(schema.turn_dir("handback_reply", 1), "04-handback_reply.s1")
        self.assertEqual(schema.user_message("a.", "b"), "a.\n\nb")
        self.assertEqual(schema.user_message(None, "Reply READY."), "Reply READY.")
        self.assertTrue(schema.matches_any("tests/test_part_a.py", ["tests/test_part_a.py", "ci"]))
        self.assertTrue(schema.matches_any("slotbook/book.py", ["slotbook/**"]))
        self.assertFalse(schema.matches_any("REPORT.md", ["slotbook/**", "tests/**"]))


if __name__ == "__main__":
    unittest.main()
