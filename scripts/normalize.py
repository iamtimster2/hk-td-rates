"""
Normalization helpers for the hk-td-rates skill.

These are the bits that are easier to get right in code than in prose:
- Tenor normalization (180D → 6M, "twelve months" → 12M)
- APY parsing ("3.80% p.a." → 3.80)
- HKD/USD min-ticket parsing ("HK$500,000" → 500000)

Run as a library from the skill flow:
    from scripts.normalize import normalize_tenor, parse_apy, parse_min_ticket
"""

from __future__ import annotations

import re
from typing import Optional


# Canonical tenor buckets we publish in the artifact.
CANONICAL_TENORS = ["1M", "3M", "6M", "12M"]

# Map any "raw" tenor expression a provider page might use to one of the
# canonical buckets. Missing rows return None and are dropped.
_TENOR_MAP = {
    # months
    "1m": "1M", "1 month": "1M", "one month": "1M", "30d": "1M",
    "30 days": "1M", "1mth": "1M",
    "3m": "3M", "3 months": "3M", "three months": "3M",
    "90d": "3M", "90 days": "3M", "3mth": "3M",
    "6m": "6M", "6 months": "6M", "six months": "6M",
    "180d": "6M", "180 days": "6M", "26 weeks": "6M", "6mth": "6M",
    "12m": "12M", "12 months": "12M", "twelve months": "12M",
    "1y": "12M", "1 year": "12M", "one year": "12M",
    "365d": "12M", "365 days": "12M", "12mth": "12M",
}


def normalize_tenor(raw: str) -> Optional[str]:
    """
    Map a free-text tenor string to one of CANONICAL_TENORS.
    Returns None for tenors we don't publish (2M, 9M, 24M, etc.).
    """
    if raw is None:
        return None
    key = raw.strip().lower().replace("-", " ").replace("_", " ")
    key = re.sub(r"\s+", " ", key)
    return _TENOR_MAP.get(key)


_APY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def parse_apy(raw: str) -> Optional[float]:
    """
    Pull the first percentage off a string. Returns float (e.g. 3.80).
    Sanity-bounded to [0, 15]; anything outside is treated as a parse error
    and returns None so the caller drops the row.
    """
    if not raw:
        return None
    m = _APY_RE.search(raw)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    if val < 0 or val > 15:
        return None
    return val


_AMOUNT_RE = re.compile(r"([\d,]+(?:\.\d+)?)")


def parse_min_ticket(raw: str) -> Optional[int]:
    """
    Pull a minimum-ticket size off a string like "HK$500,000+" or
    "USD 10,000 - 999,999". Returns the lower bound as an integer in the
    quote currency, or None if not parseable.
    """
    if not raw:
        return None
    m = _AMOUNT_RE.search(raw.replace(" ", ""))
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", "")))
    except ValueError:
        return None


def is_promo_flag(raw_row_text: str) -> bool:
    """
    Heuristic: does this row's surrounding copy mark it as a promo /
    new-money / online-exclusive rate?
    """
    if not raw_row_text:
        return False
    needles = (
        "preferential", "promo", "promotion", "promotional",
        "new fund", "new money", "new-to-bank",
        "online exclusive", "online-exclusive", "online only",
        "limited time", "limited-time", "offer", "campaign",
    )
    haystack = raw_row_text.lower()
    return any(n in haystack for n in needles)


if __name__ == "__main__":
    # Smoke tests — run `python scripts/normalize.py` to sanity-check.
    assert normalize_tenor("3 Months") == "3M"
    assert normalize_tenor("180D") == "6M"
    assert normalize_tenor("9M") is None
    assert parse_apy("3.80% p.a.") == 3.80
    assert parse_apy("up to 5.25%") == 5.25
    assert parse_apy("99%") is None  # out of bounds
    assert parse_min_ticket("HK$500,000") == 500000
    assert parse_min_ticket("USD 10,000+") == 10000
    assert is_promo_flag("Online-exclusive preferential rate for new funds") is True
    assert is_promo_flag("Standard board rate") is False
    print("normalize.py: all smoke tests passed.")
