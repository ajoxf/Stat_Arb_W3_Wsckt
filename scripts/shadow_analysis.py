#!/usr/bin/env python3
"""
Mine the shadow-hold ledger to set the hard-max-hold cutoff from real data.

Every stopped trade arms a "what-if-held" shadow that tracks, from ENTRY, whether
the position would have recovered and WHEN (trading_engine.py:_update_shadow_holds;
hit_be_min / hit_target_min are minutes from entry). This answers the three
questions the dashboard badge can't:

  1. Recovery rate — to TARGET (the badge's number) vs to BREAK-EVEN-or-better
     (the one that actually matters: "would the loss have come back?").
  2. Timing — WHEN recoveries happen (from entry), so the cutoff can be set to
     capture them instead of cutting winners early.
  3. Time-of-day / US-open / weekend — does recovery behaviour change by session?
     (thin weekends, US-open volatility, etc.)

Read-only. Run on the box with trading.db:
    python scripts/shadow_analysis.py                 # $DATABASE_PATH or ./trading.db
    python scripts/shadow_analysis.py /path/trading.db
"""
import os
import sqlite3
import sys
from datetime import datetime

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# US equity open 09:30 ET = 13:30 UTC (EDT) / 14:30 UTC (EST). Flag the band.
US_OPEN_HOURS = {13, 14}
US_SESSION_HOURS = set(range(13, 21))     # ~US cash session in UTC
CUTOFFS = [45, 60, 75, 90, 110, 130, 150, 180]


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


