"""Booking rules."""
from __future__ import annotations

from .spans import Span
from .store import Booking, Store


class BookingError(Exception):
    """A booking request was refused."""


def booked_slots(store: Store, room: str) -> int:
    """How many slots of the room are booked."""
    return sum(booking.span.length for booking in store.bookings(room))


def free_spans(store: Store, room: str, lo: int, hi: int) -> list[Span]:
    """The unbooked slots of the room between lo and hi."""
    spans = []
    at = lo
    for booking in store.bookings(room):
        if booking.span.start > at:
            spans.append(Span(at, booking.span.start))
        at = booking.span.end
    if at < hi:
        spans.append(Span(at, hi))
    return spans


def book(store: Store, account: str, room: str, span: Span) -> list[Booking]:
    """Book span in room for account."""
    booking = store.add(account, room, span)
    store.save()
    for slot in range(span.start, span.end):
        for other in store.bookings(room):
            if other.id != booking.id and other.span.contains(slot):
                store.remove(booking.id)
                raise BookingError(f"{room} {span} is taken")
    return [booking]
