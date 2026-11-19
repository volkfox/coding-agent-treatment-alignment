"""Reminder emails for bookings that start soon."""
from __future__ import annotations

from .plans import plan_for
from .store import Booking, Store


def due(store: Store, now: int) -> list[Booking]:
    """The bookings whose reminder goes out at slot `now`, earliest start first."""
    found = []
    for room in store.rooms():
        for booking in store.bookings(room.name):
            plan = plan_for(store, booking.account)
            wait = booking.span.start - now
            if wait == plan.lead or (plan.repeat and wait == 1):
                found.append(booking)
    return sorted(found, key=lambda booking: (booking.span.start, booking.room, booking.id))


def compose(store: Store, booking: Booking, now: int) -> str:
    """The text of one reminder email."""
    wait = booking.span.start - now
    slots = "1 slot" if wait == 1 else f"{wait} slots"
    return (
        f"To: {booking.account}\n"
        f"Subject: {booking.room} {booking.span}\n"
        f"Your booking of {booking.room} for slots {booking.span} starts in {slots}.\n"
        f"Plan: {plan_for(store, booking.account).name}\n"
    )


def reminders(store: Store, now: int) -> list[str]:
    """Every reminder email that goes out at slot `now`."""
    return [compose(store, booking, now) for booking in due(store, now)]
