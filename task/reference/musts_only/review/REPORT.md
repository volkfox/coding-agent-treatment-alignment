# REPORT

## Part A

- `book` and `free_spans` are in slotbook/book.py. The `free` command is in slotbook/cli.py.
- FORK-1: built as option A, no turnaround.
- FORK-2: built as option A, a partly free request is refused.
- The version is 0.10.0, with a CHANGELOG entry.
- All tests pass.

## Part B

- `enqueue` and `cancel` are in slotbook/book.py. The queue is saved in the store file.
- FORK-3: built as option C. Nobody is booked by a cancel; the report marks a waiting request that fits now.
- The report ends with the queue section.
- All tests pass.

## Review

All tests pass. Nothing to change.

R1: met
R2: met
R3: met
R4: met
R5: met
R6: met
R7: met
