#!/usr/bin/env python3
"""
MANUAL MONITORING ONLY — hedge-ratio (beta) z-score.

Reads the live hedge ratio (regime_snapshots.beta_live, logged per minute) and,
for each trading day, measures how far it has drifted from where it sat that
morning, in units of that morning's normal wiggle (a z-score). The tell you
asked for: |z| pushes past 2 and does NOT come back = the price ratio has
structurally moved (one leg outrunning the other) = trending regime.

This script has NO involvement in signal generation or order placement. It only
reads and prints. Nothing is modified.

    python scripts/beta_zscore_monitor.py
    python scripts/beta_zscore_monitor.py --anchor 38      # pin z around a fixed 38
    python scripts/beta_zscore_monitor.py --reset-hour 8 --warmup 30
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.drift_analyzer import beta_drift_status  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(_ROOT, "trading.db")

_STATUS_NOTE = {
    "STABLE":           "ratio held near its morning level (|z| < 2) — healthy",
    "DRIFTED_RETURNED": "drifted past 2 but came back — a wobble, not a trend",
    "DRIFTING":         "in the 1–2 band — watch it",
    "STRUCTURAL_DRIFT": "past 2 and NOT coming back — TRENDING regime warning",
    "WARMUP":           "not enough data yet",
}


def _epoch_sec(v):
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
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:26], fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _day_key(sec, reset_hour):
    return datetime.fromtimestamp(sec - reset_hour * 3600,
                                  tz=timezone.utc).strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--reset-hour", type=int, default=0)
    ap.add_argument("--warmup", type=int, default=30, help="morning minutes that set the anchor + baseline")
    ap.add_argument("--anchor", type=float, default=None, help="pin z around a fixed hedge ratio (e.g. 38)")
    ap.add_argument("--z-out", type=float, default=2.0)
    ap.add_argument("--z-in", type=float, default=1.0)
    args = ap.parse_args()

    con = sqlite3.connect(args.db); con.row_factory = sqlite3.Row; cur = con.cursor()
    try:
        rows = cur.execute(
            "SELECT timestamp, beta_live, beta_configured FROM regime_snapshots "
            "WHERE beta_live IS NOT NULL ORDER BY timestamp"
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"Can't read regime_snapshots.beta_live ({e}). Need the live DB.")
        return
    data = [(_epoch_sec(r["timestamp"]), float(r["beta_live"]),
             (float(r["beta_configured"]) if r["beta_configured"] is not None else None))
            for r in rows]
    data = [d for d in data if d[0] is not None]
    if len(data) < 40:
        print(f"Only {len(data)} beta_live rows — let regime_snapshots collect more.")
        return

    dts = np.diff([t for t, _, _ in data])
    dt = float(np.median(dts[dts > 0])) if len(dts[dts > 0]) else 60.0
    warmup_n = max(10, int(args.warmup * 60 / dt))

    by_day = defaultdict(list)
    cfg_by_day = defaultdict(list)
    for t, bl, bc in data:
        d = _day_key(t, args.reset_hour)
        by_day[d].append(bl)
        if bc is not None:
            cfg_by_day[d].append(bc)

    days = sorted(by_day)
    print(f"source=regime_snapshots  cadence ~{dt:.0f}s  warmup={warmup_n} "
          f"({args.warmup}m)  anchor={'fixed '+str(args.anchor) if args.anchor else 'morning mean'}\n")

    # headline: the latest (current) day
    latest = days[-1]
    s = beta_drift_status(by_day[latest], warmup_n=warmup_n, anchor=args.anchor,
                          z_out=args.z_out, z_in=args.z_in)
    cfg = np.median(cfg_by_day[latest]) if cfg_by_day.get(latest) else float("nan")
    print("=" * 72)
    print(f"TODAY ({latest})   [configured hedge ratio ~ {cfg:.2f}]")
    print("=" * 72)
    print(f"  anchor(this morning) = {s.anchor:.3f}   baseline wiggle = {s.baseline_std:.4f}")
    print(f"  live ratio now       = {s.current_beta:.3f}")
    print(f"  z now                = {s.current_z:+.2f}   (session max |z| = {s.max_abs_z:.2f})")
    if s.run_beyond:
        print(f"  been beyond ±{args.z_out:g} for ~{s.run_beyond} samples (~{s.run_beyond*dt/60:.0f} min)")
    print(f"  STATUS: {s.status}  — {_STATUS_NOTE.get(s.status,'')}\n")

    # history
    print("=" * 72)
    print("PER-DAY HISTORY")
    print("=" * 72)
    print(f"  {'day':<11}{'anchor':>8}{'now':>8}{'z_now':>7}{'max|z|':>8}{'min>2':>7}  status")
    for d in days:
        st = beta_drift_status(by_day[d], warmup_n=warmup_n, anchor=args.anchor,
                               z_out=args.z_out, z_in=args.z_in)
        mins = f"{st.run_beyond*dt/60:.0f}" if st.run_beyond else ""
        print(f"  {d:<11}{st.anchor:>8.2f}{st.current_beta:>8.2f}{st.current_z:>+7.1f}"
              f"{st.max_abs_z:>8.1f}{mins:>7}  {st.status}")
    con.close()
    print("\nManual-monitoring only. Nothing was modified, nothing was traded on this.")


if __name__ == "__main__":
    main()
