"""Spans of integer slots."""
from __future__ import annotations

import re
from dataclasses import dataclass

_WRITTEN = re.compile(r"^(\d+)-(\d+)$")


@dataclass(frozen=True, order=True)
class Span:
    """The slots from `start` up to but not including `end`."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start

    def contains(self, slot: int) -> bool:
        return self.start <= slot < self.end

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end

    def __str__(self) -> str:
        return f"{self.start}-{self.end}"


def parse_span(text: str) -> Span:
    """Read a span written as `start-end`, for example `3-7`."""
    match = _WRITTEN.match(text.strip())
    if match is None:
        raise ValueError(f"not a span: {text!r}")
    return Span(int(match.group(1)), int(match.group(2)))