def pct(n, d):
    return f"{100.0*n/d:.0f}%" if d else "  -"


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DATABASE_PATH", "trading.db")
    if not os.path.exists(db):
        print(f"DB not found: {db}")
        sys.exit(1)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # Prefer the trade's entry_time for the session lens; fall back to the
    # shadow row's own timestamp (exit time) when the trade row is gone.
    rows = conn.execute("""
        SELECT s.*, t.entry_time AS entry_time
        FROM shadow_holds s LEFT JOIN trades t ON s.trade_id = t.id
        ORDER BY s.id
    """).fetchall()
    rows = [dict(r) for r in rows]
    if not rows:
        print("No shadow_holds rows yet. (Each stopped trade arms one; they take "
              "up to 8h to finalize.)")
        return

    # Drop corrupt rows: a shadow finalized ACROSS a pair/symbol change computes
    # P&L with mismatched prices and produces absurd values (live: #141 showed
    # +$214,232 on a −$61 stop). No real trade at this size reaches |$10k|, so
    # anything beyond that is a data-integrity artifact, not a recovery.
    CORRUPT = 10_000.0
    def _bad(r):
        return any(abs(r.get(k) or 0) > CORRUPT
                   for k in ("exit_net_usd", "peak_net_usd", "trough_net_usd", "final_net_usd"))
    corrupt = [r for r in rows if _bad(r)]
    rows = [r for r in rows if not _bad(r)]
    if corrupt:
        ids = ", ".join(f"#{r.get('trade_id','?')}" for r in corrupt)
        print(f"⚠ Skipped {len(corrupt)} corrupt shadow row(s) (|P&L| > ${CORRUPT:,.0f} — "
              f"finalized across a pair/symbol change): {ids}")
        print("  Purge them from the DB so the dashboard badge is clean too (see script note).\n")
    if not rows:
        print("All shadow rows were corrupt — nothing to analyse.")
        return

    for r in rows:
        r["_when"] = parse_ts(r.get("entry_time")) or parse_ts(r.get("timestamp"))
        r["_be"] = bool(r.get("hit_break_even"))
        r["_tp"] = bool(r.get("hit_target"))

    n = len(rows)
    be = sum(1 for r in rows if r["_be"])
    tp = sum(1 for r in rows if r["_tp"])
    bleed = n - be

    print(f"\nDB: {db}")
    print(f"Shadow-holds finalized: {n}\n")
    print("=" * 60)
    print("[1] RECOVERY RATE  (of stopped trades, would-have-...)")
    print("=" * 60)
    print(f"  reverted to TARGET      : {tp:>3}/{n}  ({pct(tp,n)})   <- the dashboard badge")
    print(f"  reverted to BE-or-better: {be:>3}/{n}  ({pct(be,n)})   <- the number that matters")
    print(f"  kept bleeding (no BE)   : {bleed:>3}/{n}  ({pct(bleed,n)})")
    print(f"  avg exit P&L when stopped: ${sum(r['exit_net_usd'] or 0 for r in rows)/n:+.2f}")

    # [2] Timing of recoveries (minutes FROM ENTRY)
    def stat(vals):
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return "  n/a"
        m = vals[len(vals)//2]
        p75 = vals[min(len(vals)-1, int(len(vals)*0.75))]
        return f"median {m:.0f}m | p75 {p75:.0f}m | max {max(vals):.0f}m"
    print("\n" + "=" * 60)
    print("[2] WHEN recoveries happen  (minutes from ENTRY)")
    print("=" * 60)
    print(f"  time to BREAK-EVEN : {stat([r.get('hit_be_min') for r in rows if r['_be']])}")
    print(f"  time to TARGET     : {stat([r.get('hit_target_min') for r in rows if r['_tp']])}")

    # [3] Cutoff capture: of the eventual recoveries, how many happen BY time T?
    print("\n" + "=" * 60)
    print("[3] CUTOFF CAPTURE  (a hard-max-hold of T lets the trade reach the")
    print("    level BEFORE it's cut — higher T captures more late reverters)")
    print("=" * 60)
    print(f"{'T(min)':>7} {'BE reached by T':>18} {'TARGET reached by T':>21}")
    for T in CUTOFFS:
        be_by = sum(1 for r in rows if r["_be"] and (r.get("hit_be_min") or 1e9) <= T)
        tp_by = sum(1 for r in rows if r["_tp"] and (r.get("hit_target_min") or 1e9) <= T)
        print(f"{T:>7} {f'{be_by}/{be} ({pct(be_by,be)})':>18} {f'{tp_by}/{tp} ({pct(tp_by,tp)})':>21}")
    print("  Read: pick T where the TARGET column stops climbing — beyond it you're")
    print("  only holding non-reverters longer for no extra winners captured.")

    # [4] Session lens: recovery rate by weekday/weekend and US-open
    def bucket(rows_, keyfn, order=None):
        b = {}
        for r in rows_:
            if r["_when"] is None:
                continue
            b.setdefault(keyfn(r), []).append(r)
        keys = order or sorted(b)
        for k in keys:
            v = b.get(k, [])
            if not v:
                continue
            bev = sum(1 for r in v if r["_be"])
            tpv = sum(1 for r in v if r["_tp"])
            print(f"  {str(k):<10} n={len(v):>3}  BE {pct(bev,len(v)):>4}  TARGET {pct(tpv,len(v)):>4}  "
                  f"bleed {pct(len(v)-bev,len(v)):>4}")

    print("\n" + "=" * 60)
    print("[4] RECOVERY by SESSION  (does when-you-enter change reversion?)")
    print("=" * 60)
    print("  weekday vs weekend (UTC):")
    bucket(rows, lambda r: "weekend" if r["_when"].weekday() >= 5 else "weekday",
           order=["weekday", "weekend"])
    print("  by day:")
    bucket(rows, lambda r: WEEKDAYS[r["_when"].weekday()], order=WEEKDAYS)
    print("  US cash session vs off-hours (13:00-21:00 UTC = US open+):")
    bucket(rows, lambda r: "US-session" if r["_when"].hour in US_SESSION_HOURS else "off-hours",
           order=["US-session", "off-hours"])
    print("  by UTC hour (* = US-open 13-14 UTC):")
    bucket(rows, lambda r: f"{r['_when'].hour:02d}" + ("*" if r["_when"].hour in US_OPEN_HOURS else ""))

    # [5] The non-reverters — do they share a session signature? (entry-filter clue)
    bleeders = [r for r in rows if not r["_be"] and r["_when"] is not None]
    if bleeders:
        print("\n" + "=" * 60)
        print(f"[5] KEPT-BLEEDING trades ({len(bleeders)}) — session signature")
        print("    (if these cluster on weekends / US-open, that's your entry filter)")
        print("=" * 60)
        we = sum(1 for r in bleeders if r["_when"].weekday() >= 5)
        us = sum(1 for r in bleeders if r["_when"].hour in US_SESSION_HOURS)
        print(f"  on weekends: {we}/{len(bleeders)} ({pct(we,len(bleeders))})")
        print(f"  in US session (13-21 UTC): {us}/{len(bleeders)} ({pct(us,len(bleeders))})")
        print("  worst bleeders (by trough):")
        for r in sorted(bleeders, key=lambda r: r.get("trough_net_usd") or 0)[:6]:
            wd = WEEKDAYS[r["_when"].weekday()]
            print(f"    #{r.get('trade_id','?'):>4} {r.get('position_type','?'):<5} {wd} "
                  f"{r['_when'].hour:02d}:00 UTC  trough ${r.get('trough_net_usd') or 0:+.1f} "
                  f"@{r.get('trough_min') or 0:.0f}m  exit ${r.get('exit_net_usd') or 0:+.1f}")

    print("\nNote: hit_be_min / hit_target_min are minutes FROM ENTRY. Small samples "
          "make session buckets noisy — weight the aggregate rows [1]-[3] more until "
          "the counts build.\n")


if __name__ == "__main__":
    main()
