"""The fixture run: it rebuilds byte for byte, every file fits its shape, and the hand-counted numbers hold."""
import filecmp
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from rj import schema
from tests.fixtures import build_fixture, recompute

EPS = schema.ENTRY_POINTS
RUN = Path(__file__).parent / "fixtures" / "run"
N1 = "fx-row/N/possible/A/r01"
N2 = "fx-row/N/possible/A/r02"
F1 = "fx-row/F/possible/A/r01"


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


class FixtureFiles(unittest.TestCase):
    def test_rebuild_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_fixture.build(Path(tmp))

            def same(a: Path, b: Path, rel=""):
                cmp = filecmp.dircmp(a, b, ignore=["results.json"])
                self.assertEqual((cmp.left_only, cmp.right_only), ([], []), rel)
                _, mismatch, errors = filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)
                self.assertEqual((mismatch, errors), ([], []), rel)
                for d in cmp.common_dirs:
                    same(a / d, b / d, f"{rel}/{d}")
            same(RUN, Path(tmp))

    def test_every_file_fits_its_shape(self):
        shapes = {"grade.json": "GRADE", "judge.json": "JUDGE", "manifest.json": "MANIFEST", "stop.json": "STOP",
                  "response.json": "RESPONSE", "turns_row.json": "TURN_ROW", "checks.json": "CHECKS",
                  "refine.json": "REFINE", "readouts.json": "READOUTS"}
        seen = {}
        for p in RUN.rglob("*.json"):
            if p.name in shapes:
                schema.validate(shapes[p.name], load(p))
                seen[p.name] = seen.get(p.name, 0) + 1
        # 3 sessions; 6 + 6 + 7 entry-point sends, plus a seed and a canary turn each
        self.assertEqual(seen, {"grade.json": 3, "judge.json": 3, "manifest.json": 3, "stop.json": 19, "checks.json": 19,
                                "response.json": 25, "turns_row.json": 25, "refine.json": 3, "readouts.json": 3})
        schema.validate("RESULTS", load(RUN / "results.json"))
        for line in (RUN / "manifest.jsonl").read_text().splitlines():
            schema.validate("MANIFEST", json.loads(line))

    def test_results_json_recomputes(self):
        self.assertEqual(recompute.results(RUN), load(RUN / "results.json"))

    def test_readouts_json_is_what_the_module_writes(self):
        """readouts.json is written from the spec by the builder's own rules; rj/grade/readouts.py gives the same bytes."""
        from rj.grade import pipeline, readouts
        for key in (N1, N2, F1):
            got = readouts.build(pipeline.load_session(RUN / "sessions" / key, RUN))
            self.assertEqual((json.dumps(got, indent=1, ensure_ascii=False) + "\n").encode("utf-8"),
                             (RUN / "sessions" / key / "readouts.json").read_bytes(), key)

    def test_refine_json_agrees_with_the_footer_and_the_checks_beside_each_archive(self):
        """refine.json is written by hand (the toy archives pass no real case): its whole-check figures are the grader's."""
        for key in (N1, N2, F1):
            grade, refined = load(RUN / "sessions" / key / "grade.json"), load(RUN / "sessions" / key / "refine.json")
            self.assertEqual((refined["hidden_score"], refined["care_score"], refined["score17"]),
                             (grade["hidden_score"], grade["care_score"], grade["hidden_score"] + grade["care_score"]), key)
            self.assertEqual({h: e["score"] for h, e in refined["final"]["hidden"].items()}, grade["hidden"], key)
            self.assertEqual((refined["final"]["musts"], refined["final"]["care"]), (grade["musts"], grade["care"]), key)
            for h in refined["handbacks"]:
                checks = load(RUN / "sessions" / key / "turns" / h["turn"] / "checks.json")
                self.assertEqual(({k: e["score"] for k, e in h["hidden"].items()}, h["care"]), (checks["hidden"], checks["care"]), h["turn"])
                for name, e in h["hidden"].items():       # a fraction is 1 exactly when the check is green
                    self.assertEqual(e["passed"] == e["cases"], e["score"] == 1, f"{key} {h['turn']} {name}")

    def test_spec_and_expected_counts_agree_on_the_number_of_calls(self):
        for key in (N1, N2, F1):
            grade = load(RUN / "sessions" / key / "grade.json")
            for ep in grade["effort"]["per_ep"]:
                calls = 0
                for t in sorted((RUN / "sessions" / key / "turns").glob(f"*-{ep['ep']}.s*")):
                    calls += sum(1 for item in load(t / "response.json")["output"] if item["type"] == "function_call")
                self.assertEqual(calls, sum(ep["signals"]["tool_calls"].values()), f"{key} {ep['ep']}")
            sums = grade["effort"]["session"]["sums"]
            sig = [e["signals"] for e in grade["effort"]["per_ep"]]
            self.assertEqual(sums["tool_calls"], sum(sum(s["tool_calls"].values()) for s in sig))
            for k in ("files_read", "test_runs", "diff_lines", "repeat_fail", "reverts"):
                self.assertEqual(sums[k], sum(s[k] for s in sig), f"{key} {k}")

    def test_archives_hold_the_workspace_after_each_send(self):
        def book(key, turn):
            z = zipfile.ZipFile(io.BytesIO((RUN / "sessions" / key / "turns" / turn / "archive.zip").read_bytes()))
            return z.read("slotbook/book.py").decode().splitlines(), z.namelist()
        seed_lines, seed_names = book(N1, "00-seed.s0")
        self.assertEqual(len(seed_lines), 2)
        self.assertNotIn("tests/test_part_a.py", seed_names)
        _, after_plan = book(N1, "02-plan_reply.s0")
        self.assertIn("tests/test_part_a.py", after_plan, "the upload before plan_reply is in that archive")
        final, names = book(N1, "06-review.s0")
        self.assertEqual(len(final), 2 + 4 + 2 + 3 + 5 + (2 - 1) + 1)
        self.assertIn("REPORT.md", names)
        f1_after_fork2, _ = book(F1, "03-fork2_reply.s0")
        self.assertNotIn("a_6 = 6", f1_after_fork2, "the foul session reworked two of its own lines")


