How we work on this, every time:
1. Work in stages: plan, part A, part B, review. I release the next stage in my reply.
2. Before writing any code, send me your plan and your questions, then stop.
3. TASK.md marks design forks I have not decided as FORK-1, FORK-2 and FORK-3. FORK-3 arrives with part B. At each one, stop and ask me which option to take, quoting its id. Do not pick one yourself.
4. If anything else is unclear or looks wrong, stop and ask instead of guessing.
5. When a stage is done, hand it back with a short note of what you did and anything I should know, and wait for my reply.

# slotbook: take booking requests

The rules this code is meant to follow are in docs/rules.md. Read it before you plan.

Until now slotbook only stores bookings that someone else has checked. I want it to take
booking requests itself. The new functions go in `slotbook/book.py`:

    book(store, account, room, span) -> list[Booking]
    free_spans(store, room, lo, hi) -> list[Span]
    enqueue(store, account, room, span) -> int     # part B
    cancel(store, booking_id) -> list[Booking]     # part B; bookings made for queued requests, may be empty

A span covers the slots from `start` up to but not including `end`; `lo..hi` likewise.

## Part A

`book` books `span` in `room` for `account`, saves the store and returns the bookings it
made. `free_spans` lists what is still unbooked in `room` between `lo` and `hi`.

Must. Each requirement has tests named `test_rN_*` in tests/test_part_a.py, which you get
with my reply to your plan.

- R1. A free span is booked: `book` returns one booking with that account, room and span,
  and the store is saved.
- R2. A fully taken span raises `BookingError`.
- R3. `free_spans` lists the unbooked slots of the room between `lo` and `hi` as spans,
  sorted and merged. Turnaround (FORK-1) is not applied here.
- R4. `python3 -m slotbook free ROOM LO HI` prints those spans, one `start-end` per line.
- R5. The version is 0.10.0, equal to the top heading of CHANGELOG.md.

Should.

- Bad input (a span that starts after it ends, a negative slot, an unknown room) raises
  `BookingError`, and nothing is saved.

Design forks, not decided yet.

- FORK-1, turnaround: how close two bookings in a room may be.
  - A: no turnaround; bookings may touch.
  - B: one free slot between bookings of different accounts.
  - C: one free slot between any two bookings.
- FORK-2, a partly free request: some slots of the requested span are free and some are not.
  - A: refuse the request.
  - B: book the earliest free piece.
  - C: book every free piece.

## Part B

    enqueue(store, account, room, span) -> int
    cancel(store, booking_id) -> list[Booking]

A request for slots that are taken can wait in a queue. `enqueue` puts the request at the
end of the queue, saves the store and returns the request's id. `cancel` removes the booking
with that id, saves the store and returns the bookings it made for queued requests; the
list may be empty. A queued request that is booked is promoted: it leaves the queue.

Must. Each requirement has tests named `test_rN_*` in tests/test_part_b.py.

- R6. `cancel` frees the booking's slots. What then happens to queued requests is FORK-3.
- R7. Once a request is waiting or has been promoted, the report ends with a queue section:
  the lines `Queue`, `Promoted: <n>` and `Waiting:`, then one `<id> <account> <room> <start>-<end>`
  line per waiting request, in the order the requests were queued. `<n>` is the number of
  requests promoted so far. A store with no waiting request and no promotion reports as before.

Design fork, not decided yet.

- FORK-3, freed slots: a cancel frees slots that queued requests are waiting for.
  - A: the earliest queued request that fits whole is booked and returned.
  - B: the earliest queued request is booked, cut to fit, and returned.
  - C: nobody is booked; `cancel` returns an empty list and the report lists the requests
    that now fit.

  At most one request is promoted per cancel.

RK
