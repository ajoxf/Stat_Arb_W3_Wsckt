#!/usr/bin/env python3
"""
scripts/reconcile_okx.py

Reconcile OKX Order History CSV against the bot's trades table.

The bot writes to its `trades` table only when an entry is recognised as
successful by `_execute_entry_orders` and matched at close. Two failure
modes break the record:

  1. The 'destructive auto-close loop' (06/18 cascade): an entry is
     recovered after a partial fill, but the engine doesn't recognise it
     as complete, so no trade row is written. Orphan detector then closes
     both legs at MARKET. The MARKET fills appear on OKX but nowhere in
     the bot's DB.

  2. Manual operator action (closing positions on the OKX app while the
     bot is running): the OKX fills happen, the DB knows nothing.

This script flags both: OKX orders that have no DB counterpart, DB trades
that have no OKX counterpart, and price mismatches between matched pairs.

USAGE
-----
    python scripts/reconcile_okx.py \\
        --csv path/to/Order_History.csv \\
        [--db trading.db] \\
        [--match-window-sec 600] \\
        [--csv-out reconcile_report.csv]

INPUT
-----
- OKX Order History CSV (download from Trade > Order Center > Filled
  Orders > Export). The CSV has two header lines: an account-meta line,
  then column headers, then data rows.
- Bot's SQLite trading.db.

OUTPUT
------
- Human-readable report on stdout.
- Optional CSV export with all unmatched rows for triage.

LIMITATIONS
-----------
- OKX CSV is one row per ORDER (not per fill). Partial-fill multi-fill
  rows show up as a single row with the average fill price. That's
  intentionally aligned with how the bot stores trades.
- Pairing of opens/closes uses time proximity within --match-window-sec
  (default 10min). Trades held longer than that won't pair cleanly;
  raise the window if your typical hold time is longer.
- USDT/AED currency conversions are skipped.
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


# ────────────────────────────────────────────────────────────────────────
# Data structures
# ────────────────────────────────────────────────────────────────────────

@dataclass
class OKXOrder:
    """One row from the OKX Order History CSV (a single completed order)."""
    order_id: str
    order_time: datetime
    symbol: str             # e.g. ETH-USDT-26JUN26
    side: str               # Open long | Open short | Close long | Close short
    order_type: str         # LIMIT | MARKET
    filled_qty: float       # base units (contracts × ctVal, but OKX expresses in contracts here)
    avg_fill_price: float
    pnl: float
    fee: float
    status: str             # COMPLETE | CANCELED | ...
    raw_row: Dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.side.startswith("Open")

    @property
    def is_close(self) -> bool:
        return self.side.startswith("Close")

    @property
    def is_market(self) -> bool:
        return self.order_type.upper() == "MARKET"

    @property
    def base_asset(self) -> str:
        # ETH-USDT-26JUN26 → ETH ; BTC-USDT-26JUN26 → BTC
        return self.symbol.split("-")[0]


@dataclass
class DBTrade:
    """One row from the bot's trades table."""
    id: int
    position_type: str       # LONG | SHORT
    entry_time: Optional[datetime]
    exit_time: Optional[datetime]
    entry_spot_price: float
    entry_futures_price: float
    exit_spot_price: float
    exit_futures_price: float
    quantity: float          # futures qty (BTC)
    pnl_usd: float
    is_open: bool


@dataclass
class MatchResult:
    db_trade: Optional[DBTrade]
    okx_open_eth: Optional[OKXOrder]
    okx_open_btc: Optional[OKXOrder]
    okx_close_eth: Optional[OKXOrder]
    okx_close_btc: Optional[OKXOrder]

    @property
    def is_full_match(self) -> bool:
        return all([
            self.db_trade, self.okx_open_eth, self.okx_open_btc,
            self.okx_close_eth, self.okx_close_btc,
        ])


# ────────────────────────────────────────────────────────────────────────
# Parsing
# ────────────────────────────────────────────────────────────────────────

