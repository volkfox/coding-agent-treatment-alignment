"""Booking rules.

So far the store takes whatever it is given: callers are trusted to add only
bookings that someone has already checked. The functions that check a request
and book it belong in this module; TASK.md describes them.
"""
from __future__ import annotations

from .store import Store


class BookingError(Exception):
    """A booking request was refused."""


def booked_slots(store: Store, room: str) -> int:
    """How many slots of the room are booked."""
    return sum(booking.span.length for booking in store.bookings(room))
