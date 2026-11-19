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


def queue_section(store: Store) -> list[str]:
    """The queue lines that end the report; none while nothing waits and nothing was promoted."""
    waiting = store.waiting()
    if not waiting and not store.promoted:
        return []
    lines = ["Queue", f"Promoted: {store.promoted}", "Waiting:"]
    lines.extend(f"{request.id} {request.account} {request.room} {request.span}" for request in waiting)
    return lines


def render(store: Store) -> str:
    """The report as text: every room with its bookings, the total, then the queue if there is one."""
    per_room, total = utilisation(store)
    lines = []
    for room in store.rooms():
        label = f"{room.name} (closed)" if room.closed else room.name
        lines.append(f"{label}: {per_room[room.name]} slots booked")
        # docs/rules.md: a room's bookings are listed in slot order, whatever order they were made in
        for booking in sorted(store.bookings(room.name), key=lambda b: (b.span.start, b.span.end, b.id)):
            lines.append(f"  {booking.span} {booking.account}")
    lines.append(f"Total: {total} slots booked")
    lines.extend(queue_section(store))
    return "\n".join(lines) + "\n"
