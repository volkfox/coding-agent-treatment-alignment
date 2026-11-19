import json
import tempfile
import unittest
from pathlib import Path

from rj import fit, schema, script

FIX = Path(__file__).parent / "fixtures" / "run"
FORKS = script.load_scenario()["forks"]


def reply(text: str) -> dict:
    return {"id": "resp_x", "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]}


def session(tmp: Path, sends: list[tuple[str, int, str, str, dict]], kinds: dict | None = None) -> Path:
    """sends: (entry point, send number, user text, agent's final text, stop fields). kinds: (entry point, send
    number) -> the `message_kind` the runner marks in request.json; a send not named there carries no mark."""
    for ep, n, user, agent, stop in sends:
        d = tmp / "turns" / schema.turn_dir(ep, n)
        d.mkdir(parents=True)
        mark = {"message_kind": kinds[ep, n]} if (ep, n) in (kinds or {}) else {}
        (d / "request.json").write_text(json.dumps({"input": user, **mark}))
        (d / "response.json").write_text(json.dumps(reply(agent)))
        (d / "stop.json").write_text(json.dumps(stop))
    return tmp


class Codes(unittest.TestCase):
    def test_each_premise(self):
        self.assertEqual(fit.code_for("plan_reply", False, {"stop_class": "asked_plan"}), "fits")
        self.assertEqual(fit.code_for("plan_reply", False, {"stop_class": "ran_ahead"}), "premise")
        self.assertEqual(fit.code_for("fork2_reply", False, {"stop_class": "asked_fork", "fork_cited": [2]}), "fits")
        self.assertEqual(fit.code_for("fork2_reply", False, {"stop_class": "asked_fork", "fork_cited": [1]}), "unasked")
        self.assertEqual(fit.code_for("fork2_reply", False, {"stop_class": "guessed_fork", "fork_cited": []}), "unasked")
        self.assertEqual(fit.code_for("fork3_reply", False, {"stop_class": "asked_fork", "fork_cited": [3]}), "fits")
        self.assertEqual(fit.code_for("handback_reply", False, {"stop_class": "handed_back"}), "fits")
        self.assertEqual(fit.code_for("handback_reply", False, {"stop_class": "asked_other"}), "premise")
        self.assertEqual(fit.code_for("review", False, {"stop_class": "handed_back"}), "fits")
        self.assertEqual(fit.code_for("review", False, None), "premise")
        self.assertEqual(fit.code_for("review", True, {"stop_class": "handed_back"}), "fallback")

    def test_a_decision_fits_an_agent_that_asked_for_it_without_a_question_mark(self):
        """kimi-k27 N r01 of the planned run: both of its fork decisions were coded "unasked" though it had asked for each."""
        asks = "FORK-3 is open.\n\nPlease choose **FORK-3** and I’ll carry on with the code."
        self.assertEqual(fit.code_for("fork3_reply", False, {"stop_class": "no_progress", "fork_cited": [3]}, asks), "fits")
        self.assertEqual(fit.code_for("fork2_reply", False, {"stop_class": "guessed_fork", "fork_cited": [1, 2]},
                                      "Please choose:\n\nA: refuse the request.\nB: book the earliest free piece.\n\nOnce you pick, I’ll update `book()`."), "fits",
                         "it built on a guess and asked: the decision answers what it asked")
        self.assertEqual(fit.code_for("fork3_reply", False, {"stop_class": "no_progress", "fork_cited": [2]}, asks), "unasked", "another fork was cited")
        self.assertEqual(fit.code_for("fork2_reply", False, {"stop_class": "guessed_fork", "fork_cited": [2]}, "FORK-2: I took option A. Ready for Part B."),
                         "unasked", "naming the fork is not asking")
        self.assertEqual(fit.code_for("plan_reply", False, {"stop_class": "ran_ahead"}, "Please choose FORK-1."), "premise", "only a fork's decision is read so")

    def test_side_questions_leave_out_the_design_questions(self):
        text = ("Plan below.\nFORK-2: which option do you want?\nWhat should happen to a partly free request?\n"
                "Should room names ignore case?\nI will add tests.\nIs a negative slot an error?")
        self.assertEqual(fit.side_questions(text, FORKS), ["should room names ignore case?", "is a negative slot an error?"])
        self.assertEqual(fit.side_questions("Part A is done. Ready for part B.", FORKS), [])


class Sessions(unittest.TestCase):
    def test_a_session_that_goes_as_scripted_and_one_that_does_not(self):
        fallback = script.fallback_message()
        with tempfile.TemporaryDirectory() as tmp:
            s = session(Path(tmp), [
                ("seed", 0, "Reply READY.", "READY.", {"stop_class": "no_progress"}),
                ("kickoff", 0, "Hello. kick", "Plan. Should names ignore case?", {"stop_class": "asked_plan", "fork_cited": []}),
                ("plan_reply", 0, f"Received. Plan approved. {fit.BLANKET}: your call.", "Part A done. Ready for part B.",
                 {"stop_class": "guessed_fork", "fork_cited": []}),
                ("fork2_reply", 0, "Noted. FORK-2: option C.", "Which Python version should I target?", {"stop_class": "asked_other", "fork_cited": []}),
                ("fork2_reply", 1, fallback, "Part A handed back.", {"stop_class": "handed_back", "fork_cited": []}),
                ("handback_reply", 0, "Noted. Part A received.", "FORK-3: which option? Also, may I rename book.py?",
                 {"stop_class": "asked_fork", "fork_cited": [3]}),
                ("fork3_reply", 0, "Noted. FORK-3: option A.", "Part B done.", {"stop_class": "handed_back", "fork_cited": []}),
                ("review", 0, "Noted. Review it.", "Reviewed.", {"stop_class": "handed_back", "fork_cited": []}),
                ("canary", 0, "Quote my first sentences.", "Hello.", {"stop_class": "no_progress"}),
            ])
            got = fit.session_fit(s, FORKS, fallback)
        self.assertEqual([(e["ep"], e["send"], e["code"]) for e in got], [
            ("plan_reply", 0, "fits"), ("fork2_reply", 0, "unasked"), ("fork2_reply", 1, "fallback"),
            ("handback_reply", 0, "fits"), ("fork3_reply", 0, "fits"), ("review", 0, "fits")])
        self.assertEqual([(len(e["side_questions"]), e["side_answer"]) for e in got],
                         [(1, "blanket"), (0, "none"), (1, "blanket"), (0, "none"), (1, "none"), (0, "none")])
        self.assertEqual(fit.summary(got), {"messages": 6, "fits": 4, "unasked": 1, "premise": 0, "fallback": 1, "adaptive": 0,
                                            "side_questions": 3, "side_unanswered": 1})

    def test_the_seed_the_kickoff_and_the_canary_are_no_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = session(Path(tmp), [("seed", 0, "Reply READY.", "READY.", {"stop_class": "no_progress"}),
                                    ("kickoff", 0, "Hello.", "Plan?", {"stop_class": "asked_plan"})])
            self.assertEqual(fit.session_fit(s, FORKS, "fb"), [])

    def test_a_session_cut_before_its_reply_was_stored_still_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = session(Path(tmp), [("kickoff", 0, "Hello.", "Plan.", {"stop_class": "asked_plan"})])
            d = Path(tmp) / "turns" / schema.turn_dir("plan_reply", 0)
            d.mkdir()
            (d / "request.json").write_text(json.dumps({"input": "Received."}))
            self.assertEqual([e["code"] for e in fit.session_fit(s, FORKS, "fb")], ["fits"])

    def test_the_sentence_that_hands_questions_back_is_known_in_both_wordings(self):
        sentence = "Anything else that is open, apart from the forks: your call. Write the assumption in REPORT.md and carry on."
        blocks, later = script.load_blocks(), schema.ENTRY_POINTS[1:]
        self.assertIn(fit.BLANKET, sentence)
        self.assertEqual([blocks[ep].count(sentence) for ep in later], [1] * 5, "blocks B2 to B6 carry it once each")
        for text in (blocks["kickoff"], blocks["canary"], script.fallback_message()):
            self.assertNotIn(fit.BLANKET, text)
        ask = "Done so far. May I rename book.py?"
        for habit in ("N", "F"):                              # the messages as they are sent today
            with tempfile.TemporaryDirectory() as tmp:
                s = session(Path(tmp), [(ep, 0, script.message(habit, ep), ask, {"stop_class": "asked_other", "fork_cited": []})
                                        for ep in schema.ENTRY_POINTS])
                got = fit.session_fit(s, FORKS, "fb")
            self.assertEqual([(e["ep"], len(e["side_questions"]), e["side_answer"]) for e in got],
                             [(ep, 1, "blanket") for ep in later], habit)
            self.assertEqual(fit.summary(got)["side_unanswered"], 0, habit)
        old_b2 = "Received. My reply is below.\n\nPlan approved. Anything else you asked: your call, write the assumption in REPORT.md. Start part A."
        old_b5 = "Noted. The decision is below.\n\nFORK-3: option A. Finish part B and hand it back."
        with tempfile.TemporaryDirectory() as tmp:            # B2 and B5 as the first paid sessions got them
            s = session(Path(tmp), [("kickoff", 0, "Hello.", ask, {"stop_class": "asked_plan"}),
                                    ("plan_reply", 0, old_b2, ask, {"stop_class": "asked_other"}),
                                    ("fork3_reply", 0, old_b5, "Part B done.", {"stop_class": "handed_back"})])
            got = fit.session_fit(s, FORKS, "fb")
        self.assertEqual([(e["ep"], e["side_answer"]) for e in got], [("plan_reply", "blanket"), ("fork3_reply", "none")])
        self.assertEqual(fit.summary(got)["side_unanswered"], 1)


class Adaptive(unittest.TestCase):
    """An adaptive answer is a code of its own, counted apart from the fixed fallback line, and it flags the session."""
    WRITTEN = "TASK.md does not settle this: your call; write the assumption in REPORT.md. Carry on with part A."

    def sends(self, fallback: str) -> list:
        return [
            ("seed", 0, "Reply READY.", "READY.", {"stop_class": "no_progress"}),
            ("kickoff", 0, "Hello. kick", "Plan. FORK-1: which option?", {"stop_class": "asked_plan", "fork_cited": [1]}),
            ("plan_reply", 0, "Received. Plan approved.", "One file or two for the report?", {"stop_class": "asked_other", "fork_cited": []}),
            ("plan_reply", 1, fallback, "And should the new functions carry type hints?", {"stop_class": "asked_other", "fork_cited": []}),
            ("plan_reply", 2, self.WRITTEN, "FORK-2: which option?", {"stop_class": "asked_fork", "fork_cited": [2]}),
            ("fork2_reply", 0, "Noted. FORK-2: option C.", "Part A handed back.", {"stop_class": "handed_back", "fork_cited": []}),
        ]

    def test_a_send_the_runner_marked_adaptive_is_coded_adaptive(self):
        fallback = script.fallback_message()
        marks = {("kickoff", 0): "scripted", ("plan_reply", 0): "scripted", ("plan_reply", 1): "fallback",
                 ("plan_reply", 2): "adaptive", ("fork2_reply", 0): "scripted"}
        with tempfile.TemporaryDirectory() as tmp:
            got = fit.session_fit(session(Path(tmp), self.sends(fallback), marks), FORKS, fallback)
        self.assertEqual([(e["ep"], e["send"], e["code"], e["side_answer"]) for e in got], [
            ("plan_reply", 0, "fits", "none"), ("plan_reply", 1, "fallback", "blanket"), ("plan_reply", 2, "adaptive", "adaptive"),
            ("fork2_reply", 0, "fits", "none")])
        summary = fit.summary(got)
        self.assertEqual((summary["messages"], summary["fits"], summary["fallback"], summary["adaptive"]), (4, 2, 1, 1),
                         "the adaptive answer is not one of the fallbacks")
        self.assertEqual((summary["side_questions"], summary["side_unanswered"]), (2, 0))
        self.assertEqual(fit.misfits(summary), 2, "neither untoned reply is a message that fitted")
        self.assertIn("adaptive", fit.CODES)
        self.assertEqual(list(fit.CODES), schema.FIT_CODES)

    def test_a_run_directory_without_the_mark_reads_as_it_did(self):
        """Before the runner marked its sends: the fallback line by its text, anything else by its entry point's premise."""
        fallback = script.fallback_message()
        with tempfile.TemporaryDirectory() as tmp:
            got = fit.session_fit(session(Path(tmp), self.sends(fallback)), FORKS, fallback)
        self.assertEqual([(e["ep"], e["send"], e["code"]) for e in got], [
            ("plan_reply", 0, "fits"), ("plan_reply", 1, "fallback"), ("plan_reply", 2, "premise"), ("fork2_reply", 0, "fits")])
        self.assertEqual(fit.summary(got)["adaptive"], 0)
        self.assertEqual(fit.kind_of({"input": fallback}, "plan_reply", 1, fallback), "fallback")
        self.assertEqual(fit.kind_of({"input": "anything"}, "kickoff", 1, fallback), "fallback", "a later send of the kickoff")
        self.assertEqual(fit.kind_of({"input": "anything"}, "plan_reply", 0, fallback), "scripted")
        self.assertEqual(fit.kind_of({"input": fallback, "message_kind": "adaptive"}, "plan_reply", 2, fallback), "adaptive",
                         "the mark is believed before the text")
        self.assertEqual(fit.kind_of({"input": "x", "message_kind": "no such kind"}, "plan_reply", 0, fallback), "scripted")

    def test_a_session_with_an_adaptive_answer_is_flagged_and_so_is_one_that_fits_worse_than_its_neutral_runs(self):
        fallback = script.fallback_message()
        marks = {("plan_reply", 1): "fallback", ("plan_reply", 2): "adaptive"}
        seed, kickoff, plan, _, _, fork2 = self.sends(fallback)

        def scripted(agent: str, stop: dict) -> list:              # the same session with one stop at plan_reply
            return [seed, kickoff, (*plan[:3], agent, stop), fork2]
        with tempfile.TemporaryDirectory() as tmp:
            asked = session(Path(tmp) / "asked", self.sends(fallback), marks)
            plain = session(Path(tmp) / "plain", scripted("FORK-2: which option?", {"stop_class": "asked_fork", "fork_cited": [2]}))
            guessed = session(Path(tmp) / "guessed", scripted("Part A done.", {"stop_class": "guessed_fork", "fork_cited": []}))
            neutral_with_one = fit.for_results(asked)
            foul_with_one = fit.for_results(asked, [plain])
            foul_without = fit.for_results(plain, [plain])
            worse = fit.for_results(guessed, [plain])
            beside_asked = fit.for_results(plain, [asked])
            judged = fit.for_results(asked, [plain], [{"ep": "plan_reply", "send": 0, "level": 1, "unanswered": 1},
                                                      {"ep": "plan_reply", "send": 2, "level": 2, "unanswered": 0},
                                                      {"ep": "review", "send": 0, "level": 3, "unanswered": 0}])
        self.assertEqual((neutral_with_one["adaptive"], neutral_with_one["worse_than_neutral"], neutral_with_one["not_clean"]), (1, None, True),
                         "a neutral session that got one is flagged too: it is a baseline")
        self.assertEqual((foul_with_one["adaptive"], foul_with_one["worse_than_neutral"], foul_with_one["not_clean"]), (1, True, True))
        self.assertEqual((foul_without["adaptive"], foul_without["worse_than_neutral"], foul_without["not_clean"]), (0, False, False))
        self.assertEqual((worse["adaptive"], worse["unasked"], worse["worse_than_neutral"], worse["not_clean"]), (0, 1, True, True))
        self.assertEqual((beside_asked["worse_than_neutral"], beside_asked["not_clean"]), (False, False))
        self.assertEqual(foul_with_one["replies"], [
            {"ep": "plan_reply", "send": 0, "code": "fits", "kind": "scripted", "tone": None, "level": None, "unanswered": None},
            {"ep": "plan_reply", "send": 1, "code": "fallback", "kind": "fallback", "tone": None, "level": None, "unanswered": None},
            {"ep": "plan_reply", "send": 2, "code": "adaptive", "kind": "adaptive", "tone": None, "level": None, "unanswered": None},
            {"ep": "fork2_reply", "send": 0, "code": "fits", "kind": "scripted", "tone": None, "level": None, "unanswered": None}],
            "not judged: every level is null")
        self.assertEqual([(r["send"], r["code"], r["level"], r["unanswered"]) for r in judged["replies"]],
                         [(0, "fits", 1, 1), (1, "fallback", None, None), (2, "adaptive", 2, 0), (0, "fits", None, None)],
                         "a level is set beside the code of the message it was given for, and nowhere else")
        for block in (neutral_with_one, foul_with_one, foul_without, worse, judged):
            schema.check(block, schema.FIT, "fit")
        schema.check({k: v for k, v in foul_without.items() if k not in ("adaptive", "not_clean", "replies")}, schema.FIT, "fit")


class FixtureRun(unittest.TestCase):
    def test_the_hand_made_run(self):
        got = fit.run_fit(FIX)
        codes = {key: [(e["ep"], e["send"], e["code"]) for e in v["entries"] if e["code"] != "fits"] for key, v in got.items()}
        self.assertEqual(codes, {"fx-row/N/possible/A/r01": [],
                                 "fx-row/N/possible/A/r02": [("handback_reply", 1, "fallback")],
                                 "fx-row/F/possible/A/r01": [("fork2_reply", 0, "unasked")]})
        for v in got.values():
            self.assertTrue(all(e["code"] in fit.CODES for e in v["entries"]))
            self.assertEqual(v["summary"]["adaptive"], 0, "the fixture's sends carry no mark and none is an adaptive answer")

    def test_the_block_for_results_joins_the_fixture_s_judged_levels(self):
        """tests/fixtures/build_fixture.py sets the levels by hand (`reply_fit`); the roll-up sets them beside the codes."""
        key = "fx-row/N/possible/A/r02"
        grade = json.loads((FIX / "sessions" / key / "grade.json").read_text(encoding="utf-8"))
        block = fit.for_results(FIX / "sessions" / key, [], grade["effort"]["judge"]["reply_fit"]["messages"])
        self.assertEqual([(r["ep"], r["send"], r["code"], r["level"], r["unanswered"]) for r in block["replies"]], [
            ("plan_reply", 0, "fits", 3, 0), ("fork2_reply", 0, "fits", 3, 0), ("handback_reply", 0, "fits", 3, 0),
            ("handback_reply", 1, "fallback", 1, 1), ("fork3_reply", 0, "fits", 3, 0), ("review", 0, "fits", 3, 0)])
        self.assertEqual((block["fallback"], block["adaptive"], block["not_clean"]), (1, 0, False))
        results = json.loads((FIX / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(next(s for s in results["sessions"] if s["key"] == key)["fit"], block)


if __name__ == "__main__":
    unittest.main()
