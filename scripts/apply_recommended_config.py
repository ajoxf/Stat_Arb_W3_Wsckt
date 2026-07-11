#!/usr/bin/env python3
"""
Apply the recommended stat-arb config set to the live DB, coherently and in one
shot — the "high-WR / small-win" frontier this pair actually supports (see the
RR/EV analysis). Prints a before -> after diff and only writes if something
changes. Safe to re-run (idempotent).

What it sets and WHY (short form):
  - exit_signal_mode = spread      capture the REAL reversion, not drifted-mean z
  - profit_target_capital_pct = 0.5    a real win target (you had NONE: 0.0)
  - profit_target_min_cost_mult = 0    OFF — the 1.5x cost floor was pushing the target
                                       ABOVE a full reversion's value so it never fired
  - slippage_bps = 0.7             match real maker fills (was 1.5 -> inflated cost by ~2x)
  - hurst_enabled = False          Hurst is too often misleading — kept OFF (regime watched manually)
  - trailing_stop OFF              with a reachable TP it only cut winners early (live #91: $0.15 vs $1.81)
  - z_stop_exit_enabled = False    in-trade stop is the %-capital dollar stop only
  - min_entry_rr_multiple = 0.0    let the 1% capital cap set the stop (0.3 was inert)
  - min_std_multiple = 2.0         edge gate: only trade sigma >= 2x cost
  - stop 1.0% / gate 0.3% / max-hold 4x(20m)   kept, made explicit

The running engine loads config from the DB at start and on a Settings save, so
after this writes: RESTART the app, OR open Settings and hit Save once, for the
live engine to pick the new values up.

Usage:
    python scripts/apply_recommended_config.py            # show diff, then apply
    python scripts/apply_recommended_config.py --dry-run  # show diff only
    python scripts/apply_recommended_config.py --db trading.db
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database.manager import DatabaseManager  # noqa: E402

# The recommended set. Keys must match TradingConfig attribute names.
RECOMMENDED = {
    # ── entry selectivity ──
    "entry_threshold": 3.5,
    "stop_loss_threshold": 5.5,       # entry ceiling (blocks |z| >= this)
    "std_filter_enabled": True,
    "min_std_multiple": 2.0,          # Edge Filter: sigma >= 2x round-trip cost
    "hurst_enabled": False,           # Hurst is too often misleading — regime is watched manually, not gated
    "slippage_bps": 0.7,              # match real maker fills — un-inflates the cost floor & edge gate
    # ── exit signal ──
    "exit_threshold": 0.5,
    "exit_signal_mode": "spread",     # freeze entry mean; exit on true reversion
    # ── win side (profit target) ──
    "profit_target_sigma_frac": 0.0,
    "profit_target_capital_pct": 0.5,  # BANK THE WIN at BE + 0.5% of capital
    "profit_target_usd": 0.0,
    "profit_target_min_cost_mult": 0.0,  # OFF — the %-capital target is already net-of-fees;
                                         # the 1.5x floor was shoving the target ABOVE a full
                                         # reversion's value, so it could never fire.
    # ── loss side (stop) ──
    "stop_loss_capital_pct": 1.0,     # 1% capital catastrophe cap = your 1R
    "max_loss_usd": 0.0,
    "min_entry_rr_multiple": 0.0,     # cap sets the stop; RR field left inert
    "z_stop_exit_enabled": False,     # dollar stop only once in the trade
    # ── max hold: OFF ──
    # Disabled by choice — nothing time-based should cut a slow-reverting trade.
    # With max-hold off, a trade ends only on TP, a real reversion (spread back
    # to the entry mean), or the -1R dollar stop. BOTH drivers must be 0: the
    # ×half-life form takes precedence over the minutes fallback, so zeroing one
    # alone leaves it armed. Safe with exit_profit_gate on ONLY in spread mode
    # (the reversion exit fires deep in profit there, so the gate never bites and
    # can't deadlock); if you switch to zscore/hybrid, also set the gate to 0.
    "max_hold_halflife_mult": 0.0,
    "max_hold_minutes": 0.0,
    "max_hold_z_progress_min": 0.5,
    # ── exit profit gate (win floor on reversion exits) ──
    "exit_profit_gate_pct": 0.3,
    "exit_profit_gate_usd": 1.0,
    # ── trailing stop: OFF ──
    # With a REACHABLE take-profit (cost floor now 0), the trailing stop only
    # cuts winners mid-reversion before TP fires — live #91 banked $0.15 via a
    # 35% pullback while the spread kept reverting and would have hit TP for
    # $1.81. Let the target bank the win; the dollar stop caps the loss at -1R.
    "trailing_stop_pct": 0.0,
    "trailing_stop_floor_pct": 0.0,
}


def _fmt(v):
    if isinstance(v, bool):
        return "ON" if v else "OFF"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.getenv("DATABASE_PATH", "trading.db"))
    ap.add_argument("--dry-run", action="store_true",
                    help="print the diff but do not write")
    args = ap.parse_args()

    db = DatabaseManager(db_path=args.db)
    cfg = db.get_config()

    print(f"\nRecommended config — target: {args.db}\n" + "=" * 58)
    print(f"{'field':<28}{'current':>12}  ->  {'new':<10}")
    print("-" * 58)
    changes = {}
    for key, new in RECOMMENDED.items():
        if not hasattr(cfg, key):
            print(f"{key:<28}{'(missing)':>12}      skipped")
            continue
        cur = getattr(cfg, key)
        # Normalize numeric compare so 1 vs 1.0 isn't a false 'change'.
        same = (bool(cur) == bool(new)) if isinstance(new, bool) else (
            abs(float(cur) - float(new)) < 1e-9 if isinstance(new, (int, float))
            and isinstance(cur, (int, float)) else cur == new)
        flag = "" if same else "  <-- CHANGED"
        if not same:
            changes[key] = (cur, new)
        print(f"{key:<28}{_fmt(cur):>12}  ->  {_fmt(new):<10}{flag}")
    print("=" * 58)

    if not changes:
        print("Already at the recommended set — nothing to do.\n")
        return

    print(f"{len(changes)} field(s) will change.")
    if args.dry_run:
        print("--dry-run: no changes written.\n")
        return

    for key, (_cur, new) in changes.items():
        setattr(cfg, key, new)
    db.save_config(cfg)
    print("\nSaved to DB. RESTART the app (or open Settings and hit Save once) "
          "for the running engine to pick these up.\n")


if __name__ == "__main__":
    main()
