"""
Pair scanner — rank candidate stat-arb pairs by tradeable edge.

For every unordered pair of candidate instruments it fetches recent OKX
candles, fits the OLS hedge ratio beta (price-space regression of Leg B on
Leg A), builds the spread = priceB - beta*priceA, and scores it on the SAME
metrics the live engine uses:

  - Vol/Cost ratio: spread std-dev / round-trip breakeven cost. >1 means a
    1-sigma spread move clears costs; higher = more room to profit.
  - Hurst exponent (R/S): <0.5 mean-reverting, ~0.5 random walk, >0.5 trending.
    Same R/S implementation as core/signals.py.
  - Half-life (OU/AR1): periods to revert halfway to the mean. Short = trades
    close quickly; very long = capital sits in slow drifts.

The hurst / half-life formulas mirror core/signals.py exactly so the ranking
matches what the engine will see live.

USAGE (run on the machine with OKX network access):

    python -m backtest.pair_scanner
    python -m backtest.pair_scanner --bar 15m --bars 400 --rt-bps 8 --top 20
    python -m backtest.pair_scanner --symbols BTC-USDT-SWAP ETH-USDT-SWAP SOL-USDT-SWAP

Scan with perpetual SWAPs (deep liquidity, long history); the cointegration
relationship carries over to the dated-futures equivalents you trade.
"""
import argparse
import asyncio
import itertools
import logging
from typing import Dict, List, Optional, Tuple

import aiohttp
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"

# Liquid USDT-margined perps — a sensible default universe. Override with --symbols.
DEFAULT_SYMBOLS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "BNB-USDT-SWAP",
    "LTC-USDT-SWAP", "ADA-USDT-SWAP", "DOT-USDT-SWAP", "ATOM-USDT-SWAP",
]


async def fetch_closes(session: aiohttp.ClientSession, inst_id: str,
                       bar: str, limit: int) -> Optional[np.ndarray]:
    """Fetch `limit` close prices for inst_id, oldest-first. None on failure."""
    url = f"{OKX_BASE}/api/v5/market/candles"
    params = {"instId": inst_id, "bar": bar, "limit": str(min(limit, 300))}
    try:
        async with session.get(url, params=params, timeout=20) as r:
            data = await r.json()
        if data.get("code") != "0" or not data.get("data"):
            logger.warning("  %s: no data (%s)", inst_id, data.get("msg", "?"))
            return None
        # OKX returns newest-first: [ts, o, h, l, c, vol, ...]
        closes = [float(row[4]) for row in data["data"]]
        closes.reverse()  # oldest-first
        return np.asarray(closes, dtype=float)
    except Exception as e:
        logger.warning("  %s: fetch error %s", inst_id, e)
        return None


