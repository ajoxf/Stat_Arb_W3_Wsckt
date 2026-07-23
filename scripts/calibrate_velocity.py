#!/usr/bin/env python3
"""
Calibrate the Spread Velocity Exit (and sanity-check the Hurst exit) from real
trade history.

The live velocity exit (core/trading_engine.py) fires when the spread drifts
ADVERSELY faster than `velocity_exit_pts_per_min` for N consecutive ticks.
Picking that threshold by guessing is how you either (a) never fire or (b) cut
winners short. This script measures, for every CLOSED real trade, the fastest
adverse spread drift that ACTUALLY occurred during the trade's life, using the
1-minute `regime_snapshots` series (retained ~120 days) as the spread path.

It then shows, at a range of candidate thresholds, how many past LOSERS would
have been cut vs how many WINNERS would have been hit by mistake — so you can
choose a number that separates them. It also flags the "slow-grind" losers that
NO velocity threshold can catch (their drift was too gradual) and checks whether
the Hurst regime exit would have caught those instead.

CAVEAT: regime_snapshots is 1-minute resolution, while the live exit measures
over a 10-second window. So this UNDERSTATES short bursts — a threshold chosen
here is conservative: the live 10s-window exit will fire at least as readily.

Run on the box that holds trading.db (e.g. the EC2 live box):

    python scripts/calibrate_velocity.py                 # $DATABASE_PATH or ./trading.db
    python scripts/calibrate_velocity.py /path/trading.db

Read-only: opens the DB, computes, prints. It never writes.
"""
import bisect
import os
import sqlite3
import sys
from datetime import datetime

# Direction convention MUST match core/trading_engine.py: a SHORT position is
# hurt by the spread RISING, a LONG by the spread FALLING.
HURST_TREND_THRESHOLD = 0.55          # matches config default hurst_exit_threshold
CANDIDATE_THRESHOLDS = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0]
SUSTAIN_MINUTES = 2                   # require drift sustained this many minutes
MAX_GAP_MIN = 5.0                     # ignore snapshot gaps wider than this (bot was down)


def parse_ts(s):
    """Parse both 'YYYY-MM-DDTHH:MM:SS[.ffffff]' and 'YYYY-MM-DD HH:MM:SS'."""
    if not s:
        return None
    s = str(s).strip().replace("T", " ")
    s = s[:19]  # drop fractional seconds / timezone tail
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M")
        except ValueError:
            return None


def adverse_series(snaps, position_type):
    """snaps: list of (dt, spread) sorted by time. Returns per-minute adverse
    pts/min between consecutive snapshots (skips gaps > MAX_GAP_MIN)."""
    out = []
    for (t0, s0), (t1, s1) in zip(snaps, snaps[1:]):
        if s0 is None or s1 is None:
            continue
        dmin = (t1 - t0).total_seconds() / 60.0
        if dmin <= 0 or dmin > MAX_GAP_MIN:
            continue
        raw = (s1 - s0) / dmin
        out.append(raw if position_type == "SHORT" else -raw)
    return out


def max_sustained(series, n):
    """Max, over all windows of n consecutive values, of the window MINIMUM
    (i.e. the fastest drift that held for n minutes straight)."""
    if not series:
        return 0.0
    if len(series) < n:
        return min(series)  # not enough for a full window: honest = weakest link
    best = float("-inf")
    for i in range(len(series) - n + 1):
        best = max(best, min(series[i:i + n]))
    return best


def load_snapshots(conn):
    """asset -> (sorted list of dt, parallel list of spread)."""
    by_asset = {}
    rows = conn.execute(
        "SELECT asset, timestamp, spread, hurst FROM regime_snapshots "
        "WHERE spread IS NOT NULL ORDER BY asset, timestamp"
    ).fetchall()
    for r in rows:
        dt = parse_ts(r["timestamp"])
        if dt is None:
            continue
        a = by_asset.setdefault(r["asset"], {"dt": [], "spread": [], "hurst": []})
        a["dt"].append(dt)
        a["spread"].append(r["spread"])
        a["hurst"].append(r["hurst"])
    return by_asset


