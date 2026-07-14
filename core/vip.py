"""
Pure, dependency-free helpers for the dashboard VIP-tier volume card.

OKX's API exposes your current VIP *level* (via /api/v5/account/config) but NOT
your 30-day trading volume, so this module holds the tier→volume thresholds
(converted from OKX's UAE AED fee schedule at the fixed 3.6725 AED/USD peg) and
the small amount of math the /api/account-info endpoint needs to render a
"volume traded this month vs the tier floor" progress bar. All USD.

Read-only: nothing here touches signals, sizing, or orders.
"""
import re
from typing import Dict, Optional

# The UAE dirham is hard-pegged to the US dollar at 3.6725 AED per USD (fixed
# since 1997). OKX's UAE fee page quotes VIP thresholds in AED; we display USD.
AED_PER_USD = 3.6725

# 30-day trading-volume FLOOR to reach / maintain each VIP tier, in AED, taken
# from OKX's "VIP users" fee schedule (the lower bound of each tier's volume
# band). VIP tier is granted on the HIGHER of assets-or-volume, so hitting this
# volume floor alone is sufficient to hold the tier.
VIP_VOLUME_FLOOR_AED: Dict[int, int] = {
    1: 18_000_001,
    2: 36_000_001,
    3: 180_000_001,
    4: 720_000_001,
    5: 2_160_000_001,
    6: 3_600_000_001,
    7: 5_400_000_001,
    8: 7_200_000_001,
    9: 72_000_000_001,
}

# Same floors in USD (computed once at import — no hand-rounding to drift).
VIP_VOLUME_FLOOR_USD: Dict[int, float] = {
    tier: round(aed / AED_PER_USD, 2) for tier, aed in VIP_VOLUME_FLOOR_AED.items()
}

# ── Manual current-month seed ────────────────────────────────────────────────
# OKX gives no per-account volume via API, so the CURRENT (partial) month is
# seeded by hand from the OKX VIP page; the bot's own fills for the month are
# added on top (see database.get_month_volume_usd). Update these two lines at the
# start of each month from the OKX screenshot, or set the USD to 0.0 to track
# ONLY this bot's measured volume. The seed only applies when the tag matches the
# current calendar month, so a stale seed auto-expires next month.
VIP_MONTH_BASELINE_MONTH = "2026-07"          # YYYY-MM this seed belongs to
VIP_MONTH_BASELINE_USD = 1_093_838.0          # 4,017,120 AED / 3.6725 (screenshot)


def parse_vip_tier(level_str) -> int:
    """OKX 'level' string → integer tier. Accepts 'VIP4', 'Lv4', '4', etc.
    Returns 0 for Regular / unknown / empty."""
    m = re.search(r"(\d+)", str(level_str or ""))
    return int(m.group(1)) if m else 0


def maintain_floor_usd(tier: int) -> Optional[float]:
    """Volume floor (USD) needed to hold `tier`. For Regular (0) there's nothing
    to maintain, so we point at VIP 1's floor as the first milestone. None only
    if the tier is off the top of the table."""
    if tier < 1:
        return VIP_VOLUME_FLOOR_USD.get(1)
    return VIP_VOLUME_FLOOR_USD.get(tier)


def next_tier_floor_usd(tier: int):
    """(next_tier, floor_usd) for the tier above `tier`, or (None, None) if
    already at the top."""
    nxt = max(tier, 0) + 1
    floor = VIP_VOLUME_FLOOR_USD.get(nxt)
    return (nxt, floor) if floor is not None else (None, None)


def month_baseline_usd(month_str: str) -> float:
    """The hand-entered seed for `month_str` (YYYY-MM), or 0.0 if the seed is for
    a different month (so it doesn't leak into later months)."""
    return VIP_MONTH_BASELINE_USD if month_str == VIP_MONTH_BASELINE_MONTH else 0.0
