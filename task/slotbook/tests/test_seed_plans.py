import unittest
from pathlib import Path

from slotbook.plans import PLANS, plan_for
from slotbook.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "office.json"


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.store = Store.load(FIXTURE)

    def test_tiers(self):
        self.assertEqual(sorted(PLANS), ["basic", "campus", "team"])
        self.assertLess(PLANS["basic"].lead, PLANS["team"].lead)
        self.assertLess(PLANS["team"].lead, PLANS["campus"].lead)

    def test_plan_of_an_account(self):
        self.assertEqual(plan_for(self.store, "ann"), PLANS["team"])
        self.assertEqual(plan_for(self.store, "cy"), PLANS["campus"])

    def test_account_without_a_plan_is_basic(self):
        self.assertEqual(plan_for(self.store, "bob"), PLANS["basic"])


if __name__ == "__main__":
    unittest.main()
