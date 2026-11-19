"""Helpers shared by the hidden checks, the care tests and the fork probes. Imported after the workspace is on sys.path."""
import itertools
import json
import pathlib

_count = itertools.count(1)


def make_store(folder, rooms=("a",), bookings=(), closed=()):
    """Write a store file in the seed's layout and load it through the workspace's Store.

    rooms: names in file order; closed: the names among them that are closed;
    bookings: (account, room, start, end) in stored order, given the ids 1, 2, ...
    """
    from slotbook.store import Store

    records = [
        {"id": number, "account": account, "room": room, "start": start, "end": end}
        for number, (account, room, start, end) in enumerate(bookings, 1)
    ]
    data = {
        "rooms": [{"name": name, "closed": name in closed} for name in rooms],
        "bookings": records,
        "plans": {},
        "next_id": len(records) + 1,
    }
    path = pathlib.Path(folder) / f"store{next(_count)}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return Store.load(path)


def reload(store):
    """The store as its file now has it."""
    from slotbook.store import Store

    return Store.load(store.path)


def rows(bookings):
    """Bookings as plain tuples in slot order, so a check does not depend on how Booking compares."""
    return sorted((b.span.start, b.span.end, b.account, b.room) for b in bookings)


def spans(found):
    """Spans as (start, end) pairs, in the order given."""
    return [(span.start, span.end) for span in found]
