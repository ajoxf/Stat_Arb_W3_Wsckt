#!/usr/bin/env python3
"""
Loss attribution: WHEN and in what conditions does the strategy lose?

Tests the hypothesis that losses concentrate in (a) weekends / thin-liquidity
sessions and (b) high-volatility windows (scheduled economic data), rather than
being spread evenly. If they cluster, a calendar/volatility ENTRY filter beats
any exit tweak. If they don't, we've avoided filtering away good trades.

For each closed real trade it buckets realised P&L by:
  1. weekday vs weekend (UTC)
  2. day of week
  3. hour of day (UTC)  — US data lands ~12:30-14:00 & 18:00-19:00 UTC
  4. volatility regime AT ENTRY (sigma_bps from the nearest prior regime_snapshot)
and lists the big losers with their entry weekday / hour / entry-vol so you can
see if they share a signature.

All timestamps are whatever the DB stored (the app writes UTC). Translate to your
data calendar accordingly. Read-only.

Run on the box with trading.db:
    python scripts/loss_attribution.py            # $DATABASE_PATH or ./trading.db
    python scripts/loss_attribution.py /path/trading.db
"""
import bisect
import os
import sqlite3
import sys
from datetime import datetime

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
US_DATA_HOURS_UTC = {12, 13, 14, 18, 19}   # rough CPI/NFP (8:30 ET) & FOMC (14:00 ET)


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


def load_vol_path(conn):
    """asset -> {'dt':[...], 'sigma':[...]} sorted, for entry-vol lookup."""
    by_asset = {}
    for r in conn.execute("SELECT asset, timestamp, sigma_bps FROM regime_snapshots "
                          "WHERE sigma_bps IS NOT NULL ORDER BY asset, timestamp"):
        dt = parse_ts(r["timestamp"])
        if dt is None:
            continue
        a = by_asset.setdefault(r["asset"], {"dt": [], "sigma": []})
        a["dt"].append(dt)
        a["sigma"].append(r["sigma_bps"])
    return by_asset


def sigma_at(path, when):
    if not path:
        return None
    i = bisect.bisect_right(path["dt"], when) - 1
    if i < 0:
        return None
    return path["sigma"][i]


def block(title, buckets, order=None):
    """buckets: key -> [pnls]. Prints count / net / avg / win% per key."""
    print(f"\n{title}")
    print(f"{'bucket':>10} {'n':>4} {'net$':>10} {'avg$':>8} {'win%':>6} {'losers$':>10}")
    keys = order if order else sorted(buckets)
    for k in keys:
        v = buckets.get(k, [])
        if not v:
            continue
        net = sum(v)
        wins = sum(1 for x in v if x > 0)
        losers = sum(x for x in v if x < 0)
        print(f"{str(k):>10} {len(v):>4} {net:>10.2f} {net/len(v):>8.2f} "
              f"{100*wins/len(v):>5.0f}% {losers:>10.2f}")


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DATABASE_PATH", "trading.db")
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    vol = load_vol_path(conn)
    trades = conn.execute(
        "SELECT * FROM trades WHERE is_open = 0 AND COALESCE(is_paper,0) = 0 "
        "AND entry_time IS NOT NULL ORDER BY entry_time"
    ).fetchall()

    rows = []
    for t in trades:
        edt = parse_ts(t["entry_time"])
        if edt is None:
            continue
        rows.append({
            "id": t["id"], "type": t["position_type"], "pnl": t["pnl_usd"] or 0.0,
            "reason": t["exit_reason"] or "-", "wd": edt.weekday(), "hr": edt.hour,
            "sigma": sigma_at(vol.get(t["asset"]), edt),
        })

    if not rows:
        print("No closed real trades.")
        return

    total = sum(r["pnl"] for r in rows)
    print(f"\nDB: {db_path}")
    print(f"Closed real trades: {len(rows)}   net P&L ${total:.2f}")

    # 1. weekend vs weekday
    we = {"weekday": [], "weekend": []}
    for r in rows:
        we["weekend" if r["wd"] >= 5 else "weekday"].append(r["pnl"])
    block("[1] Weekend vs weekday (UTC):", we, order=["weekday", "weekend"])

    # 2. by day of week
    dow = {}
    for r in rows:
        dow.setdefault(WEEKDAYS[r["wd"]], []).append(r["pnl"])
    block("[2] By day of week:", dow, order=WEEKDAYS)

    # 3. by hour of day
    hod = {}
    for r in rows:
        hod.setdefault(r["hr"], []).append(r["pnl"])
    block("[3] By hour of day (UTC) — *=US data window:",
          {(f"{k:02d}*" if k in US_DATA_HOURS_UTC else f"{k:02d}"): v
           for k, v in hod.items()})

    # 4. volatility regime at entry
    with_sigma = [r for r in rows if r["sigma"] is not None]
    if with_sigma:
        s_sorted = sorted(r["sigma"] for r in with_sigma)
        med = s_sorted[len(s_sorted)//2]
        vb = {"low-vol": [], "high-vol": []}
        for r in with_sigma:
            vb["high-vol" if r["sigma"] > med else "low-vol"].append(r["pnl"])
        print(f"\n[4] Volatility regime AT ENTRY (split at median sigma_bps={med:.1f}):")
        block("", vb, order=["low-vol", "high-vol"])
        print(f"    ({len(rows)-len(with_sigma)} trades had no entry-vol snapshot)")

    # 5. big-loser signature
    big = sorted([r for r in rows if r["pnl"] < -20], key=lambda r: r["pnl"])
    if big:
        print(f"\n[5] Big losers (< -$20) — shared signature?")
        print(f"{'id':>5} {'type':<5} {'pnl$':>9} {'day':>4} {'hrUTC':>6} "
              f"{'entryVol(bps)':>13}  reason")
        for r in big:
            sg = f"{r['sigma']:.1f}" if r["sigma"] is not None else "  -  "
            star = "*" if r["hr"] in US_DATA_HOURS_UTC else " "
            print(f"{r['id']:>5} {r['type']:<5} {r['pnl']:>9.2f} "
                  f"{WEEKDAYS[r['wd']]:>4} {r['hr']:>4}{star:>1}  {sg:>13}  {r['reason']}")

    print("\nRead: if [1]/[2] show weekend net << weekday, or [5] clusters on Sat/Sun\n"
          "or the *-hours, your calendar-filter hypothesis holds. If [4] high-vol net\n"
          "<< low-vol, a volatility-gate at entry is the lever. Even net/evenly spread\n"
          "losses would instead point back at exit/stop logic.\n")


if __name__ == "__main__":
    main()
