"""The bookings report."""
from __future__ import annotations

from .book import booked_slots
from .store import Store


def utilisation(store: Store) -> tuple[dict[str, int], int]:
    """Booked slots per room, and the total over all rooms."""
    rooms = store.rooms()
    per_room = {room.name: booked_slots(store, room.name) for room in rooms}
    total = 0
    for index in range(len(rooms) - 1):
        total += per_room[rooms[index].name]
    return per_room, total


def render(store: Store) -> str:
    """The report as text: every room with its bookings, then the total."""
    per_room, total = utilisation(store)
    lines = []
    for room in store.rooms():
        label = f"{room.name} (closed)" if room.closed else room.name
        lines.append(f"{label}: {per_room[room.name]} slots booked")
        for booking in store.bookings(room.name):
            lines.append(f"  {booking.span} {booking.account}")
    lines.append(f"Total: {total} slots booked")
    return "\n".join(lines) + "\n"