def _strip_bom(s: str) -> str:
    """OKX CSVs sprinkle BOMs on EVERY line, not just file start."""
    return s.lstrip("﻿").strip() if s else s


def parse_okx_csv(path: str) -> List[OKXOrder]:
    """Parse an OKX Order History CSV. Skips spot-conversion rows."""
    orders: List[OKXOrder] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        # First line is account metadata ("UID:..., Account Type:...").
        first = f.readline()
        if "Order ID" not in first:
            # Not a header — proper CSV starts on next line
            reader = csv.reader(f)
        else:
            # No metadata, header was on line 1; rewind
            f.seek(0)
            reader = csv.reader(f)

        header: Optional[List[str]] = None
        for row in reader:
            if not row or not row[0].strip():
                continue
            cleaned = [_strip_bom(c) for c in row]
            if header is None and "Order ID" in cleaned:
                header = cleaned
                continue
            if header is None:
                continue
            if cleaned[0] == "Order ID":  # skip duplicate headers
                continue
            row_dict = dict(zip(header, cleaned))
            try:
                inst = row_dict.get("Instrument", "")
                symbol = row_dict.get("Symbol", "")
                # Skip USDT/AED conversions and any non-derivative rows
                if inst.lower() == "spot" or "CONVERT" in symbol.upper():
                    continue
                order = OKXOrder(
                    order_id=row_dict["Order ID"],
                    order_time=datetime.strptime(row_dict["Order Time"], "%Y-%m-%d %H:%M:%S"),
                    symbol=symbol,
                    side=row_dict.get("Side", ""),
                    order_type=row_dict.get("Order Type", ""),
                    filled_qty=float(row_dict.get("Filled Amount") or 0),
                    avg_fill_price=float(row_dict.get("Avg. Filled Price") or 0),
                    pnl=float(row_dict.get("PNL") or 0),
                    fee=float(row_dict.get("Fee") or 0),
                    status=row_dict.get("Status", ""),
                    raw_row=row_dict,
                )
                # Skip cancelled orders (no fill = nothing to reconcile)
                if order.status.upper() == "CANCELED" or order.filled_qty == 0:
                    continue
                orders.append(order)
            except (KeyError, ValueError) as e:
                print(f"WARNING: skipped malformed row: {e}: {row_dict}",
                      file=sys.stderr)
    return orders


def load_db_trades(db_path: str) -> List[DBTrade]:
    """Load all closed trades from the bot's DB. Open trades are excluded
    because there's no exit to reconcile against."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT id, position_type, entry_time, exit_time,
               entry_spot_price, entry_futures_price,
               exit_spot_price, exit_futures_price,
               quantity, pnl_usd, is_open
        FROM trades
        ORDER BY id DESC
    """)
    out: List[DBTrade] = []
    for r in cur.fetchall():
        out.append(DBTrade(
            id=r["id"],
            position_type=r["position_type"],
            entry_time=datetime.fromisoformat(r["entry_time"]) if r["entry_time"] else None,
            exit_time=datetime.fromisoformat(r["exit_time"]) if r["exit_time"] else None,
            entry_spot_price=r["entry_spot_price"] or 0.0,
            entry_futures_price=r["entry_futures_price"] or 0.0,
            exit_spot_price=r["exit_spot_price"] or 0.0,
            exit_futures_price=r["exit_futures_price"] or 0.0,
            quantity=r["quantity"] or 0.0,
            pnl_usd=r["pnl_usd"] or 0.0,
            is_open=bool(r["is_open"]),
        ))
    conn.close()
    return out


# ────────────────────────────────────────────────────────────────────────
# Matching
# ────────────────────────────────────────────────────────────────────────

def _within_window(t1: datetime, t2: datetime, window_sec: int) -> bool:
    return abs((t1 - t2).total_seconds()) <= window_sec


