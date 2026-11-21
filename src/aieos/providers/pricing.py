"""Token usage to USD."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from aieos.providers.config import Price

_MILLION = Decimal(1_000_000)
_MICRO = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int  # includes cached and cache-write tokens
    cached_input_tokens: int
    output_tokens: int
    cache_write_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def cost_usd(usage: TokenUsage, price: Price) -> Decimal:
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    cache_write = min(usage.cache_write_input_tokens, usage.input_tokens - cached)
    uncached = usage.input_tokens - cached - cache_write
    write_rate = price.input_per_mtok if price.cache_write_input_per_mtok is None else price.cache_write_input_per_mtok
    raw = (
        Decimal(uncached) * price.input_per_mtok
        + Decimal(cached) * price.cached_input_per_mtok
        + Decimal(cache_write) * write_rate
        + Decimal(usage.output_tokens) * price.output_per_mtok
    ) / _MILLION
    return raw.quantize(_MICRO, rounding=ROUND_HALF_UP)  # matches numeric(12, 6)
