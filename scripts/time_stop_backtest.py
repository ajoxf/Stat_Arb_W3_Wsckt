#!/usr/bin/env python3
"""
Time-stop backtest: what net P&L would a HARD max-hold have produced?

The velocity calibration proved the losses are a fat tail of long-held
non-reverters, not fast movers. This script quantifies the fix: for each
candidate cut time T, it reconstructs every trade's P&L *at T minutes after
entry* and reports the total strategy P&L if a hard time-stop had forced the
exit there.

How the reconstruction works (self-calibrating, no leg/qty modelling needed):
  A spread position's gross P&L is LINEAR in the spread (that's what the hedge
  ratio buys you). Each trade reveals its own dollar-per-spread-point k from its
  realised outcome:
        k = pnl_gross / (favourable spread move entry->exit)
  Then at any intermediate spread S_t (from the 1-min regime_snapshots path):
        gross(t) = k * (favourable move entry->S_t)
        net(t)   = gross(t) - round_trip_fees
  A time-stop at T only changes trades that ran LONGER than T; trades that
  already exited before T keep their real P&L (they hit profit-target / z-exit
  first). So:
        pnl_under_stop(trade, T) = realised           if duration <= T
                                 = reconstructed(T)    if duration >  T

Caveats (stated, not hidden):
  * 1-min snapshot resolution; the real exit could fire a minute either side.
  * Funding on multi-hour perp holds is folded into realised P&L but not
    re-modelled at intermediate T — so very long holds' reconstructed P&L is
    spread-only and slightly optimistic on the funding component.
  * Trades whose spread barely moved (k undefined) or with no snapshot coverage
    are carried at their realised P&L unchanged and counted separately.

Run on the box with trading.db (read-only):
    python scripts/time_stop_backtest.py                 # $DATABASE_PATH or ./trading.db
    python scripts/time_stop_backtest.py /path/trading.db
"""
import bisect
import os
import sqlite3
import sys
from datetime import datetime, timedelta

CUTOFFS_MIN = [30, 45, 60, 90, 120, 180, 240, 360]
MIN_MOVE_EPS = 1e-6          # |favourable spread move| below this => k undefined
MAX_GAP_MIN = 10.0           # don't trust a cut spread pulled from a gap wider than this


def parse_ts(s):
    if not s:
        return None
    s = str(s).strip().replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def favourable_move(delta_spread, ptype):
    """Favourable = spread up for LONG, down for SHORT. delta = S - entry."""
    return delta_spread if ptype == "LONG" else -delta_spread


def load_spread_paths(conn):
    """asset -> {'dt':[...], 'spread':[...], 'hl':[...]} sorted by time."""
    by_asset = {}
    for r in conn.execute("SELECT asset, timestamp, spread, half_life "
                          "FROM regime_snapshots WHERE spread IS NOT NULL "
                          "ORDER BY asset, timestamp"):
        dt = parse_ts(r["timestamp"])
        if dt is None:
            continue
        a = by_asset.setdefault(r["asset"], {"dt": [], "spread": [], "hl": []})
        a["dt"].append(dt)
        a["spread"].append(r["spread"])
        a["hl"].append(r["half_life"])
    return by_asset


