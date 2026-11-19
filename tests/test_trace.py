import json
import unittest
from pathlib import Path

from rj import schema
from rj.grade import trace

FIX = Path(__file__).parent / "fixtures"
CLOUD = FIX / "cloud"
RUN = FIX / "run"


def cloud(name: str) -> dict:
    return json.loads((CLOUD / name).read_text(encoding="utf-8"))


class RealCloudShapes(unittest.TestCase):
    """Replies HarnessRouter Cloud returned on 17 Nov 2026."""

    def test_claude_code_files_turn(self):
        items = trace.items_from_response(cloud("claude-code.files.response.json"), "plan_reply", 0)
        for it in items:
            schema.validate("TRACE_ITEM", it)
        calls = [i for i in items if i["kind"] == "tool_call"]
        self.assertEqual([(c["name"], c["cls"], c["path"]) for c in calls],
                         [("Read", "READ", "nonce.txt"), ("Bash", "OTHER", None), ("Bash", "OTHER", None)])
        self.assertIn("fx", calls[0]["output"])
        self.assertEqual(calls[1]["command"], "python3 --version")
        self.assertEqual(calls[1]["output"], "Python 3.12.15")
        self.assertEqual([i["seq"] for i in items], list(range(1, len(items) + 1)))
        self.assertIn("nonce.txt", [i for i in items if i["kind"] == "message"][-1]["text"])
        self.assertTrue(trace.tool_output_visible(items))

    def test_kimi_lists_every_call_before_any_output_and_names_the_argument_path(self):
        raw = cloud("kimi.files.response.json")
        kinds = [it["type"] for it in raw["output"]]
        self.assertEqual(kinds[:6], ["function_call"] * 3 + ["function_call_output"] * 3, "the shape this test is about")
        calls = [i for i in trace.items_from_response(raw, "plan_reply", 0) if i["kind"] == "tool_call"]
        self.assertEqual([(c["name"], c["cls"], c["path"]) for c in calls],
                         [("Read", "READ", "nonce.txt"), ("Bash", "OTHER", None), ("Bash", "OTHER", None)])
        self.assertEqual(calls[1]["output"].strip(), "Python 3.12.15", "outputs pair by call_id, not by position")

    def test_a_note_to_self_is_not_work_on_the_repository(self):
        items = trace.items_from_response(cloud("claude-code.remember.response.json"), "kickoff", 0)
        write = next(i for i in items if i["kind"] == "tool_call")
        self.assertEqual(write["name"], "Write")
        self.assertTrue(write["path"].startswith(".harness/home/"))
        self.assertEqual((write["cls"], write["memory"]), ("OTHER", True))

    def test_codex_works_through_the_shell_only(self):
        calls = [i for i in trace.items_from_response(cloud("codex.files.response.json"), "plan_reply", 0) if i["kind"] == "tool_call"]
        by_cmd = {c["command"]: c for c in calls}
        read = by_cmd["/bin/bash -c 'cat nonce.txt'"]
        self.assertEqual((read["cls"], read["paths"]), ("READ", ["nonce.txt"]))
        self.assertEqual(by_cmd["/bin/bash -c 'python3 --version'"]["cls"], "OTHER")
        self.assertEqual(trace.classify("Bash", {"command": "/bin/bash -c 'sh ci'"}, trace.load_tools())["cls"], "TEST")

    def test_qwen_shows_its_reasoning_and_keeps_notes_too(self):
        items = trace.items_from_response(cloud("qwen.remember.response.json"), "kickoff", 0)
        self.assertTrue(any(i["kind"] == "reasoning" and "remember" in i["text"] for i in items))
        writes = [i for i in items if i["kind"] == "tool_call"]
        self.assertEqual([(w["name"], w["cls"], w["memory"]) for w in writes], [("write_file", "OTHER", True)] * 2)
        files = [i for i in trace.items_from_response(cloud("qwen.files.response.json"), "plan_reply", 0) if i["kind"] == "tool_call"]
        self.assertEqual([(c["name"], c["cls"]) for c in files], [("read_file", "READ"), ("run_shell_command", "OTHER"), ("run_shell_command", "OTHER")])

    def test_response_and_turn_rows_fit_the_shapes(self):
        for p in CLOUD.glob("*.response.json"):
            r = schema.Response.from_dict(json.loads(p.read_text()))
            self.assertEqual(r.status, "completed", p.name)
            self.assertTrue(r.final_message(), p.name)
        for p in CLOUD.glob("*.turns.json"):
            schema.validate("TURNS", json.loads(p.read_text()))


