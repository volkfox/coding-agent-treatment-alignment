"""Booking rules: which slots a request may take.

`book` and `free_spans` are part A of TASK.md, `enqueue` and `cancel` part B.
"""
from __future__ import annotations

from .spans import Span
from .store import Booking, Room, Store


class BookingError(Exception):
    """A booking request was refused."""


def booked_slots(store: Store, room: str) -> int:
    """How many slots of the room are booked."""
    return sum(booking.span.length for booking in store.bookings(room))


def _check_span(lo: int, hi: int, empty_ok: bool = False) -> None:
    """Refuse bad input before the store is read or changed."""
    for slot in (lo, hi):
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise BookingError(f"a slot is a whole number, not {slot!r}")
    if lo < 0 or hi < 0:
        raise BookingError(f"a slot cannot be negative: {lo}-{hi}")
    if lo > hi:
        raise BookingError(f"a span cannot start after it ends: {lo}-{hi}")
    if lo == hi and not empty_ok:
        raise BookingError(f"an empty span books nothing: {lo}-{hi}")      # my assumption, see REPORT.md


def _room(store: Store, name: str) -> Room:
    try:
        return store.room(name)
    except KeyError:
        raise BookingError(f"unknown room: {name}") from None


def _read(store: Store, room: str) -> list[Booking]:
    """The room's bookings. This is the one store read of an operation (docs/rules.md)."""
    try:
        return store.bookings(room)
    except KeyError:
        raise BookingError(f"unknown room: {room}") from None


def _gaps(blocks: list[tuple[int, int]], lo: int, hi: int) -> list[Span]:
    """What is left of lo..hi once the blocks are taken out: sorted, merged and clipped."""
    gaps, at = [], lo
    for start, end in sorted(blocks):
        if start >= hi:
            break
        if start > at:
            gaps.append(Span(at, start))
        at = max(at, end)
    if at < hi:
        gaps.append(Span(at, hi))
    return gaps


def _open_pieces(bookings: list[Booking], account: str, span: Span) -> list[Span]:
    """The pieces of span that the account may book, in slot order.

    FORK-1, option B: a slot next to another account's booking stays free, so
    such a booking blocks one slot more on either side. The account's own
    bookings block only their own slots, so it may book back to back.
    """
    blocks = []
    for booking in bookings:
        margin = 0 if booking.account == account else 1
        blocks.append((booking.span.start - margin, booking.span.end + margin))
    return _gaps(blocks, span.start, span.end)


def free_spans(store: Store, room: str, lo: int, hi: int) -> list[Span]:
    """The unbooked slots of the room between lo and hi, as sorted, merged spans. No turnaround."""
    _check_span(lo, hi, empty_ok=True)
    bookings = _read(store, room)
    return _gaps([(booking.span.start, booking.span.end) for booking in bookings], lo, hi)


def book(store: Store, account: str, room: str, span: Span) -> list[Booking]:
    """Book span in room for account, save the store and return the bookings made."""
    _check_span(span.start, span.end)
    if _room(store, room).closed:
        raise BookingError(f"{room} is closed")
    bookings = _read(store, room)
    pieces = _open_pieces(bookings, account, span)
    if not pieces:
        raise BookingError(f"no free slot for {account} in {room} {span}")
    # FORK-2, option B: only the earliest free piece is booked.
    made = [store.add(account, room, pieces[0])]
    store.save()
    return made


def enqueue(store: Store, account: str, room: str, span: Span) -> int:
    """Put a request at the end of the queue, save the store and return the request's id."""
    _check_span(span.start, span.end)
    if _room(store, room).closed:
        raise BookingError(f"{room} is closed")
    request = store.enqueue(account, room, span)
    store.save()
    return request.id


def cancel(store: Store, booking_id: int) -> list[Booking]:
    """Remove the booking, save the store and return the bookings made for queued requests."""
    try:
        booking = store.booking(booking_id)
    except KeyError:
        raise BookingError(f"unknown booking: {booking_id}") from None
    store.remove(booking_id)
    made = []
    if store.room(booking.room).closed:      # a closed room takes no new bookings
        store.save()
        return made
    bookings = _read(store, booking.room)
    for request in store.waiting():
        # FORK-3, option A: the earliest queued request that fits whole is booked.
        # A request that does not fit keeps its place. At most one per cancel.
        if request.room == booking.room and _open_pieces(bookings, request.account, request.span) == [request.span]:
            made.append(store.add(request.account, request.room, request.span))
            store.promote(request.id)
            break
    store.save()
    return made
