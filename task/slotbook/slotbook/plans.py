"""Plan tiers. An account's tier sets when its reminder emails go out."""
from __future__ import annotations

from dataclasses import dataclass

from .store import Store


@dataclass(frozen=True)
class Plan:
    name: str
    lead: int       # the reminder goes out this many slots before a booking starts
    repeat: bool    # a second reminder goes out one slot before the start


PLANS = {
    "basic": Plan("basic", lead=2, repeat=False),
    "team": Plan("team", lead=4, repeat=False),
    "campus": Plan("campus", lead=8, repeat=True),
}
DEFAULT_PLAN = "basic"


def plan_for(store: Store, account: str) -> Plan:
    """The account's plan tier. An account the store lists no plan for is on the basic tier."""
    return PLANS[store.plan(account) or DEFAULT_PLAN]