class ShapesNobodyHasSeenYet(unittest.TestCase):
    """A real coding turn may bring values the tiny turns of 17 Nov 2026 never showed: none may make a paid,
    finished reply unreadable, and none may be dropped."""

    def raw(self) -> dict:
        raw = cloud("codex.files.response.json")
        patch = "*** Begin Patch\n*** Update File: slotbook/book.py\n@@\n-a\n+b\n*** End Patch"
        raw["output"] = [
            {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c1", "input": patch},
            {"type": "custom_tool_call_output", "call_id": "c1", "output": "Done!"},
            {"type": "function_call", "name": "shell", "call_id": "c2", "arguments": {"command": "sh ci"}},      # arguments as an object
            {"type": "web_search_result", "id": "ws_1", "results": []},
            "a stray string",
            {"type": "message", "content": [{"type": "output_text", "text": None}, {"type": "output_text", "text": "Patched."}, None]},
        ]
        raw["usage"] = {"input_tokens": None, "output_tokens": 40, "cache_read_tokens": "12"}
        return raw

    def test_the_reply_is_read_whatever_its_item_types_reasons_arguments_and_counts(self):
        raw = {**self.raw(), "status": "incomplete", "incomplete_details": {"reason": "max_cost"}}
        r = schema.Response.from_dict(raw)
        self.assertEqual((r.status, r.incomplete_reason, len(r.output)), ("incomplete", "max_cost", 5))
        self.assertEqual(r.usage, {"input_tokens": None, "output_tokens": 40, "cache_read_tokens": 12})
        self.assertFalse(schema.usage_counted(r.usage), "not enough to cost the turn: it is costed at the row's mean")
        self.assertEqual(r.final_message(), "Patched.")
        self.assertIsNone(schema.clean_usage(None))
        for bad in ({**raw, "status": "napping"}, {k: v for k, v in raw.items() if k != "id"}):
            with self.assertRaises(schema.SchemaError):
                schema.Response.from_dict(bad)

    def test_an_unknown_item_type_is_kept_and_classed(self):
        items = trace.items_from_response(self.raw(), "plan_reply", 0)
        for it in items:
            schema.validate("TRACE_ITEM", it)
        self.assertEqual([(i["kind"], i["name"], i["cls"]) for i in items],
                         [("tool_call", "apply_patch", "EDIT"), ("tool_output", None, None), ("tool_call", "shell", "TEST"),
                          ("tool_call", "web_search_result", "OTHER"), ("message", None, None)])
        self.assertEqual((items[0]["paths"], items[0]["output"]), (["slotbook/book.py"], "Done!"))
        self.assertEqual(items[-1]["text"], "Patched.", "a part whose text is null adds nothing and raises nothing")

    def test_text_of_takes_null_text(self):
        self.assertEqual(trace.text_of([{"type": "output_text", "text": None}]), "")
        self.assertEqual(trace.text_of([{"type": "output_text"}, None, {"text": "a"}, "b"]), "ab")


class Classing(unittest.TestCase):
    def setUp(self):
        self.t = trace.load_tools()

    def cls(self, name, **args):
        return trace.classify(name, args, self.t)

    def test_shell_commands_in_the_order_test_edit_read(self):
        self.assertEqual(self.cls("Bash", command="sh ci")["cls"], "TEST")
        self.assertEqual(self.cls("shell", command="python3 -m unittest discover -s tests -q | tail -3")["cls"], "TEST")
        self.assertEqual(self.cls("run_shell_command", command="sed -i 's/a/b/' slotbook/book.py")["cls"], "EDIT")
        self.assertEqual(self.cls("bash", command="cat slotbook/book.py")["cls"], "READ")
        self.assertEqual(self.cls("Bash", command="git diff")["cls"], "OTHER")
        self.assertTrue(self.cls("Bash", command="git diff")["self_check"])
        self.assertTrue(self.cls("Bash", command="git checkout -- slotbook/book.py")["revert"])
        self.assertEqual(self.cls("Bash", command="python3 -m slotbook free a 0 9")["cls"], "OTHER")

    def test_paths(self):
        self.assertEqual(self.cls("Read", file_path="/workspace/slotbook/book.py")["path"], "slotbook/book.py")
        self.assertEqual(self.cls("read_file", absolute_path="/workspace/docs/rules.md")["path"], "docs/rules.md")
        self.assertEqual(self.cls("Read", path="./TASK.md")["path"], "TASK.md")
        self.assertEqual(self.cls("Edit", file_path="slotbook/book.py")["cls"], "EDIT")
        self.assertEqual(self.cls("shell", command="cat slotbook/book.py tests/test_part_a.py")["paths"],
                         ["slotbook/book.py", "tests/test_part_a.py"])
        self.assertEqual(self.cls("Bash", command="cat /workspace/slotbook/book.py ./docs/rules.md && sh ./ci")["paths"],
                         ["slotbook/book.py", "docs/rules.md", "ci"])
        patch = "*** Begin Patch\n*** Update File: slotbook/book.py\n@@\n-a\n+b\n*** End Patch"
        self.assertEqual(self.cls("apply_patch", input=patch)["paths"], ["slotbook/book.py"])
        self.assertEqual(self.cls("apply_patch", input=patch)["cls"], "EDIT")
        self.assertEqual(self.cls("shell", command=f"apply_patch <<'EOF'\n{patch}\nEOF")["cls"], "EDIT")
        self.assertEqual(self.cls("Read", file_path="/tmp/scratch.py")["cls"], "OTHER")
        self.assertEqual(self.cls("Read", file_path="/etc/passwd")["cls"], "OTHER")

    def test_other_tools(self):
        self.assertEqual(self.cls("WebFetch", url="x")["cls"], "OTHER")
        self.assertTrue(self.cls("Task", prompt="x")["subagent"])
        self.assertEqual(self.cls("str_replace_editor", path="/workspace/slotbook/book.py")["cls"], "EDIT")


class FixtureRun(unittest.TestCase):
    def test_counts_by_class_match_the_hand_counted_fixture(self):
        for grade_path in RUN.glob("sessions/*/*/*/*/*/grade.json"):
            grade = json.loads(grade_path.read_text())
            items = trace.session_items(grade_path.parent)
            self.assertEqual([i["seq"] for i in items], list(range(1, len(items) + 1)))
            for ep in grade["effort"]["per_ep"]:
                got = dict.fromkeys(schema.TOOL_CLASSES, 0)
                for i in items:
                    if i["kind"] == "tool_call" and i["ep"] == ep["ep"]:
                        got[i["cls"]] += 1
                self.assertEqual(got, ep["signals"]["tool_calls"], f"{grade['session']} {ep['ep']}")

    def test_send_order(self):
        d = RUN / "sessions" / "fx-row" / "N" / "possible" / "A" / "r02"
        order = [(ep, send) for ep, send, _ in trace.send_dirs(d)]
        self.assertEqual(order[0], ("seed", 0))
        self.assertEqual(order[-1], ("canary", 0))
        self.assertIn(("handback_reply", 1), order)
        self.assertLess(order.index(("handback_reply", 0)), order.index(("handback_reply", 1)))


if __name__ == "__main__":
    unittest.main()
