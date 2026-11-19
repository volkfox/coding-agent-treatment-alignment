# slotbook rules

These are the rules the code is meant to follow. Where the code and this file differ, say so.

## Slots and spans

Time is a row of integer slots, numbered from 0. A span is written `start-end` and covers the
slots from `start` up to but not including `end`: `3-7` is the slots 3, 4, 5 and 6.

## Rooms

A room has a name and is open or closed. Room names ignore case: `Annex` and `annex` are the
same room.

A closed room keeps the bookings it has and refuses new ones.

## Bookings

A booking has an id, an account, a room and a span. Ids count up from 1 and are never used twice.

## The store

One JSON file holds everything:

    {"rooms": [{"name": "atrium", "closed": false}],
     "bookings": [{"id": 1, "account": "ann", "room": "atrium", "start": 2, "end": 5}],
     "plans": {"ann": "team"},
     "next_id": 2}

Rooms and plans are edited in the file by hand. A file written by an earlier version must
still load.

`store.bookings(room)` builds its list from the file's records each time it is called, so it
is the costly read. One store read per call: an operation reads a room's bookings once and
works from that list. It never reads inside a loop over slots or over bookings.

`store.add` and `store.remove` change the store in memory; `store.save()` writes the file.

## Reports

`python3 -m slotbook report` prints every room with its bookings, then the total of booked
slots. A room's bookings are listed in slot order.

## Plans and reminders

Each account is on a plan tier: `basic`, `team` or `campus`. An account with no plan in the
store is on `basic`. The tier sets how many slots before a booking its reminder email goes
out (2, 4 or 8); `campus` also gets a second reminder one slot before the start.

## Command line

    python3 -m slotbook [--store PATH] COMMAND

`--store` names the store file; the default is `slotbook.json` in the current directory.
Commands: `rooms`, `list ROOM`, `report`, `utilisation`, `remind NOW`. A command that fails
prints `error: ...` on standard error and exits with status 2.
