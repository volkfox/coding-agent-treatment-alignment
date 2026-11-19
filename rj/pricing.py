"""Prices: the catalogue ratio rule, the per-session estimate and the per-turn cost.

Plan section 5 (prices.yaml) and section 14. USD per million tokens throughout.
The dollar unit of the catalogue is inferred (model_ratio x 2) until check 11 measures a session.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Assumed token counts for one session (plan section 14): 6M input of which 85% are cache reads, 60k output.
ASSUMED_UNCACHED_M = 0.9
ASSUMED_CACHED_M = 5.1
ASSUMED_OUTPUT_M = 0.06


@dataclass(frozen=True)
class Price:
    input: float
    cache_read: float
    cache_write: float
    output: float


def price_from_ratios(row: dict) -> Price:
    """One row of GET api.tokenrouter.com/api/pricing `data[]` -> USD per million tokens."""
    inp = float(row["model_ratio"]) * 2
    cache = row.get("cache_ratio")
    write = row.get("create_cache_ratio")
    return Price(
        input=inp,
        cache_read=inp * float(cache) if cache is not None else inp,
        cache_write=inp * float(write) if write is not None else inp,
        output=inp * float(row["completion_ratio"]),
    )


def est_session_usd(p: Price) -> float:
    """Rounded half up to the cent, in decimal arithmetic, so 4.425 reads 4.43 as in the plan."""
    d = lambda x: Decimal(repr(round(float(x), 6)))  # noqa: E731
    total = d(ASSUMED_UNCACHED_M) * d(p.input) + d(ASSUMED_CACHED_M) * d(p.cache_read) + d(ASSUMED_OUTPUT_M) * d(p.output)
    return float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def turn_cost_usd(usage: dict | None, p: Price, input_includes_cache: bool = False) -> float | None:
    """Cost of one turn from its usage block. None when the turn reported no usage.

    Whether input_tokens already includes the cache tokens is unknown until check 11;
    the default assumes not, which is the dearer reading.
    """
    if not usage:
        return None
    inp = int(usage.get("input_tokens") or 0)
    read = int(usage.get("cache_read_tokens") or 0)
    write = int(usage.get("cache_write_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    if input_includes_cache:
        inp = max(0, inp - read - write)
    return (inp * p.input + read * p.cache_read + write * p.cache_write + out * p.output) / 1_000_000


def price_from_yaml(entry: dict) -> Price:
    inp = float(entry["input"])
    return Price(
        input=inp,
        cache_read=float(entry.get("cache_read", inp)),
        cache_write=float(entry.get("cache_write", inp)),
        output=float(entry["output"]),
    )