def slice_trade(asset_snaps, entry_dt, exit_dt):
    """Return (list[(dt,spread)], max_hurst) for snapshots within [entry,exit]."""
    dts = asset_snaps["dt"]
    lo = bisect.bisect_left(dts, entry_dt)
    hi = bisect.bisect_right(dts, exit_dt)
    pairs = list(zip(dts[lo:hi], asset_snaps["spread"][lo:hi]))
    hursts = [h for h in asset_snaps["hurst"][lo:hi] if h is not None]
    return pairs, (max(hursts) if hursts else None)


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DATABASE_PATH", "trading.db")
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}  (set DATABASE_PATH or pass a path)")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    snaps_by_asset = load_snapshots(conn)
    trades = conn.execute(
        "SELECT * FROM trades WHERE is_open = 0 AND COALESCE(is_paper,0) = 0 "
        "AND entry_time IS NOT NULL AND exit_time IS NOT NULL ORDER BY entry_time"
    ).fetchall()

    rows = []          # per-trade analysis with snapshot coverage
    no_cover = 0       # closed trades with no snapshot data in window
    for t in trades:
        entry_dt, exit_dt = parse_ts(t["entry_time"]), parse_ts(t["exit_time"])
        asset_snaps = snaps_by_asset.get(t["asset"])
        if entry_dt is None or exit_dt is None or not asset_snaps:
            no_cover += 1
            continue
        pairs, max_hurst = slice_trade(asset_snaps, entry_dt, exit_dt)
        if len(pairs) < 2:
            no_cover += 1
            continue
        series = adverse_series(pairs, t["position_type"])
        if not series:
            no_cover += 1
            continue
        dur_min = (exit_dt - entry_dt).total_seconds() / 60.0
        rows.append({
            "id": t["id"],
            "type": t["position_type"],
            "pnl": t["pnl_usd"] or 0.0,
            "reason": t["exit_reason"] or "-",
            "dur_min": dur_min,
            "snaps": len(pairs),
            "peak_1m": max(series),                       # fastest single-minute adverse drift
            "peak_sust": max_sustained(series, SUSTAIN_MINUTES),  # sustained SUSTAIN_MINUTES
            "max_hurst": max_hurst,
        })

    if not rows:
        print(f"No closed real trades with snapshot coverage in {db_path}.")
        print(f"(Closed trades with no regime_snapshots overlap: {no_cover})")
        print("This feature needs trades that ran AFTER regime_snapshots logging began.")
        return

    losers = [r for r in rows if r["pnl"] < 0]
    winners = [r for r in rows if r["pnl"] >= 0]

    print(f"\nDB: {db_path}")
    print(f"Closed real trades analysed: {len(rows)}  "
          f"(losers {len(losers)}, winners {len(winners)}; "
          f"no snapshot coverage: {no_cover})")
    print(f"Adverse drift metric: pts/min, sustained {SUSTAIN_MINUTES} min, "
          f"from 1-min regime_snapshots (understates sub-min bursts).\n")

    # ---- per-trade table ----
    print(f"{'id':>5} {'type':<5} {'pnl$':>9} {'dur_m':>7} {'snaps':>6} "
          f"{'peak1m':>8} {'peakSust':>9} {'maxHurst':>9}  reason")
    for r in sorted(rows, key=lambda x: x["pnl"]):
        hz = f"{r['max_hurst']:.3f}" if r["max_hurst"] is not None else "  -  "
        print(f"{r['id']:>5} {r['type']:<5} {r['pnl']:>9.2f} {r['dur_min']:>7.1f} "
              f"{r['snaps']:>6} {r['peak_1m']:>8.2f} {r['peak_sust']:>9.2f} {hz:>9}  {r['reason']}")

    def stat(vals, f):
        vals = sorted(vals)
        return f(vals) if vals else 0.0

    if losers:
        lv = [r["peak_sust"] for r in losers]
        print(f"\nLOSER adverse drift (sustained {SUSTAIN_MINUTES}m), pts/min: "
              f"min {min(lv):.2f} | median {stat(lv, lambda v: v[len(v)//2]):.2f} | max {max(lv):.2f}")
    if winners:
        wv = [r["peak_sust"] for r in winners]
        print(f"WINNER adverse drift (sustained {SUSTAIN_MINUTES}m), pts/min: "
              f"min {min(wv):.2f} | median {stat(wv, lambda v: v[len(v)//2]):.2f} | max {max(wv):.2f}")

    # ---- threshold tradeoff ----
    print("\nIf velocity_exit_pts_per_min = T, using this history:")
    print(f"{'T':>5} {'losers_cut':>12} {'winners_hit':>13}   note")
    best_t, best_score = None, -1e9
    for thr in CANDIDATE_THRESHOLDS:
        lc = sum(1 for r in losers if r["peak_sust"] >= thr)
        wh = sum(1 for r in winners if r["peak_sust"] >= thr)
        # Prefer cutting losers, penalise hitting winners ~2x (a cut winner is
        # a real opportunity cost). Simple, transparent scoring.
        score = lc - 2 * wh
        if score > best_score:
            best_score, best_t = score, thr
        print(f"{thr:>5.1f} {lc:>7}/{len(losers):<4} {wh:>8}/{len(winners):<4}")

    # ---- residual: slow grinds velocity can't catch, and does Hurst help? ----
    if best_t is not None and losers:
        missed = [r for r in losers if r["peak_sust"] < best_t]
        hurst_catch = [r for r in missed if r["max_hurst"] is not None
                       and r["max_hurst"] > HURST_TREND_THRESHOLD]
        print(f"\nSuggested threshold ≈ {best_t:.1f} pts/min "
              f"(cuts {sum(1 for r in losers if r['peak_sust'] >= best_t)}/{len(losers)} "
              f"losers, hits {sum(1 for r in winners if r['peak_sust'] >= best_t)}/{len(winners)} winners).")
        print(f"Losers too slow for velocity at that threshold: {len(missed)} "
              f"(slow grinds).")
        print(f"  ...of those, {len(hurst_catch)} had Hurst > {HURST_TREND_THRESHOLD} "
              f"during the trade → the HURST exit would likely catch them.")
        residual = len(missed) - len(hurst_catch)
        if residual > 0:
            print(f"  ...{residual} caught by NEITHER → these need a time-stop or "
                  f"hard dollar stop, not velocity/Hurst.")

    print("\nReminder: 1-min data understates the live 10s-window velocity, so the\n"
          "real exit fires at least as readily as this table implies. Start a\n"
          "notch HIGHER than the suggestion and tighten if it under-fires.\n")


if __name__ == "__main__":
    main()