def _find_okx_match(
    orders: List[OKXOrder],
    target_time: datetime,
    target_price: float,
    side_prefix: str,         # "Open" or "Close"
    base_asset: str,          # "ETH" or "BTC"
    window_sec: int,
    price_tol_bps: float = 5.0,
    consumed: Optional[set] = None,
) -> Optional[OKXOrder]:
    """Find an OKX order that best matches the given side/asset/time/price."""
    consumed = consumed if consumed is not None else set()
    best: Optional[OKXOrder] = None
    best_diff = float("inf")
    for o in orders:
        if id(o) in consumed:
            continue
        if not o.side.lower().startswith(side_prefix.lower()):
            continue
        if o.base_asset != base_asset:
            continue
        if not _within_window(o.order_time, target_time, window_sec):
            continue
        if target_price > 0 and o.avg_fill_price > 0:
            px_diff_bps = abs(o.avg_fill_price - target_price) / target_price * 10_000
            if px_diff_bps > price_tol_bps:
                continue
            score = px_diff_bps + abs((o.order_time - target_time).total_seconds()) / 60.0
        else:
            score = abs((o.order_time - target_time).total_seconds())
        if score < best_diff:
            best_diff = score
            best = o
    return best


def reconcile(
    okx_orders: List[OKXOrder],
    db_trades: List[DBTrade],
    window_sec: int = 600,
    price_tol_bps: float = 10.0,
) -> Tuple[List[MatchResult], List[OKXOrder]]:
    """Pair DB trades with OKX orders. Returns:
       - matches: one MatchResult per DB closed trade, with whatever OKX
                  orders we could find for each of its 4 legs
       - unmatched_okx: OKX orders not claimed by any DB trade
    """
    matches: List[MatchResult] = []
    consumed: set = set()

    for trade in db_trades:
        if trade.is_open or not trade.entry_time:
            continue

        eth_open = _find_okx_match(
            okx_orders, trade.entry_time, trade.entry_spot_price,
            "Open", "ETH", window_sec, price_tol_bps, consumed,
        )
        btc_open = _find_okx_match(
            okx_orders, trade.entry_time, trade.entry_futures_price,
            "Open", "BTC", window_sec, price_tol_bps, consumed,
        )
        eth_close = _find_okx_match(
            okx_orders, trade.exit_time or trade.entry_time, trade.exit_spot_price,
            "Close", "ETH", window_sec, price_tol_bps, consumed,
        )
        btc_close = _find_okx_match(
            okx_orders, trade.exit_time or trade.entry_time, trade.exit_futures_price,
            "Close", "BTC", window_sec, price_tol_bps, consumed,
        )

        for o in (eth_open, btc_open, eth_close, btc_close):
            if o is not None:
                consumed.add(id(o))

        matches.append(MatchResult(
            db_trade=trade,
            okx_open_eth=eth_open,
            okx_open_btc=btc_open,
            okx_close_eth=eth_close,
            okx_close_btc=btc_close,
        ))

    unmatched = [o for o in okx_orders if id(o) not in consumed]
    return matches, unmatched


# ────────────────────────────────────────────────────────────────────────
# Reporting
# ────────────────────────────────────────────────────────────────────────

def _fmt_time(t: Optional[datetime]) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S") if t else "-"


def _classify_unmatched(orders: List[OKXOrder]) -> Dict[str, List[OKXOrder]]:
    """Group unmatched OKX orders by likely cause."""
    groups = {
        "MARKET orphan close (auto-flatten by orphan detector)": [],
        "LIMIT trade with no DB record (entry never persisted)": [],
        "MARKET trade with no DB record (likely manual or auto-close)": [],
    }
    for o in orders:
        if o.is_market and o.is_close:
            groups["MARKET orphan close (auto-flatten by orphan detector)"].append(o)
        elif o.is_market:
            groups["MARKET trade with no DB record (likely manual or auto-close)"].append(o)
        else:
            groups["LIMIT trade with no DB record (entry never persisted)"].append(o)
    return {k: v for k, v in groups.items() if v}


