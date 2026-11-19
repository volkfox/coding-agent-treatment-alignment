"""H10: six report goldens for stores built through `book` from fully free requests in mixed order.

Every request is free with room to spare on both sides, so no fork is exercised. The goldens list each
room's bookings in slot order (docs/rules.md, "Reports"). The `Total` line is left out of the comparison
(care item C1), and the queue is empty, so there is no queue section.
"""
import tempfile
import unittest

import rjsupport
from slotbook.book import book
from slotbook.report import render
from slotbook.spans import Span

GOLDENS = [
    {"rooms": ("a",), "closed": (),
     "requests": [("bob", "a", 20, 24), ("ann", "a", 2, 6), ("cy", "a", 10, 13)],
     "report": """\
a: 11 slots booked
  2-6 ann
  10-13 cy
  20-24 bob
"""},
    {"rooms": ("a", "b"), "closed": (),
     "requests": [("ann", "b", 30, 40), ("ann", "a", 8, 9), ("bob", "b", 0, 3), ("cy", "a", 0, 5),
                  ("bob", "a", 15, 20)],
     "report": """\
a: 11 slots booked
  0-5 cy
  8-9 ann
  15-20 bob
b: 13 slots booked
  0-3 bob
  30-40 ann
"""},
    {"rooms": ("lab", "hall", "den"), "closed": (),
     "requests": [("cy", "den", 7, 9), ("ann", "lab", 50, 60), ("bob", "lab", 5, 6), ("ann", "den", 1, 4),
                  ("cy", "lab", 20, 30)],
     "report": """\
lab: 21 slots booked
  5-6 bob
  20-30 cy
  50-60 ann
hall: 0 slots booked
den: 5 slots booked
  1-4 ann
  7-9 cy
"""},
    {"rooms": ("a", "b", "c"), "closed": ("b",),
     "requests": [("dan", "c", 12, 18), ("ann", "c", 3, 5), ("bob", "a", 100, 110), ("bob", "a", 40, 45),
                  ("ann", "a", 60, 70)],
     "report": """\
a: 25 slots booked
  40-45 bob
  60-70 ann
  100-110 bob
b (closed): 0 slots booked
c: 8 slots booked
  3-5 ann
  12-18 dan
"""},
    {"rooms": ("a",), "closed": (),
     "requests": [("ann", "a", 30, 31), ("ann", "a", 10, 12), ("ann", "a", 20, 25), ("ann", "a", 0, 2),
                  ("ann", "a", 40, 48)],
     "report": """\
a: 18 slots booked
  0-2 ann
  10-12 ann
  20-25 ann
  30-31 ann
  40-48 ann
"""},
    {"rooms": ("x", "y"), "closed": (),
     "requests": [("bob", "x", 90, 95), ("ann", "y", 80, 82), ("cy", "x", 70, 75), ("bob", "y", 60, 61),
                  ("ann", "x", 50, 55), ("cy", "y", 40, 44), ("bob", "x", 30, 33)],
     "report": """\
x: 18 slots booked
  30-33 bob
  50-55 ann
  70-75 cy
  90-95 bob
y: 7 slots booked
  40-44 cy
  60-61 bob
  80-82 ann
"""},
]


class H10ReportGoldens(unittest.TestCase):
    def check(self, golden):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = rjsupport.make_store(folder.name, rooms=golden["rooms"], closed=golden["closed"])
        for account, room, start, end in golden["requests"]:
            book(store, account, room, Span(start, end))
        lines = [line.rstrip() for line in render(store).splitlines()]
        lines = [line for line in lines if line and not line.startswith("Total")]
        self.assertEqual(lines, golden["report"].splitlines())

    def test_golden_1_one_room(self):
        self.check(GOLDENS[0])

    def test_golden_2_two_rooms(self):
        self.check(GOLDENS[1])

    def test_golden_3_an_empty_room_between(self):
        self.check(GOLDENS[2])

    def test_golden_4_a_closed_room_between(self):
        self.check(GOLDENS[3])

    def test_golden_5_one_account(self):
        self.check(GOLDENS[4])

    def test_golden_6_latest_first(self):
        self.check(GOLDENS[5])