def spread_at(path, cut_dt, entry_dt):
    """Last spread at or before cut_dt (but at/after entry). None if no usable
    snapshot, or the nearest one sits across a gap wider than MAX_GAP_MIN."""
    dts = path["dt"]
    i = bisect.bisect_right(dts, cut_dt) - 1
    if i < 0 or dts[i] < entry_dt:
        return None
    if (cut_dt - dts[i]).total_seconds() / 60.0 > MAX_GAP_MIN:
        return None
    return path["spread"][i]


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DATABASE_PATH", "trading.db")
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}  (set DATABASE_PATH or pass a path)")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    paths = load_spread_paths(conn)
    trades = conn.execute(
        "SELECT * FROM trades WHERE is_open = 0 AND COALESCE(is_paper,0) = 0 "
        "AND entry_time IS NOT NULL AND exit_time IS NOT NULL "
        "AND entry_spread IS NOT NULL AND exit_spread IS NOT NULL ORDER BY entry_time"
    ).fetchall()

    sim = []          # trades we can reconstruct
    carried = 0       # trades carried at realised P&L (no coverage / flat spread)
    all_hl = []
    for t in trades:
        entry_dt, exit_dt = parse_ts(t["entry_time"]), parse_ts(t["exit_time"])
        if entry_dt is None or exit_dt is None:
            carried += 1
            continue
        ptype = t["position_type"]
        realised = t["pnl_usd"] or 0.0
        fees = t["fees_usd"] or 0.0
        gross = t["pnl_gross_usd"] if (t["pnl_gross_usd"] or 0.0) != 0.0 else realised + fees
        dur = (exit_dt - entry_dt).total_seconds() / 60.0
        path = paths.get(t["asset"])

        fav_total = favourable_move(t["exit_spread"] - t["entry_spread"], ptype)
        if path is None or abs(fav_total) < MIN_MOVE_EPS:
            carried += 1
            sim.append({"realised": realised, "dur": dur, "k": None, "t": t,
                        "entry_dt": entry_dt})
            continue
        k = gross / fav_total
        sim.append({"realised": realised, "dur": dur, "k": k, "fees": fees,
                    "entry_spread": t["entry_spread"], "ptype": ptype,
                    "entry_dt": entry_dt, "path": path, "t": t})
        for h in [path["hl"][i] for i in range(len(path["dt"]))
                  if entry_dt <= path["dt"][i] <= exit_dt and path["hl"][i]]:
            all_hl.append(h)

    def pnl_under_stop(s, T):
        if s["dur"] <= T or s["k"] is None:
            return s["realised"]
        cut_dt = s["entry_dt"] + timedelta(minutes=T)
        S_t = spread_at(s["path"], cut_dt, s["entry_dt"])
        if S_t is None:
            return s["realised"]  # no data at the cut -> can't simulate, carry
        gross_t = s["k"] * favourable_move(S_t - s["entry_spread"], s["ptype"])
        return gross_t - s["fees"]

    baseline = sum(s["realised"] for s in sim)
    med_hl = sorted(all_hl)[len(all_hl)//2] if all_hl else None

    print(f"\nDB: {db_path}")
    print(f"Simulatable closed real trades: {len(sim)}  "
          f"(carried at realised P&L, no coverage/flat spread: {carried})")
    if med_hl:
        print(f"Median half-life during trades: {med_hl:.1f} periods "
              f"(~{med_hl*0.5/60:.1f} min) — so cutoffs in half-lives ≈ min / {med_hl*0.5/60:.1f}")
    print(f"\nBASELINE (no time-stop): ${baseline:.2f}\n")

    print(f"{'T(min)':>7} {'≈half-lives':>12} {'total P&L':>11} {'vs base':>9} "
          f"{'#cut':>5} {'winners_lost':>13} {'losers_saved$':>14}")
    best = (None, -1e18)
    for T in CUTOFFS_MIN:
        total = sum(pnl_under_stop(s, T) for s in sim)
        cut = [s for s in sim if s["dur"] > T and s["k"] is not None]
        # winners forgone: trades that realised >0 but get cut to a lower value
        wl = sum(max(0.0, s["realised"] - pnl_under_stop(s, T))
                 for s in cut if s["realised"] > 0)
        # losers saved: reduction in loss on trades that realised <0
        ls = sum(max(0.0, pnl_under_stop(s, T) - s["realised"])
                 for s in cut if s["realised"] < 0)
        hl_txt = f"{T/(med_hl*0.5/60):.0f}x" if med_hl else "-"
        print(f"{T:>7} {hl_txt:>12} {total:>11.2f} {total-baseline:>+9.2f} "
              f"{len(cut):>5} {wl:>13.2f} {ls:>14.2f}")
        if total > best[1]:
            best = (T, total)

    print(f"\nBest cutoff by total P&L: T = {best[0]} min  ->  ${best[1]:.2f}  "
          f"(baseline ${baseline:.2f}, gain ${best[1]-baseline:+.2f})")

    # Per-big-loser detail at a few cutoffs
    big = sorted([s for s in sim if s["realised"] < -20 and s["k"] is not None],
                 key=lambda s: s["realised"])[:8]
    if big:
        show_T = [60, 90, 120]
        print(f"\nBiggest losers — realised vs reconstructed if cut at T:")
        hdr = "  ".join(f"@{T}m" for T in show_T)
        print(f"{'id':>5} {'type':<5} {'dur_m':>7} {'realised$':>10}   {hdr}")
        for s in big:
            vals = "  ".join(f"{pnl_under_stop(s, T):>6.1f}" for T in show_T)
            print(f"{s['t']['id']:>5} {s['ptype']:<5} {s['dur']:>7.0f} "
                  f"{s['realised']:>10.2f}   {vals}")

    print("\nNote: reconstruction is spread-linear (self-calibrated per trade) and\n"
          "does not re-model perp funding at intermediate T, so multi-hour holds'\n"
          "cut-P&L is marginally optimistic. Directional conclusion is robust.\n")


if __name__ == "__main__":
    main()
