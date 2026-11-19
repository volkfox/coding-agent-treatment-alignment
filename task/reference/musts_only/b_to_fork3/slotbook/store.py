"""The store: rooms, bookings, account plans and the queue, kept in one JSON file."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .spans import Span


@dataclass(frozen=True)
class Room:
    name: str
    closed: bool = False


@dataclass(frozen=True)
class Booking:
    id: int
    account: str
    room: str
    span: Span


@dataclass(frozen=True)
class Request:
    """A queued request: what a booking would hold, with an id of its own."""

    id: int
    account: str
    room: str
    span: Span


class Store:
    """Rooms and bookings as they stand in the store file.

    Bookings are kept as the file's records, in the order they were added.
    """

    def __init__(self, path: Path, data: dict):
        self.path = Path(path)
        self._rooms = {r["name"]: Room(r["name"], bool(r.get("closed", False))) for r in data.get("rooms", [])}
        self._records = list(data.get("bookings", []))
        self._plans = dict(data.get("plans", {}))
        highest = max((record["id"] for record in self._records), default=0)
        self._next_id = int(data.get("next_id", highest + 1))
        # The queue came with 0.10; a file written before it has none of these keys.
        self._queue = list(data.get("queue", []))
        highest = max((record["id"] for record in self._queue), default=0)
        self._next_request = int(data.get("next_request", highest + 1))
        self.promoted = int(data.get("promoted", 0))

    @classmethod
    def load(cls, path) -> Store:
        """Read a store file. A file that does not exist yet is an empty store."""
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return cls(path, data)

    def save(self) -> None:
        data = {
            "rooms": [{"name": room.name, "closed": room.closed} for room in self._rooms.values()],
            "bookings": self._records,
            "plans": self._plans,
            "next_id": self._next_id,
            "queue": self._queue,
            "next_request": self._next_request,
            "promoted": self.promoted,
        }
        self.path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")

    def rooms(self) -> list[Room]:
        return list(self._rooms.values())

    def room(self, name: str) -> Room:
        """The room of that name; KeyError if there is none."""
        return self._rooms[name]

    def plan(self, account: str) -> str | None:
        """The name of the account's plan tier, or None if the file gives none."""
        return self._plans.get(account)

    def bookings(self, room: str) -> list[Booking]:
        """The room's bookings, built from the file's records, in the order they were added."""
        self.room(room)
        return [
            Booking(record["id"], record["account"], record["room"], Span(record["start"], record["end"]))
            for record in self._records
            if record["room"] == room
        ]

    def booking(self, booking_id: int) -> Booking:
        """The booking with that id; KeyError if there is none."""
        for record in self._records:
            if record["id"] == booking_id:
                return Booking(record["id"], record["account"], record["room"], Span(record["start"], record["end"]))
        raise KeyError(booking_id)

    def add(self, account: str, room: str, span: Span) -> Booking:
        """Store a booking as given and return it. Nothing is checked beyond the room's name."""
        self.room(room)
        booking = Booking(self._next_id, account, room, span)
        self._next_id += 1
        self._records.append(
            {"id": booking.id, "account": account, "room": room, "start": span.start, "end": span.end}
        )
        return booking

    def remove(self, booking_id: int) -> None:
        """Drop the booking with that id; KeyError if there is none."""
        for index, record in enumerate(self._records):
            if record["id"] == booking_id:
                del self._records[index]
                return
        raise KeyError(booking_id)

    def waiting(self) -> list[Request]:
        """The queued requests, earliest first."""
        return [
            Request(record["id"], record["account"], record["room"], Span(record["start"], record["end"]))
            for record in self._queue
        ]

    def enqueue(self, account: str, room: str, span: Span) -> Request:
        """Put a request at the end of the queue and return it."""
        self.room(room)
        request = Request(self._next_request, account, room, span)
        self._next_request += 1
        self._queue.append(
            {"id": request.id, "account": account, "room": room, "start": span.start, "end": span.end}
        )
        return request

    def promote(self, request_id: int) -> None:
        """Take a request that has been booked out of the queue and count it; KeyError if there is none."""
        for index, record in enumerate(self._queue):
            if record["id"] == request_id:
                del self._queue[index]
                self.promoted += 1
                return
        raise KeyError(request_id)