def hurst_rs(series: np.ndarray) -> float:
    """R/S Hurst exponent — mirrors core/signals.py:_calculate_hurst."""
    n = len(series)
    if n < 20:
        return 0.5
    max_k = min(n // 2, 50)
    min_k = 10
    if max_k <= min_k:
        return 0.5
    rs_values, n_values = [], []
    for k in range(min_k, max_k + 1, 5):
        rs_list = []
        for start in range(0, n - k + 1, k):
            sub = series[start:start + k]
            if len(sub) < k:
                continue
            dev = sub - np.mean(sub)
            cum = np.cumsum(dev)
            r = np.max(cum) - np.min(cum)
            s = np.std(sub, ddof=1)
            if s > 0:
                rs_list.append(r / s)
        if rs_list:
            rs_values.append(np.mean(rs_list))
            n_values.append(k)
    if len(rs_values) < 2:
        return 0.5
    try:
        slope, _ = np.polyfit(np.log(n_values), np.log(rs_values), 1)
        return float(np.clip(slope, 0.0, 1.0))
    except Exception:
        return 0.5


def half_life(series: np.ndarray) -> float:
    """OU half-life in periods — mirrors core/signals.py:_calculate_half_life."""
    n = len(series)
    if n < 10:
        return float("inf")
    lag = series[:-1]
    diff = series[1:] - series[:-1]
    x = np.mean(lag) - lag
    y = diff
    denom = np.dot(x, x)
    if denom == 0:
        return float("inf")
    theta = np.dot(x, y) / denom
    if theta <= 0:
        return float("inf")
    hl = float(np.log(2) / theta)
    # Clamp + round to match core/signals.py exactly
    hl = max(1.0, min(hl, float(n)))
    return round(hl, 1)


def score_pair(closes_a: np.ndarray, closes_b: np.ndarray,
               rt_bps: float) -> Optional[Dict]:
    """Fit beta, build spread, return metrics. None if data unusable."""
    n = min(len(closes_a), len(closes_b))
    if n < 30:
        return None
    a = closes_a[-n:]
    b = closes_b[-n:]
    if np.any(a <= 0):
        return None

    # OLS hedge ratio through the origin: beta = sum(a*b)/sum(a*a)
    beta = float(np.dot(a, b) / np.dot(a, a))
    if beta <= 0:
        return None

    spread = b - beta * a
    spread_std = float(np.std(spread, ddof=1))

    # Breakeven spread move (same formula as the engine's std filter):
    #   cost_price = rt_bps/10000 * beta * mean(priceA)  (== ~rt_bps on Leg B notional)
    cost_price = (rt_bps / 10000.0) * beta * float(np.mean(a))
    vol_cost = spread_std / cost_price if cost_price > 0 else float("inf")

    return {
        "beta": beta,
        "spread_std": spread_std,
        "cost_price": cost_price,
        "vol_cost": vol_cost,
        "hurst": hurst_rs(spread),
        "half_life": half_life(spread),
        "bars": n,
    }


async def main():
    ap = argparse.ArgumentParser(description="Rank stat-arb pairs by tradeable edge.")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS,
                    help="Instrument universe (OKX instIds).")
    ap.add_argument("--bar", default="5m", help="Candle size (1m,5m,15m,1H,...).")
    ap.add_argument("--bars", type=int, default=288, help="Candles per instrument (max 300).")
    ap.add_argument("--rt-bps", type=float, default=8.0, help="Round-trip cost in bps.")
    ap.add_argument("--top", type=int, default=25, help="Rows to print.")
    ap.add_argument("--min-vol-cost", type=float, default=0.0,
                    help="Hide pairs below this Vol/Cost ratio.")
    args = ap.parse_args()

    logger.info("Fetching %d candles (%s) for %d instruments...",
                args.bars, args.bar, len(args.symbols))
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[
            fetch_closes(session, s, args.bar, args.bars) for s in args.symbols
        ])
    closes = {s: c for s, c in zip(args.symbols, results) if c is not None and len(c) >= 30}
    logger.info("Got usable data for %d instruments.\n", len(closes))

    rows = []
    for a_sym, b_sym in itertools.combinations(sorted(closes), 2):
        m = score_pair(closes[a_sym], closes[b_sym], args.rt_bps)
        if not m or m["vol_cost"] < args.min_vol_cost:
            continue
        rows.append((a_sym, b_sym, m))

    # Rank: mean-reverting (hurst<0.5) first, then by Vol/Cost ratio
    rows.sort(key=lambda r: (r[2]["hurst"] >= 0.5, -r[2]["vol_cost"]))

    hl = lambda v: f"{v:6.1f}" if v != float("inf") else "   inf"
    print(f"{'Leg A':>16} / {'Leg B':<16}  {'beta':>9}  {'Vol/Cost':>8}  "
          f"{'Hurst':>6}  {'HalfLife':>8}  {'mean-rev?':>9}")
    print("-" * 88)
    for a_sym, b_sym, m in rows[:args.top]:
        mr = "YES" if m["hurst"] < 0.5 else "no"
        print(f"{a_sym:>16} / {b_sym:<16}  {m['beta']:9.4f}  {m['vol_cost']:8.2f}  "
              f"{m['hurst']:6.3f}  {hl(m['half_life']):>8}  {mr:>9}")

    print("\nGuide:")
    print("  Vol/Cost > ~2  : spread swings comfortably clear round-trip costs (tradeable)")
    print("  Hurst   < 0.5  : mean-reverting (what you want); >0.5 trends, avoid")
    print("  HalfLife       : periods (×%s) to revert halfway — short closes fast" % args.bar)
    print("  Scan uses perps for liquidity; trade the dated-futures equivalent of the same pair.")


if __name__ == "__main__":
    asyncio.run(main())
