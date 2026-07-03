#!/usr/bin/env python3
"""
READ-ONLY backtest: does the daily drift flag separate winners from losers?

Replays the real per-minute spread series (regime_snapshots, the same source
regime_report.py uses) through DailyDriftMonitor, day by day. Then — walk-forward,
using only data available up to each entry — reads the drift state at the moment
each trade was opened and buckets outcomes by it. If RANGING entries win and
TRENDING entries lose, the intraday drift flag has edge and is worth wiring in.
If they don't separate, we don't build it. Nothing is modified.

Usage (from anywhere; resolves the project DB automatically):
    python scripts/drift_backtest.py
    python scripts/drift_backtest.py --reset-hour 8 --warmup 30 --window 45
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.drift_analyzer import DailyDriftMonitor, analyze  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(_ROOT, "trading.db")


def _epoch_sec(v):
    """Accept ms-epoch ints or ISO strings; return float seconds (UTC)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000.0 if v > 1e11 else 1.0)
    s = str(v)
    try:
        f = float(s)
        return f / (1000.0 if f > 1e11 else 1.0)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:26], fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _day_key(sec, reset_hour):
    shifted = sec - reset_hour * 3600
    return datetime.fromtimestamp(shifted, tz=timezone.utc).strftime("%Y-%m-%d")


def _load_spreads(cur):
    """Prefer regime_snapshots (persisted per minute); fall back to spread_history."""
    for table in ("regime_snapshots", "spread_history"):
        try:
            rows = cur.execute(
                f"SELECT timestamp, spread FROM {table} "
                f"WHERE spread IS NOT NULL ORDER BY timestamp"
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        if rows:
            return table, rows
    return None, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--reset-hour", type=int, default=0, help="UTC hour the daily anchor resets")
    ap.add_argument("--warmup", type=int, default=30, help="morning warm-up minutes before the flag arms")
    ap.add_argument("--window", type=int, default=45, help="trailing assessment window minutes")
    ap.add_argument("--persist", type=int, default=3, help="consecutive TRENDING assessments to halt")
    args = ap.parse_args()

    con = sqlite3.connect(args.db); con.row_factory = sqlite3.Row; cur = con.cursor()

    table, sh = _load_spreads(cur)
    spreads = [(_epoch_sec(r["timestamp"]), float(r["spread"])) for r in sh]
    spreads = [(t, s) for t, s in spreads if t is not None]
    if len(spreads) < 100:
        print(f"Only {len(spreads)} usable spread rows (from {table or 'no table'}).\n"
              f"Need the live DB with intraday history in regime_snapshots — let it "
              f"run and collect for a few days, then re-run.")
        return
    print(f"source={table}  rows={len(spreads)}")

    dts = np.diff([t for t, _ in spreads])
    dt = float(np.median(dts[dts > 0])) if len(dts[dts > 0]) else 60.0
    warmup_n = max(20, int(args.warmup * 60 / dt))
    window_n = max(warmup_n, int(args.window * 60 / dt))
    print(f"cadence ~{dt:.0f}s/sample -> warmup={warmup_n} ({args.warmup}m), "
          f"window={window_n} ({args.window}m), halt after {args.persist}x TRENDING\n")

    by_day = defaultdict(list)
    for t, s in spreads:
        by_day[_day_key(t, args.reset_hour)].append((t, s))

    # ---- trades ----
    tcols = [c[1] for c in cur.execute("PRAGMA table_info(trades)").fetchall()]
    pnl_c = next((c for c in ("pnl_usd", "net_pnl_usd", "pnl", "realized_pnl") if c in tcols), None)
    ein_c = next((c for c in ("entry_time", "open_time", "opened_at") if c in tcols), None)
    rsn_c = next((c for c in ("exit_reason", "reason", "close_reason") if c in tcols), None)
    zin_c = next((c for c in ("entry_zscore", "entry_z", "zscore") if c in tcols), None)
    if not (pnl_c and ein_c):
        print(f"trades table missing pnl/entry_time. cols={tcols}"); return
    sel = f"SELECT {ein_c} AS et, {pnl_c} AS pnl"
    sel += f", {rsn_c} AS why" if rsn_c else ", '' AS why"
    sel += f", {zin_c} AS z" if zin_c else ", NULL AS z"
    sel += " FROM trades" + (" WHERE is_open=0" if "is_open" in tcols else "") + " ORDER BY et"
    trades = cur.execute(sel).fetchall()

    buckets = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    saved = {"n": 0, "pnl": 0.0}
    per_trade = []
    for tr in trades:
        et = _epoch_sec(tr["et"])
        if et is None:
            continue
        day = _day_key(et, args.reset_hour)
        day_spreads = [(t, s) for t, s in by_day.get(day, []) if t <= et]
        if len(day_spreads) < warmup_n:
            state, score, halted = "NO_DATA", float("nan"), False
        else:
            mon = DailyDriftMonitor(min_samples=warmup_n, halt_persistence=args.persist,
                                    assess_window=window_n)
            mon.reset_day()
            mon.set_anchor(float(np.mean([s for _, s in day_spreads[:warmup_n]])))
            for i, (_, s) in enumerate(day_spreads):
                mon.update(s)
                if i >= warmup_n:
                    mon.assess()
            m = analyze(np.asarray([s for _, s in day_spreads[-window_n:]]), anchor=mon._anchor)
            state, score, halted = m.state, m.trend_score, mon.should_halt()

        pnl = float(tr["pnl"] or 0)
        b = buckets[state]; b["n"] += 1; b["pnl"] += pnl; b["wins"] += (pnl > 0)
        if halted:
            saved["n"] += 1; saved["pnl"] += pnl
        per_trade.append((day, tr["z"], state, score, halted, pnl, str(tr["why"] or "")))

    print("=" * 74); print("DRIFT STATE AT ENTRY  vs  OUTCOME"); print("=" * 74)
    print(f"  {'state':<10}{'trades':>7}{'win%':>7}{'sum_pnl':>10}{'avg_pnl':>9}")
    for st in ("RANGING", "NEUTRAL", "TRENDING", "NO_DATA"):
        b = buckets.get(st)
        if not b or b["n"] == 0:
            continue
        print(f"  {st:<10}{b['n']:>7}{100*b['wins']/b['n']:>6.0f}%"
              f"{b['pnl']:>10.2f}{b['pnl']/b['n']:>9.2f}")

    print("\n" + "=" * 74); print("WHAT A DAILY HALT WOULD HAVE DONE"); print("=" * 74)
    print(f"  trades entered after the day was flagged TRENDING: {saved['n']}")
    print(f"  their combined P&L (what the halt would have skipped): {saved['pnl']:.2f}")

    print("\n" + "=" * 74); print("PER-TRADE (walk-forward, last 40)"); print("=" * 74)
    print(f"  {'day':<11}{'z':>7}  {'state':<9}{'score':>6}{'halt':>6}{'pnl':>9}  reason")
    for day, z, state, score, halted, pnl, why in per_trade[-40:]:
        zs = f"{z:.2f}" if isinstance(z, (int, float)) else "-"
        ss = f"{score:.2f}" if score == score else "-"
        print(f"  {day:<11}{zs:>7}  {state:<9}{ss:>6}{('Y' if halted else ''):>6}"
              f"{pnl:>9.2f}  {why}")
    con.close()
    print("\nDone. Nothing was modified.")


if __name__ == "__main__":
    main()
