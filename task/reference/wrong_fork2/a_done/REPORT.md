# REPORT

## Part A

- `book` and `free_spans` are in slotbook/book.py; the `free` command is in slotbook/cli.py.
- FORK-1, option B as decided: one free slot stays between bookings of different accounts; the same account may book back to back.
- FORK-2: built as option B. The earliest free piece of a partly free request is booked and the rest of the request is dropped. A request with no free slot raises BookingError and changes nothing.
- The version is 0.10.0, with a CHANGELOG entry.
- docs/rules.md asks for one store read per call: `book` and `free_spans` each read the room's bookings once.
- docs/rules.md asks for a room's bookings in slot order in the report. `book` can add them in any order, so the report now sorts them.
- Bad input (start above end, a negative slot, an unknown room) raises BookingError before the store is read or saved.
- My own tests are in tests/test_book_rules.py.

## Assumptions and open points

- An empty span (start == end) is not covered by TASK.md. `book` refuses it with BookingError; `free_spans` returns nothing for an empty window.
- `__version__` said 0.9.0 while the top CHANGELOG heading was 0.9.1. I took the CHANGELOG as right and set both to 0.10.0.
- docs/rules.md says room names ignore case, but the store compares them exactly, so it is case-sensitive: `Annex` is not found when the file says `annex`. I did not change the store, and `book` treats a name the store does not know as an unknown room. Tell me if you want names folded to lower case.