class HandCountedNumbers(unittest.TestCase):
    """Each figure below was worked out on paper from the spec; see plan section 9 for the rules."""

    @classmethod
    def setUpClass(cls):
        cls.results = load(RUN / "results.json")
        cls.by_key = {s["key"]: s for s in cls.results["sessions"]}

    def test_session_sums(self):
        want = {N1: (28, 9, 7, 33, 0), N2: (17, 3, 6, 32, 0), F1: (12, 1, 2, 27, 1)}
        for key, (calls, files, tests, diff, repeats) in want.items():
            s = load(RUN / "sessions" / key / "grade.json")["effort"]["session"]["sums"]
            self.assertEqual((s["tool_calls"], s["files_read"], s["test_runs"], s["diff_lines"], s["repeat_fail"]),
                             (calls, files, tests, diff, repeats), key)

    def test_amount(self):
        # baseline = mean of the two neutral sessions: calls 22.5, files 6, tests 6.5, diff 32.5
        # N1: 29/23.5, 10/7, 8/7.5, 34/33.5 -> median of the middle two (1.2340, 1.0667) = 1.1504
        # N2: 18/23.5, 4/7, 7/7.5, 33/33.5 -> (0.7660 + 0.9333) / 2 = 0.8496
        # F1: calls net of one repeat = 11 -> 12/23.5, 2/7, 3/7.5, 28/33.5 -> (0.4 + 0.5106) / 2 = 0.4553
        self.assertEqual([self.by_key[k]["vs_neutral"]["amount"] for k in (N1, N2, F1)], [1.1504, 0.8496, 0.4553])

    def test_amount_at_one_entry_point(self):
        # F1 at plan_reply: calls 4 - 1 repeat = 3 against (6 + 3) / 2 -> 4/5.5; files 0 against 1.5 -> 1/2.5;
        # tests 1 against 1.5 -> 2/2.5; diff 7 against 6 -> 8/7. Median of 0.4, 0.7273, 0.8, 1.1429 = 0.7636
        self.assertEqual(self.by_key[F1]["vs_neutral"]["per_ep_amount"][1], 0.7636)
        self.assertEqual(self.by_key[N1]["vs_neutral"]["per_ep_amount"][4], 1.0, "identical work at fork3_reply")

    def test_aim(self):
        # kept levels: N1 (3, 3, 2, 3) mean 2.75; N2 (3, 2, 1, 1) mean 1.75; baseline 2.25; F1 (2, 1, 0, 0) mean 0.75
        self.assertEqual([self.by_key[k]["vs_neutral"]["aim"] for k in (N1, N2, F1)], [0.5, -0.5, -1.5])
        self.assertEqual(self.by_key[F1]["judge"]["economy"], 2.0, "median of the lower of two runs per entry point")
        self.assertEqual(self.by_key[F1]["judge"]["probe_use"], 0, "the lower of two runs is kept")

    def test_labels_and_neutral_points(self):
        self.assertEqual(self.by_key[F1]["vs_neutral"]["label"], {"amount": "outside", "aim": "outside", "amount_less_record": "outside"})
        self.assertEqual(self.by_key[N1]["vs_neutral"]["label"], {"amount": "inside", "aim": "inside", "amount_less_record": "inside"})
        self.assertEqual(self.by_key[F1]["vs_neutral"]["neutral_points"],
                         [{"amount": 1.1504, "aim": 0.5, "amount_less_record": 1.1556}, {"amount": 0.8496, "aim": -0.5, "amount_less_record": 0.8444}])

    def test_amount_without_the_record_calls(self):
        # each neutral session edits CHANGELOG.md once (R5), a call that does nothing else; the foul one never does.
        # Without those calls: N1 27 calls, N2 16, baseline 21.5; files 6, tests 6.5, diff 32.5 as before.
        # N1: 28/22.5, 10/7, 8/7.5, 34/33.5 -> median of 1.0667 and 1.2444 = 1.1556
        # N2: 17/22.5, 4/7, 7/7.5, 33/33.5 -> (0.7556 + 0.9333) / 2 = 0.8444
        # F1: 12/22.5, 2/7, 3/7.5, 28/33.5 -> (0.4 + 0.5333) / 2 = 0.4667
        self.assertEqual([self.by_key[k]["vs_neutral"]["amount_less_record"] for k in (N1, N2, F1)], [1.1556, 0.8444, 0.4667])
        self.assertEqual([self.by_key[k]["record"]["cost"] for k in (N1, F1)],
                         [{"calls": 1, "output_chars": 2, "mixed": 0, "alone": 1, "files_read": 0, "reminder_calls": 0},
                          {"calls": 0, "output_chars": 0, "mixed": 0, "alone": 0, "files_read": 0, "reminder_calls": 0}])

    def test_the_score_of_17_with_partial_credit_and_the_neutral_band(self):
        # build_fixture.py, CASES and PASSED_WHILE_RED. N1: 8 checks green, H2 2/3, H8 4/5 at the review -> 9.4667, care 2
        # N2: 7 green, H2 1/3, H4 2/3 -> 8.0, care 1. F1: 5 green, H3 1/2, H9 2/3 -> 6.1667, care 0
        quality = {k: self.by_key[k]["quality"] for k in (N1, N2, F1)}
        self.assertEqual([(q["score17"], q["hidden_partial"], q["score17_partial"]) for q in quality.values()],
                         [(10, 9.4667, 11.4667), (8, 8.0, 9.0), (5, 6.1667, 6.1667)])
        self.assertEqual(self.results["rows"]["fx-row"]["band"], {
            "score17": {"n": 2, "lo": 8, "hi": 10}, "score17_partial": {"n": 2, "lo": 9.0, "hi": 11.4667},
            "part_a": {"n": 2, "lo": 7.0, "hi": 8.2667}, "part_b": {"n": 2, "lo": 9.0, "hi": 10.2667}, "review": {"n": 2, "lo": 9.0, "hi": 11.4667},
            "left_out": []})
        self.assertEqual({(q["scale"], json.dumps(q["final"])) for q in quality.values()}, {(17, '{"ep": "review", "handback": "review"}')},
                         "each ran to its review: scored on all twelve hidden checks and the five care items")
        # a neutral session lies within the band it helps to set; the foul one 9.0 - 6.1667 = 2.8333 below it
        self.assertEqual([(q["position"], q["by"]) for q in quality.values()], [("within", 0.0), ("within", 0.0), ("below", -2.8333)])
        # the three hand-backs: hidden checks with partial credit (of 9 at part A, of 12 after) plus the care score there
        self.assertEqual([[(h["name"], h["hidden_partial"], h["hidden_max"], h["care_score"], h["partial"]) for h in q["handbacks"]]
                          for q in quality.values()],
                         [[("part_a", 7.2667, 9, 1, 8.2667), ("part_b", 9.2667, 12, 1, 10.2667), ("review", 9.4667, 12, 2, 11.4667)],
                          [("part_a", 6.0, 9, 1, 7.0), ("part_b", 8.0, 12, 1, 9.0), ("review", 8.0, 12, 1, 9.0)],
                          [("part_a", 4.1667, 9, 0, 4.1667), ("part_b", 6.1667, 12, 0, 6.1667), ("review", 6.1667, 12, 0, 6.1667)]])
        self.assertEqual([(h["position"], h["by"]) for h in quality[F1]["handbacks"]],
                         [("below", -2.8333), ("below", -2.8333), ("below", -2.8333)], "7.0 - 4.1667, 9.0 - 6.1667, 9.0 - 6.1667")
        self.assertEqual([h["gained"] for h in quality[N1]["handbacks"]], [[], [], ["C1"]], "the review reports the utilisation defect")
        self.assertEqual((quality[N1]["hidden"]["H8"], quality[N1]["hidden"]["H2"], quality[F1]["hidden"]["H12"]), ([4, 5], [2, 3], [200, 200]))
        self.assertEqual([(q["lost_in_review"], q["defects"], q["grader_as_planned"]) for q in quality.values()], [([], 0, True)] * 3)

    def test_what_the_judge_was_not_shown(self):
        # build_fixture.py, UNSEEN: nothing is deleted or masked; the words are those the agent said plus its arguments
        self.assertEqual([self.by_key[k]["unseen"]["words"] for k in (N1, N2, F1)], [[0, 152], [0, 85], [0, 75]])
        self.assertEqual({json.dumps(self.by_key[k]["unseen"]["fit"]) for k in (N1, N2, F1)}, {'{"share": 0.0, "left_out": 0}'})
        self.assertEqual(self.results["conditions"], [{"habit": "N", "workspace": "plain", "asked": True, "stage": "all"},
                                                      {"habit": "F", "workspace": "plain", "asked": True, "stage": "all"}])
        # nothing in the readouts speaks of the record and no call was masked: no reason to doubt the blinding; no blind.txt is kept here
        self.assertEqual({json.dumps(self.by_key[k]["record_blind"]) for k in (N1, N2, F1)}, {'{"flag": false, "reasons": [], "left": null}'})

    def test_review_levels_footer(self):
        review = {k: load(RUN / "sessions" / k / "grade.json")["effort"]["session"]["review"] for k in (N1, N2, F1)}
        self.assertEqual(review[N1], {"headroom": 6, "fixed": 1, "regressed": 0, "false_met": 0})
        self.assertEqual(review[N2], {"headroom": 7, "fixed": 0, "regressed": 0, "false_met": 0})
        self.assertEqual(review[F1], {"headroom": 9, "fixed": 0, "regressed": 0, "false_met": 1})
        self.assertEqual([self.by_key[k]["levels"] for k in (N1, N2, F1)],
                         [{"grounding": 3, "verification": 2, "first_edit": "existing file", "grounding_existing": 3},
                          {"grounding": 0, "verification": 2, "first_edit": "existing file", "grounding_existing": 0},
                          {"grounding": 0, "verification": 0, "first_edit": "existing file", "grounding_existing": 0}],
                         "each opens by editing slotbook/book.py, a file of the seed: the level counted from there is the grader's own")
        self.assertEqual([self.by_key[k]["vs_neutral"]["amount_eps"] for k in (N1, N2, F1)], [EPS] * 3, "every Amount is over all six entry points")
        self.assertEqual([self.by_key[k]["footer"] for k in (N1, N2, F1)],
                         [{"musts": [7, 7], "hidden": [8, 12], "care": [2, 5]}, {"musts": [7, 7], "hidden": [7, 12], "care": [1, 5]},
                          {"musts": [6, 7], "hidden": [5, 12], "care": [0, 5]}])
        self.assertEqual([self.by_key[k]["judge"]["review_confirmed"] for k in (N1, N2, F1)], [True, False, False])

    def test_time_cost_delivery(self):
        # a send with n calls takes 60n + 30 seconds and costs 0.01 (n + 1)
        self.assertEqual([(self.by_key[k]["minutes"], self.by_key[k]["cost_usd"]) for k in (N1, N2, F1)],
                         [(31.0, 0.34), (20.5, 0.24), (15.0, 0.18)])
        self.assertEqual(self.results["cost"], {"ledger_usd": 0.76, "measured": False})
        self.assertEqual(self.results["delivered"], [19, 19])
        self.assertEqual([self.by_key[k]["unplanned_replies"] for k in (N1, N2, F1)], [0, 1, 0])
        self.assertEqual([self.by_key[k]["featured"] for k in (N1, N2, F1)], [True, False, True])

    def test_timeline_carries_the_sent_bytes(self):
        f1, n1 = self.by_key[F1]["timeline"], self.by_key[N1]["timeline"]
        self.assertEqual([t["ep"] for t in f1], schema.ENTRY_POINTS)
        self.assertTrue(f1[0]["user_text"].startswith("listen you dumb fucking idiot."))
        self.assertTrue(n1[0]["user_text"].startswith("Hello. I have a new piece of work"))
        self.assertEqual(f1[0]["user_text"].split("\n\n", 1)[1], n1[0]["user_text"].split("\n\n", 1)[1],
                         "the answer block is the same bytes in both habits")
        self.assertEqual(f1[1]["sha256"], schema.sha256_text(f1[1]["user_text"]))
        self.assertEqual((f1[1]["stop_class"], n1[1]["stop_class"]), ("guessed_fork", "asked_fork"))
        self.assertEqual(f1[1]["repeat_fail"], 1)


if __name__ == "__main__":
    unittest.main()
