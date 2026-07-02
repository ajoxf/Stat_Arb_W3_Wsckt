#!/usr/bin/env python3
"""
Regime / tradeability report from the regime_snapshots time series.

Answers the question we couldn't answer before: how often is this pair actually
tradeable — i.e. is the spread's sigma big enough (vs costs) to support an entry
at the entry threshold — and how much of the current "Edge: NO" is just a quiet
low-vol / trending patch versus a structural edge-below-cost problem.

The engine logs one snapshot per minute (see app.on_tick_callback), so a few
hours of runtime is enough for a first read; a few days gives a solid picture.

Usage:
    python scripts/regime_report.py                    # all history, config defaults
    python scripts/regime_report.py --days 7
    python scripts/regime_report.py --hours 12 --entry-z 2.8 --sigma-frac 0.5
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import mean, median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database.manager import DatabaseManager  # noqa: E402

_FMT = "%Y-%m-%d %H:%M:%S"


def _pct(n, d):
    return (100.0 * n / d) if d else 0.0


def _parse_ts(s):
    try:
        return datetime.strptime(str(s).replace("T", " ").split(".")[0], _FMT)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.getenv("DATABASE_PATH", "trading.db"))
    ap.add_argument("--asset", default=None, help="defaults to configured asset")
    ap.add_argument("--days", type=float, default=None)
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--sigma-frac", type=float, default=None, help="f; default from config")
    ap.add_argument("--entry-z", type=float, default=None, help="default from config entry_threshold")
    ap.add_argument("--req", type=float, default=None, help="required edge multiple; default from config")
    args = ap.parse_args()

    db = DatabaseManager(args.db)
    cfg = db.get_config()
    asset = args.asset or cfg.asset
    f = args.sigma_frac if args.sigma_frac is not None else (
        getattr(cfg, "profit_target_sigma_frac", 0.0) or 0.5)
    entry_z = args.entry_z if args.entry_z is not None else (cfg.entry_threshold or 2.5)
    req = args.req if args.req is not None else max(
        cfg.min_std_multiple or 0.0,
        getattr(cfg, "profit_target_min_cost_mult", 0.0) or 0.0) or 1.0

    since, window = None, "all history"
    if args.hours or args.days:
        hrs = (args.hours or 0) + (args.days or 0) * 24
        since = (datetime.now(timezone.utc) - timedelta(hours=hrs)).strftime(_FMT)
        window = f"last {hrs:.0f}h"

    rows = db.get_regime_snapshots(asset, since_iso=since)
    if not rows:
        print(f"No regime snapshots for {asset} ({window}).")
        print("The recorder writes ~1/min once the app is running and data_ready — "
              "let it collect, then re-run.")
        return

    n = len(rows)
    sig = [r["sigma_bps"] for r in rows if r.get("sigma_bps") is not None]
    cost = [r["cost_bps"] for r in rows if r.get("cost_bps") is not None]
    zs = [abs(r["zscore"]) for r in rows if r.get("zscore") is not None]
    hursts = [r["hurst"] for r in rows if r.get("hurst") is not None]
    drifts = [abs(r["beta_drift_pct"]) for r in rows if r.get("beta_drift_pct") is not None]

    # Core measure: would an entry AT the entry threshold clear the edge gate?
    #   f * entry_z * sigma_bps >= req * cost_bps
    would_pass, needs = 0, []
    for r in rows:
        sb, cb = r.get("sigma_bps"), r.get("cost_bps")
        if sb is None or cb is None:
            continue
        need = req * cb / (f * entry_z) if (f and entry_z) else float("inf")
        needs.append(need)
        if sb >= need:
            would_pass += 1
    live_pass = sum(1 for r in rows if r.get("edge_pass"))
    denom = len(needs) or 1

    t0, t1 = _parse_ts(rows[0]["timestamp"]), _parse_ts(rows[-1]["timestamp"])
    span_h = ((t1 - t0).total_seconds() / 3600.0) if (t0 and t1) else 0.0
    avg_need = mean(needs) if needs else 0.0
    tradeable = _pct(would_pass, denom)
    rate = f"~{n / span_h:.1f}/h over {span_h:.1f}h" if span_h > 0.02 else "span <1min"

    print(f"═══ REGIME / TRADEABILITY — {asset} ═══")
    print(f"Window:        {window}   ({rows[0]['timestamp']} → {rows[-1]['timestamp']})")
    print(f"Snapshots:     {n}  ({rate})")
    print(f"Assumptions:   f={f}  entry_z={entry_z}  req={req:.2f}x")
    print()
    print(f"► TRADEABLE @ entry z={entry_z}:  {tradeable:.1f}%  (~{tradeable / 100 * 24:.1f} h/day)")
    print(f"  (would an entry at the threshold clear the {req:.2f}x edge gate)")
    print(f"  Live edge passed (at whatever z was live): {_pct(live_pass, n):.1f}%")
    print()
    if sig:
        ss = sorted(sig)
        p90 = ss[min(int(0.9 * (len(ss) - 1)), len(ss) - 1)]
        print(f"sigma_bps:     min {min(sig):.2f}  median {median(sig):.2f}  "
              f"mean {mean(sig):.2f}  p90 {p90:.2f}  max {max(sig):.2f}")
        print(f"sigma needed:  ~{avg_need:.2f} bps  →  median sigma is "
              f"{'ABOVE ✓' if median(sig) >= avg_need else 'BELOW ✗'} the bar")
    if cost:
        print(f"cost_bps:      median {median(cost):.2f}")
    if zs:
        print(f"|z| ≥ {entry_z}:     {_pct(sum(1 for z in zs if z >= entry_z), len(zs)):.1f}% of the time")
    if hursts:
        print(f"Hurst ≥ 0.5:   {_pct(sum(1 for h in hursts if h >= 0.5), len(hursts)):.1f}% "
              f"(trending)   median H {median(hursts):.3f}")
    if drifts:
        print(f"|β drift|>1%:  {_pct(sum(1 for d in drifts if d > 1.0), len(drifts)):.1f}%   "
              f"max {max(drifts):.2f}%")
    print("regime mix:    " + "  ".join(f"{k}:{_pct(v, n):.0f}%" for k, v in Counter(
        (r.get("regime") or "?") for r in rows).most_common()))
    print()

    if tradeable >= 20:
        v = (f"Tradeable ~{tradeable:.0f}% of the time — real opportunity; the current "
             f"'Edge: NO' is a quiet patch, not a dead pair.")
    elif tradeable >= 5:
        v = (f"Thin — tradeable only ~{tradeable:.0f}% of the time. Works in bursts; "
             f"expect long idle gaps. Cutting costs (slippage) widens the window.")
    elif sig and max(sig) >= avg_need:
        v = (f"Rarely tradeable (~{tradeable:.0f}%). σ clears the bar only in spikes — edge is "
             f"structurally thin vs costs. Cut cost_bps or the pair won't pay at this size.")
    else:
        v = (f"Not tradeable in this window (~{tradeable:.0f}%). σ never clears the bar at these "
             f"costs — reduce cost_bps (slippage) or reconsider the pair/timeframe before sizing up.")
    print("VERDICT: " + v)


if __name__ == "__main__":
    main()