def report(
    matches: List[MatchResult],
    unmatched: List[OKXOrder],
    csv_out: Optional[str] = None,
) -> int:
    """Print a human-readable reconciliation report. Returns # of issues found."""
    issues = 0

    print("=" * 78)
    print("OKX ↔ Bot DB Reconciliation Report")
    print("=" * 78)

    # ---- DB trades and their OKX coverage ----
    fully_matched = [m for m in matches if m.is_full_match]
    partial_matched = [m for m in matches if not m.is_full_match]
    print()
    print(f"DB trades evaluated:        {len(matches)}")
    print(f"  Fully matched on OKX:     {len(fully_matched)}")
    print(f"  Partial / missing legs:   {len(partial_matched)}")

    if partial_matched:
        issues += len(partial_matched)
        print()
        print("─" * 78)
        print("DB TRADES WITH MISSING OKX LEGS")
        print("─" * 78)
        for m in partial_matched:
            t = m.db_trade
            missing = []
            if m.okx_open_eth is None:  missing.append("open-ETH")
            if m.okx_open_btc is None:  missing.append("open-BTC")
            if m.okx_close_eth is None: missing.append("close-ETH")
            if m.okx_close_btc is None: missing.append("close-BTC")
            print(
                f"  trade #{t.id} ({t.position_type}) "
                f"{_fmt_time(t.entry_time)} → {_fmt_time(t.exit_time)} | "
                f"pnl=${t.pnl_usd:+.2f} | missing: {', '.join(missing)}"
            )

    # ---- Price-mismatch check on matched trades ----
    px_mismatches = []
    for m in fully_matched:
        t = m.db_trade
        for label, db_price, okx in (
            ("entry-ETH", t.entry_spot_price, m.okx_open_eth),
            ("entry-BTC", t.entry_futures_price, m.okx_open_btc),
            ("exit-ETH",  t.exit_spot_price, m.okx_close_eth),
            ("exit-BTC",  t.exit_futures_price, m.okx_close_btc),
        ):
            if db_price == 0 or not okx or okx.avg_fill_price == 0:
                continue
            diff_bps = abs(okx.avg_fill_price - db_price) / db_price * 10_000
            if diff_bps > 1.0:  # >1bp difference is suspicious
                px_mismatches.append((t.id, label, db_price, okx.avg_fill_price, diff_bps))
    if px_mismatches:
        issues += len(px_mismatches)
        print()
        print("─" * 78)
        print("PRICE MISMATCHES (DB vs OKX, > 1 bp)")
        print("─" * 78)
        for tid, leg, db_p, okx_p, bps in px_mismatches:
            print(f"  trade #{tid} {leg:10s}  db={db_p:>12,.4f}  okx={okx_p:>12,.4f}  Δ={bps:>6.2f} bps")

    # ---- Unmatched OKX orders ----
    if unmatched:
        issues += len(unmatched)
        print()
        print("─" * 78)
        print(f"OKX ORDERS WITHOUT A DB MATCH ({len(unmatched)} total)")
        print("─" * 78)
        groups = _classify_unmatched(unmatched)
        for label, group in groups.items():
            total_pnl = sum(o.pnl for o in group)
            total_fees = sum(o.fee for o in group)
            print(f"\n  ▸ {label}  [{len(group)} order(s), Σpnl={total_pnl:+.2f} USDT, Σfees={total_fees:.2f} USDT]")
            for o in group:
                print(
                    f"     {_fmt_time(o.order_time)}  {o.symbol:25s}  "
                    f"{o.side:14s} {o.order_type:8s}  "
                    f"qty={o.filled_qty:>7,.4f}  px={o.avg_fill_price:>10,.4f}  "
                    f"pnl={o.pnl:+7.2f}  fee={o.fee:+7.4f}"
                )

    # ---- Summary ----
    print()
    print("=" * 78)
    if issues == 0:
        print("✅ Clean: DB and OKX are aligned within tolerance.")
    else:
        print(f"⚠️  {issues} reconciliation issue(s) found. See sections above.")
        print()
        print("Common explanations for unmatched OKX orders:")
        print("  • MARKET close orders = orphan auto-flatten by the bot's")
        print("    orphan detector after a recovery wasn't recorded as a trade.")
        print("    These should disappear after the cef6cba PARTIAL-fill fix.")
        print("  • LIMIT orders with no DB record = entry succeeded on OKX but")
        print("    the bot's _execute_entry_orders returned False (state-machine")
        print("    bug). The override in 9427a20 should now record these.")
        print("  • Hand-typed trades on the OKX app won't appear in the bot's")
        print("    DB — that's expected, not a bug.")
    print("=" * 78)

    # ---- Optional CSV output ----
    if csv_out:
        _write_csv(csv_out, matches, unmatched)
        print(f"\nDetailed CSV written to: {csv_out}")

    return issues


