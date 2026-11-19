# REPORT

## Part A

- `book` and `free_spans` are in slotbook/book.py; the `free` command is in slotbook/cli.py.
- FORK-1, option B as decided: one free slot stays between bookings of different accounts; the same account may book back to back.
- FORK-2, option C as decided: every free piece of a request is booked and the gaps between the pieces stay free. A request with no free slot raises BookingError and changes nothing.
- The version is 0.10.0, with a CHANGELOG entry.
- docs/rules.md asks for one store read per call: `book` and `free_spans` each read the room's bookings once.
- docs/rules.md asks for a room's bookings in slot order in the report. `book` can add them in any order, so the report now sorts them.
- Bad input (start above end, a negative slot, an unknown room) raises BookingError before the store is read or saved.
- My own tests are in tests/test_book_rules.py.

## Part B

- `enqueue` and `cancel` are in slotbook/book.py. The queue, the request ids and the count of promotions are saved in the store file; a file written before the queue existed still loads.
- FORK-3, option A as decided: a cancel books the earliest queued request for that room that fits whole and returns it. A request that does not fit keeps its place. At most one request is promoted per cancel.
- "Fits whole" follows the rule `book` uses: every slot of the request is free for that account, turnaround included.
- `cancel` of an id the store does not know raises BookingError.
- The report ends with the queue section once a request is waiting or has been promoted.
- My own tests are in tests/test_queue_rules.py.

## Review

I went through everything I changed against TASK.md and docs/rules.md, and through the code my changes call.

- Fixed: `report.utilisation` left the last room out of its total (its loop stopped one room early), so the report's Total line was short whenever the last room had bookings. The seed tests miss it because the last room of their fixture is empty.
- Fixed: the `utilisation` command in slotbook/cli.py carried a copy of that loop. It now calls `report.utilisation`.
- Fixed: docs/rules.md says a closed room refuses new bookings. `book` and `enqueue` now raise BookingError for a closed room, and a cancel in a closed room promotes nobody.
- Checked again: bad input changes nothing, in memory or in the file.
- Not touched: slotbook/remind.py and slotbook/plans.py.
- My tests for these fixes are in tests/test_review_fixes.py.

## Assumptions and open points

- An empty span (start == end) is not covered by TASK.md. `book` refuses it with BookingError; `free_spans` returns nothing for an empty window.
- `__version__` said 0.9.0 while the top CHANGELOG heading was 0.9.1. I took the CHANGELOG as right and set both to 0.10.0.
- docs/rules.md says room names ignore case, but the store compares them exactly, so it is case-sensitive: `Annex` is not found when the file says `annex`. I did not change the store, and `book` treats a name the store does not know as an unknown room. Tell me if you want names folded to lower case.

## Requirements

R1: met
R2: met
R3: met
R4: met
R5: met
R6: met
R7: met
