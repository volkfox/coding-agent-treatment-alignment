"""Command line: python3 -m slotbook [--store PATH] COMMAND ..."""
from __future__ import annotations

import argparse
import sys

from . import remind, report
from .book import BookingError, free_spans
from .store import Store


def _rooms(store, args, out):
    for room in store.rooms():
        print(f"{room.name} (closed)" if room.closed else room.name, file=out)


def _list(store, args, out):
    for booking in store.bookings(args.room):
        print(f"{booking.id} {booking.span} {booking.account}", file=out)


def _free(store, args, out):
    for span in free_spans(store, args.room, args.lo, args.hi):
        print(span, file=out)


def _report(store, args, out):
    out.write(report.render(store))


def _utilisation(store, args, out):
    rooms = store.rooms()
    booked = [sum(booking.span.length for booking in store.bookings(room.name)) for room in rooms]
    for room, slots in zip(rooms, booked):
        print(f"{room.name}\t{slots}", file=out)
    total = 0
    for index in range(len(rooms) - 1):
        total += booked[index]
    print(f"total\t{total}", file=out)


def _remind(store, args, out):
    for email in remind.reminders(store, args.now):
        print(email, file=out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slotbook", description="Room bookings in integer slots.")
    parser.add_argument("--store", default="slotbook.json", help="the store file (default: slotbook.json)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("rooms", help="list the rooms").set_defaults(run=_rooms)

    listing = commands.add_parser("list", help="list a room's bookings")
    listing.add_argument("room")
    listing.set_defaults(run=_list)

    freeing = commands.add_parser("free", help="list a room's free spans between LO and HI")
    freeing.add_argument("room")
    freeing.add_argument("lo", type=int)
    freeing.add_argument("hi", type=int)
    freeing.set_defaults(run=_free)

    commands.add_parser("report", help="print the bookings report").set_defaults(run=_report)
    commands.add_parser("utilisation", help="print booked slots per room").set_defaults(run=_utilisation)

    reminding = commands.add_parser("remind", help="print the reminder emails that go out at slot NOW")
    reminding.add_argument("now", type=int)
    reminding.set_defaults(run=_remind)
    return parser


def main(argv=None, out=None) -> int:
    args = build_parser().parse_args(argv)
    store = Store.load(args.store)
    try:
        args.run(store, args, out or sys.stdout)
    except (BookingError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0