def _write_csv(path: str, matches: List[MatchResult], unmatched: List[OKXOrder]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "category", "db_trade_id", "db_position_type",
            "db_entry_time", "db_exit_time", "db_pnl_usd",
            "okx_order_id", "okx_order_time", "okx_symbol", "okx_side",
            "okx_order_type", "okx_filled_qty", "okx_avg_fill_price",
            "okx_pnl", "okx_fee", "okx_status", "notes",
        ])
        for m in matches:
            if m.is_full_match:
                continue
            t = m.db_trade
            for label, o in (
                ("missing-open-eth",  m.okx_open_eth),
                ("missing-open-btc",  m.okx_open_btc),
                ("missing-close-eth", m.okx_close_eth),
                ("missing-close-btc", m.okx_close_btc),
            ):
                if o is None:
                    w.writerow([
                        label, t.id, t.position_type,
                        _fmt_time(t.entry_time), _fmt_time(t.exit_time), t.pnl_usd,
                        "", "", "", "", "", "", "", "", "", "",
                        "no OKX order found within window",
                    ])
        for o in unmatched:
            w.writerow([
                "okx-unmatched", "", "",
                "", "", "",
                o.order_id, _fmt_time(o.order_time), o.symbol, o.side,
                o.order_type, o.filled_qty, o.avg_fill_price,
                o.pnl, o.fee, o.status,
                "no DB trade claims this order",
            ])


# ────────────────────────────────────────────────────────────────────────
# Entrypoint
# ────────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    p.add_argument("--csv", required=True, help="OKX Order History CSV path")
    p.add_argument("--db",  default="trading.db", help="Bot SQLite DB path (default: trading.db)")
    p.add_argument("--match-window-sec", type=int, default=600,
                   help="Max seconds between DB trade time and OKX order time for a match (default: 600 = 10min)")
    p.add_argument("--price-tol-bps", type=float, default=10.0,
                   help="Max price difference (bps) for a match (default: 10 bps)")
    p.add_argument("--csv-out", help="Write detailed reconciliation CSV here")
    args = p.parse_args(argv)

    if not os.path.exists(args.csv):
        print(f"ERROR: CSV not found: {args.csv}", file=sys.stderr)
        return 2
    if not os.path.exists(args.db):
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        return 2

    print(f"Loading OKX CSV: {args.csv}")
    okx_orders = parse_okx_csv(args.csv)
    print(f"  {len(okx_orders)} filled order(s) parsed (cancelled/spot-conversion rows skipped)")

    print(f"Loading bot DB:  {args.db}")
    db_trades = load_db_trades(args.db)
    closed = [t for t in db_trades if not t.is_open]
    print(f"  {len(closed)} closed trade(s) found ({len(db_trades) - len(closed)} still open)")

    matches, unmatched = reconcile(
        okx_orders, db_trades,
        window_sec=args.match_window_sec,
        price_tol_bps=args.price_tol_bps,
    )
    issues = report(matches, unmatched, csv_out=args.csv_out)
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
