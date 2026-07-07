"""
Database manager for the trading system.
Handles SQLite operations for configuration, exchanges, trades, and logs.
"""

import sqlite3
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from models import TradingConfig, Exchange, Trade, SDTouchEvent

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    SQLite database manager for the trading system.

    Tables:
    - trading_config: Singleton configuration settings
    - exchanges: Exchange API credentials and status
    - trades: Trade journal
    - signal_log: Historical signals
    - std_filter_log: STD filter events
    - sd_touch_log: SD level touch events
    """

    def __init__(self, db_path: str = "trading.db"):
        self.db_path = db_path
        self._init_database()

    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _init_database(self) -> None:
        """Initialize database tables."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Trading configuration (singleton)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trading_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    asset TEXT DEFAULT 'BTC',
                    spot_symbol TEXT DEFAULT 'BTC-USDT',
                    futures_symbol TEXT DEFAULT 'BTC-USDT-SWAP',
                    entry_threshold REAL DEFAULT 2.0,
                    exit_threshold REAL DEFAULT 0.5,
                    stop_loss_threshold REAL DEFAULT 4.0,
                    profit_target_sigma_frac REAL DEFAULT 0.0,
                    profit_target_capital_pct REAL DEFAULT 0.0,
                    exit_profit_gate_usd REAL DEFAULT 0.0,
                    exit_profit_gate_pct REAL DEFAULT 0.0,
                    profit_target_usd REAL DEFAULT 0.0,
                    profit_target_min_cost_mult REAL DEFAULT 0.0,
                    max_hold_halflife_mult REAL DEFAULT 0.0,
                    max_hold_minutes REAL DEFAULT 0.0,
                    max_hold_z_progress_min REAL DEFAULT 0.5,
                    stop_loss_capital_pct REAL DEFAULT 0.0,
                    max_loss_usd REAL DEFAULT 0.0,
                    min_entry_rr_multiple REAL DEFAULT 0.0,
                    exit_signal_mode TEXT DEFAULT 'zscore',
                    lookback_period INTEGER DEFAULT 100,
                    stats_update_interval INTEGER DEFAULT 300,
                    hurst_enabled INTEGER DEFAULT 1,
                    hurst_threshold REAL DEFAULT 0.5,
                    std_filter_enabled INTEGER DEFAULT 1,
                    min_std_multiple REAL DEFAULT 1.5,
                    position_size_usd REAL DEFAULT 1000.0,
                    max_position_size_usd REAL DEFAULT 10000.0,
                    daily_max_loss_usd REAL DEFAULT 0.0,
                    spot_leverage INTEGER DEFAULT 1,
                    futures_leverage INTEGER DEFAULT 1,
                    hedge_ratio REAL DEFAULT 1.0,
                    m2m_buffer_pct REAL DEFAULT 10.0,
                    paper_trading INTEGER DEFAULT 1,
                    algo_enabled INTEGER DEFAULT 0,
                    order_execution_mode TEXT DEFAULT 'MARKET',
                    limit_order_timeout_sec INTEGER DEFAULT 30,
                    limit_order_price_offset_bps REAL DEFAULT 1.0,
                    taker_fee_bps REAL DEFAULT 5.0,
                    maker_fee_bps REAL DEFAULT 2.0,
                    estimated_costs_bps REAL DEFAULT 10.0,
                    entry_cooldown_seconds INTEGER DEFAULT 60,
                    verify_exchange_position INTEGER DEFAULT 1,
                    orphan_recovery_timeout_sec INTEGER DEFAULT 60,
                    entry_slices INTEGER DEFAULT 1,
                    entry_slice_interval_sec REAL DEFAULT 5.0,
                    min_fill_ratio REAL DEFAULT 0.95,
                    CHECK (id = 1)
                )
            """)

            # Exchanges
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS exchanges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    exchange_type TEXT NOT NULL,
                    api_key TEXT NOT NULL,
                    secret_key TEXT NOT NULL,
                    passphrase TEXT DEFAULT '',
                    is_testnet INTEGER DEFAULT 1,
                    role TEXT DEFAULT 'BOTH',
                    is_active INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'DISCONNECTED',
                    last_error TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Trades
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    position_type TEXT NOT NULL,
                    entry_time TEXT,
                    entry_spot_price REAL,
                    entry_futures_price REAL,
                    entry_spread REAL,
                    entry_zscore REAL,
                    exit_time TEXT,
                    exit_spot_price REAL,
                    exit_futures_price REAL,
                    exit_spread REAL,
                    exit_zscore REAL,
                    exit_reason TEXT,
                    quantity REAL,
                    spot_qty REAL DEFAULT 0,
                    notional_usd REAL,
                    pnl_usd REAL DEFAULT 0,
                    pnl_percent REAL DEFAULT 0,
                    pnl_gross_usd REAL DEFAULT 0,
                    fees_usd REAL DEFAULT 0,
                    capital_locked_usd REAL DEFAULT 0,
                    pnl_pct_on_capital REAL DEFAULT 0,
                    spot_order_id TEXT,
                    futures_order_id TEXT,
                    is_open INTEGER DEFAULT 1,
                    is_paper INTEGER DEFAULT 1
                )
            """)

            # Signal log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signal_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    asset TEXT,
                    signal_type TEXT,
                    zscore REAL,
                    spread REAL,
                    spread_mean REAL,
                    spread_std REAL,
                    hurst REAL,
                    hurst_ok INTEGER,
                    std_filter_ok INTEGER,
                    regime TEXT,
                    current_position TEXT
                )
            """)

            # STD filter log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS std_filter_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    asset TEXT,
                    std_value REAL,
                    cost_threshold REAL,
                    profitability_ratio REAL,
                    passed INTEGER
                )
            """)

            # SD touch log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sd_touch_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    asset TEXT,
                    sd_level REAL,
                    direction TEXT,
                    spread REAL,
                    zscore REAL,
                    spot_price REAL,
                    futures_price REAL
                )
            """)

            # Spread history for persistence/recovery
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS spread_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    asset TEXT NOT NULL,
                    spot_price REAL,
                    futures_price REAL,
                    spread REAL
                )
            """)

            # Create index for faster queries
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_spread_history_asset_time
                ON spread_history (asset, timestamp DESC)
            """)

            # Regime/edge snapshots — a long-term, low-cadence (~1/min) time
            # series of the DERIVED stats (sigma in bps, edge ratio + pass/fail,
            # z, hurst, half-life, beta drift, cost). Unlike spread_history (raw
            # + short rolling) and signal_log (only when a signal fires), this
            # captures the QUIET no-edge periods too, so we can measure how often
            # the pair is actually tradeable (sigma_bps >= required) over days.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS regime_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    asset TEXT,
                    spot_price REAL,
                    futures_price REAL,
                    beta_configured REAL,
                    beta_live REAL,
                    beta_drift_pct REAL,
                    spread REAL,
                    spread_mean REAL,
                    spread_std REAL,
                    sigma_bps REAL,
                    zscore REAL,
                    hurst REAL,
                    half_life REAL,
                    regime TEXT,
                    cost_bps REAL,
                    edge_ratio REAL,
                    edge_required REAL,
                    edge_pass INTEGER
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_regime_snapshots_asset_time
                ON regime_snapshots (asset, timestamp DESC)
            """)

            # Post-trade AI analysis log (raw JSON / text)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_analysis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id INTEGER NOT NULL,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    analysis TEXT NOT NULL,
                    model TEXT DEFAULT 'claude-sonnet-4-6'
                )
            """)

            # Structured learnings — one row per closed trade analysis
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS learnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id INTEGER NOT NULL,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    root_cause TEXT,
                    patterns TEXT,
                    recommendations TEXT,
                    confidence_score INTEGER,
                    summary TEXT
                )
            """)

            # Audit log of every auto-tuned parameter change
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS learning_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    param TEXT NOT NULL,
                    old_value REAL NOT NULL,
                    new_value REAL NOT NULL,
                    avg_confidence REAL,
                    rationale TEXT,
                    learning_ids TEXT,
                    trigger_trade_id INTEGER,
                    reverted INTEGER DEFAULT 0
                )
            """)

            # AI insights requiring human review (OBSERVATION type + manually-applicable recs)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ai_insights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    learning_id INTEGER,
                    trade_id INTEGER,
                    insight_type TEXT DEFAULT 'OBSERVATION',
                    param TEXT,
                    current_value TEXT,
                    suggested_value TEXT,
                    confidence REAL,
                    rationale TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    applied_at TEXT,
                    dismissed_at TEXT
                )
            """)

            # Untracked-close ledger: money that moved on the exchange OUTSIDE
            # a recorded trade (orphan auto-closes, leg-leak flattens). These
            # costs appear nowhere in trade P&L — this table makes them visible.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS untracked_closes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    source TEXT,
                    symbol TEXT,
                    side TEXT,
                    quantity REAL,
                    pnl_usd REAL DEFAULT 0,
                    fee_est_usd REAL DEFAULT 0,
                    note TEXT
                )
            """)

            # Insert default config if not exists
            cursor.execute("SELECT COUNT(*) FROM trading_config")
            if cursor.fetchone()[0] == 0:
                cursor.execute("INSERT INTO trading_config (id) VALUES (1)")

            # Migrations: Add new columns if they don't exist
            cursor.execute("PRAGMA table_info(trading_config)")
            existing_columns = {row[1] for row in cursor.fetchall()}

            if 'taker_fee_bps' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN taker_fee_bps REAL DEFAULT 5.0")
            if 'maker_fee_bps' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN maker_fee_bps REAL DEFAULT 2.0")
            if 'spot_leverage' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN spot_leverage INTEGER DEFAULT 1")
            if 'futures_leverage' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN futures_leverage INTEGER DEFAULT 1")
            if 'entry_cooldown_seconds' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN entry_cooldown_seconds INTEGER DEFAULT 60")
            if 'verify_exchange_position' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN verify_exchange_position INTEGER DEFAULT 1")
            if 'orphan_recovery_timeout_sec' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN orphan_recovery_timeout_sec INTEGER DEFAULT 60")
            if 'entry_execution_mode' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN entry_execution_mode TEXT DEFAULT 'LIMIT'")
            if 'exit_execution_mode' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN exit_execution_mode TEXT DEFAULT 'LIMIT'")
            else:
                # Migrate existing MARKET exits to LIMIT to reduce taker fee drain
                cursor.execute("UPDATE trading_config SET exit_execution_mode = 'LIMIT' WHERE exit_execution_mode = 'MARKET' AND id = 1")
            if 'spot_maker_fee_bps' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN spot_maker_fee_bps REAL DEFAULT 8.0")
            if 'spot_taker_fee_bps' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN spot_taker_fee_bps REAL DEFAULT 10.0")
            if 'futures_maker_fee_bps' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN futures_maker_fee_bps REAL DEFAULT 2.0")
            if 'futures_taker_fee_bps' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN futures_taker_fee_bps REAL DEFAULT 5.0")
            if 'slippage_bps' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN slippage_bps REAL DEFAULT 3.0")
            if 'auto_tune_enabled' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN auto_tune_enabled INTEGER DEFAULT 0")
            if 'telegram_enabled' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN telegram_enabled INTEGER DEFAULT 0")
            if 'telegram_bot_token' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN telegram_bot_token TEXT DEFAULT ''")
            if 'telegram_chat_id' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN telegram_chat_id TEXT DEFAULT ''")
            if 'telegram_notify_trades' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN telegram_notify_trades INTEGER DEFAULT 1")
            if 'telegram_notify_signals' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN telegram_notify_signals INTEGER DEFAULT 0")
            if 'telegram_notify_errors' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN telegram_notify_errors INTEGER DEFAULT 1")
            if 'daily_max_loss_usd' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN daily_max_loss_usd REAL DEFAULT 0.0")
            if 'hedge_ratio' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN hedge_ratio REAL DEFAULT 1.0")
            if 'm2m_buffer_pct' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN m2m_buffer_pct REAL DEFAULT 10.0")
            if 'exit_signal_mode' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN exit_signal_mode TEXT DEFAULT 'zscore'")
            if 'entry_slices' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN entry_slices INTEGER DEFAULT 1")
            if 'entry_slice_interval_sec' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN entry_slice_interval_sec REAL DEFAULT 5.0")
            if 'min_fill_ratio' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN min_fill_ratio REAL DEFAULT 0.95")
            if 'profit_target_usd' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN profit_target_usd REAL DEFAULT 0.0")
            if 'max_hold_minutes' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN max_hold_minutes REAL DEFAULT 0.0")
            if 'max_hold_z_progress_min' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN max_hold_z_progress_min REAL DEFAULT 0.5")
            if 'max_loss_usd' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN max_loss_usd REAL DEFAULT 0.0")
            if 'profit_target_sigma_frac' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN profit_target_sigma_frac REAL DEFAULT 0.0")
            if 'profit_target_capital_pct' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN profit_target_capital_pct REAL DEFAULT 0.0")
            if 'exit_profit_gate_usd' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN exit_profit_gate_usd REAL DEFAULT 0.0")
            if 'exit_profit_gate_pct' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN exit_profit_gate_pct REAL DEFAULT 0.0")
            if 'lattice_sizing_enabled' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN lattice_sizing_enabled INTEGER DEFAULT 1")
            if 'max_hold_halflife_mult' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN max_hold_halflife_mult REAL DEFAULT 0.0")
            if 'stop_loss_capital_pct' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN stop_loss_capital_pct REAL DEFAULT 0.0")
            if 'profit_target_min_cost_mult' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN profit_target_min_cost_mult REAL DEFAULT 0.0")
            if 'min_entry_rr_multiple' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN min_entry_rr_multiple REAL DEFAULT 0.0")
            # RFQ / Block trading config (atomic multi-leg execution)
            if 'rfq_notional_threshold_usd' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN rfq_notional_threshold_usd REAL DEFAULT 0.0")
            if 'rfq_anonymous' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN rfq_anonymous INTEGER DEFAULT 1")
            if 'rfq_counterparties' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN rfq_counterparties TEXT DEFAULT ''")
            if 'rfq_quote_timeout_sec' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN rfq_quote_timeout_sec REAL DEFAULT 10.0")
            if 'rfq_min_quotes' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN rfq_min_quotes INTEGER DEFAULT 1")
            if 'rfq_fallback_to_orderbook' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN rfq_fallback_to_orderbook INTEGER DEFAULT 1")
            if 'rfq_max_markup_bps' not in existing_columns:
                cursor.execute("ALTER TABLE trading_config ADD COLUMN rfq_max_markup_bps REAL DEFAULT 5.0")

            # Migrate learnings table to include richer analysis fields
            cursor.execute("PRAGMA table_info(learnings)")
            learning_cols = {row[1] for row in cursor.fetchall()}
            if 'execution_quality' not in learning_cols:
                cursor.execute("ALTER TABLE learnings ADD COLUMN execution_quality TEXT")
            if 'regime_assessment' not in learning_cols:
                cursor.execute("ALTER TABLE learnings ADD COLUMN regime_assessment TEXT")
            if 'health_score' not in learning_cols:
                cursor.execute("ALTER TABLE learnings ADD COLUMN health_score INTEGER")

            # Re-enable STD filter and lower threshold for existing DBs where it was disabled
            # min_std_multiple=1.5 was too aggressive; 1.2 with LIMIT exits is more permissive
            cursor.execute("""
                UPDATE trading_config
                SET std_filter_enabled = 1,
                    min_std_multiple = CASE WHEN min_std_multiple >= 1.5 THEN 1.2 ELSE min_std_multiple END
                WHERE id = 1 AND std_filter_enabled = 0
            """)

            # Migrate trades table for the realized-P&L breakdown (pnl_gross + fees)
            cursor.execute("PRAGMA table_info(trades)")
            trade_cols = {row[1] for row in cursor.fetchall()}
            if 'pnl_gross_usd' not in trade_cols:
                cursor.execute("ALTER TABLE trades ADD COLUMN pnl_gross_usd REAL DEFAULT 0")
            if 'fees_usd' not in trade_cols:
                cursor.execute("ALTER TABLE trades ADD COLUMN fees_usd REAL DEFAULT 0")
            if 'capital_locked_usd' not in trade_cols:
                cursor.execute("ALTER TABLE trades ADD COLUMN capital_locked_usd REAL DEFAULT 0")
            if 'pnl_pct_on_capital' not in trade_cols:
                cursor.execute("ALTER TABLE trades ADD COLUMN pnl_pct_on_capital REAL DEFAULT 0")
            if 'spot_qty' not in trade_cols:
                cursor.execute("ALTER TABLE trades ADD COLUMN spot_qty REAL DEFAULT 0")

            logger.info("Database initialized: %s", self.db_path)

    # Trading Config Methods
    def get_config(self) -> TradingConfig:
        """Get trading configuration."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trading_config WHERE id = 1")
            row = cursor.fetchone()

            if row:
                return TradingConfig(
                    id=row["id"],
                    asset=row["asset"],
                    spot_symbol=row["spot_symbol"],
                    futures_symbol=row["futures_symbol"],
                    entry_threshold=row["entry_threshold"],
                    exit_threshold=row["exit_threshold"],
                    stop_loss_threshold=row["stop_loss_threshold"],
                    exit_signal_mode=row["exit_signal_mode"] if "exit_signal_mode" in row.keys() else "zscore",
                    lookback_period=row["lookback_period"],
                    stats_update_interval=row["stats_update_interval"] if "stats_update_interval" in row.keys() else 300,
                    hurst_enabled=bool(row["hurst_enabled"]),
                    hurst_threshold=row["hurst_threshold"],
                    std_filter_enabled=bool(row["std_filter_enabled"]),
                    min_std_multiple=row["min_std_multiple"],
                    position_size_usd=row["position_size_usd"],
                    max_position_size_usd=row["max_position_size_usd"],
                    daily_max_loss_usd=row["daily_max_loss_usd"] if "daily_max_loss_usd" in row.keys() else 0.0,
                    spot_leverage=row["spot_leverage"] if "spot_leverage" in row.keys() else 1,
                    futures_leverage=row["futures_leverage"] if "futures_leverage" in row.keys() else 1,
                    hedge_ratio=row["hedge_ratio"] if "hedge_ratio" in row.keys() else 1.0,
                    m2m_buffer_pct=row["m2m_buffer_pct"] if "m2m_buffer_pct" in row.keys() else 10.0,
                    paper_trading=bool(row["paper_trading"]),
                    algo_enabled=bool(row["algo_enabled"]),
                    order_execution_mode=row["order_execution_mode"] if "order_execution_mode" in row.keys() else "MARKET",
                    entry_execution_mode=row["entry_execution_mode"] if "entry_execution_mode" in row.keys() else "LIMIT",
                    exit_execution_mode=row["exit_execution_mode"] if "exit_execution_mode" in row.keys() else "LIMIT",
                    limit_order_timeout_sec=row["limit_order_timeout_sec"] if "limit_order_timeout_sec" in row.keys() else 30,
                    limit_order_price_offset_bps=row["limit_order_price_offset_bps"] if "limit_order_price_offset_bps" in row.keys() else 1.0,
                    taker_fee_bps=row["taker_fee_bps"] if "taker_fee_bps" in row.keys() else 5.0,
                    maker_fee_bps=row["maker_fee_bps"] if "maker_fee_bps" in row.keys() else 2.0,
                    spot_maker_fee_bps=row["spot_maker_fee_bps"] if "spot_maker_fee_bps" in row.keys() else 8.0,
                    spot_taker_fee_bps=row["spot_taker_fee_bps"] if "spot_taker_fee_bps" in row.keys() else 10.0,
                    futures_maker_fee_bps=row["futures_maker_fee_bps"] if "futures_maker_fee_bps" in row.keys() else 2.0,
                    futures_taker_fee_bps=row["futures_taker_fee_bps"] if "futures_taker_fee_bps" in row.keys() else 5.0,
                    slippage_bps=row["slippage_bps"] if "slippage_bps" in row.keys() else 3.0,
                    auto_tune_enabled=bool(row["auto_tune_enabled"]) if "auto_tune_enabled" in row.keys() else False,
                    estimated_costs_bps=row["estimated_costs_bps"],
                    entry_cooldown_seconds=row["entry_cooldown_seconds"] if "entry_cooldown_seconds" in row.keys() else 60,
                    verify_exchange_position=bool(row["verify_exchange_position"]) if "verify_exchange_position" in row.keys() else True,
                    orphan_recovery_timeout_sec=row["orphan_recovery_timeout_sec"] if "orphan_recovery_timeout_sec" in row.keys() else 60,
                    telegram_enabled=bool(row["telegram_enabled"]) if "telegram_enabled" in row.keys() else False,
                    telegram_bot_token=row["telegram_bot_token"] if "telegram_bot_token" in row.keys() else "",
                    telegram_chat_id=row["telegram_chat_id"] if "telegram_chat_id" in row.keys() else "",
                    telegram_notify_trades=bool(row["telegram_notify_trades"]) if "telegram_notify_trades" in row.keys() else True,
                    telegram_notify_signals=bool(row["telegram_notify_signals"]) if "telegram_notify_signals" in row.keys() else False,
                    telegram_notify_errors=bool(row["telegram_notify_errors"]) if "telegram_notify_errors" in row.keys() else True,
                    entry_slices=row["entry_slices"] if "entry_slices" in row.keys() else 1,
                    entry_slice_interval_sec=row["entry_slice_interval_sec"] if "entry_slice_interval_sec" in row.keys() else 5.0,
                    min_fill_ratio=row["min_fill_ratio"] if "min_fill_ratio" in row.keys() else 0.95,
                    profit_target_sigma_frac=row["profit_target_sigma_frac"] if "profit_target_sigma_frac" in row.keys() else 0.0,
                    profit_target_capital_pct=row["profit_target_capital_pct"] if "profit_target_capital_pct" in row.keys() and row["profit_target_capital_pct"] is not None else 0.0,
                    exit_profit_gate_usd=row["exit_profit_gate_usd"] if "exit_profit_gate_usd" in row.keys() and row["exit_profit_gate_usd"] is not None else 0.0,
                    exit_profit_gate_pct=row["exit_profit_gate_pct"] if "exit_profit_gate_pct" in row.keys() and row["exit_profit_gate_pct"] is not None else 0.0,
                    lattice_sizing_enabled=bool(row["lattice_sizing_enabled"]) if "lattice_sizing_enabled" in row.keys() and row["lattice_sizing_enabled"] is not None else True,
                    profit_target_usd=row["profit_target_usd"] if "profit_target_usd" in row.keys() else 0.0,
                    profit_target_min_cost_mult=row["profit_target_min_cost_mult"] if "profit_target_min_cost_mult" in row.keys() else 0.0,
                    max_hold_halflife_mult=row["max_hold_halflife_mult"] if "max_hold_halflife_mult" in row.keys() else 0.0,
                    max_hold_minutes=row["max_hold_minutes"] if "max_hold_minutes" in row.keys() else 0.0,
                    max_hold_z_progress_min=row["max_hold_z_progress_min"] if "max_hold_z_progress_min" in row.keys() else 0.5,
                    stop_loss_capital_pct=row["stop_loss_capital_pct"] if "stop_loss_capital_pct" in row.keys() else 0.0,
                    max_loss_usd=row["max_loss_usd"] if "max_loss_usd" in row.keys() else 0.0,
                    min_entry_rr_multiple=row["min_entry_rr_multiple"] if "min_entry_rr_multiple" in row.keys() else 0.0,
                    rfq_notional_threshold_usd=row["rfq_notional_threshold_usd"] if "rfq_notional_threshold_usd" in row.keys() else 0.0,
                    rfq_anonymous=bool(row["rfq_anonymous"]) if "rfq_anonymous" in row.keys() else True,
                    rfq_counterparties=row["rfq_counterparties"] if "rfq_counterparties" in row.keys() else "",
                    rfq_quote_timeout_sec=row["rfq_quote_timeout_sec"] if "rfq_quote_timeout_sec" in row.keys() else 10.0,
                    rfq_min_quotes=row["rfq_min_quotes"] if "rfq_min_quotes" in row.keys() else 1,
                    rfq_fallback_to_orderbook=bool(row["rfq_fallback_to_orderbook"]) if "rfq_fallback_to_orderbook" in row.keys() else True,
                    rfq_max_markup_bps=row["rfq_max_markup_bps"] if "rfq_max_markup_bps" in row.keys() else 5.0,
                )

            return TradingConfig()

    def save_config(self, config: TradingConfig) -> None:
        """Save trading configuration."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trading_config SET
                    asset = ?,
                    spot_symbol = ?,
                    futures_symbol = ?,
                    entry_threshold = ?,
                    exit_threshold = ?,
                    stop_loss_threshold = ?,
                    exit_signal_mode = ?,
                    exit_profit_gate_usd = ?,
                    exit_profit_gate_pct = ?,
                    lattice_sizing_enabled = ?,
                    lookback_period = ?,
                    stats_update_interval = ?,
                    hurst_enabled = ?,
                    hurst_threshold = ?,
                    std_filter_enabled = ?,
                    min_std_multiple = ?,
                    position_size_usd = ?,
                    max_position_size_usd = ?,
                    daily_max_loss_usd = ?,
                    spot_leverage = ?,
                    futures_leverage = ?,
                    hedge_ratio = ?,
                    m2m_buffer_pct = ?,
                    paper_trading = ?,
                    algo_enabled = ?,
                    order_execution_mode = ?,
                    entry_execution_mode = ?,
                    exit_execution_mode = ?,
                    limit_order_timeout_sec = ?,
                    limit_order_price_offset_bps = ?,
                    taker_fee_bps = ?,
                    maker_fee_bps = ?,
                    spot_maker_fee_bps = ?,
                    spot_taker_fee_bps = ?,
                    futures_maker_fee_bps = ?,
                    futures_taker_fee_bps = ?,
                    slippage_bps = ?,
                    auto_tune_enabled = ?,
                    estimated_costs_bps = ?,
                    entry_cooldown_seconds = ?,
                    verify_exchange_position = ?,
                    orphan_recovery_timeout_sec = ?,
                    telegram_enabled = ?,
                    telegram_bot_token = ?,
                    telegram_chat_id = ?,
                    telegram_notify_trades = ?,
                    telegram_notify_signals = ?,
                    telegram_notify_errors = ?,
                    entry_slices = ?,
                    entry_slice_interval_sec = ?,
                    min_fill_ratio = ?,
                    profit_target_sigma_frac = ?,
                    profit_target_capital_pct = ?,
                    profit_target_usd = ?,
                    profit_target_min_cost_mult = ?,
                    max_hold_halflife_mult = ?,
                    max_hold_minutes = ?,
                    max_hold_z_progress_min = ?,
                    stop_loss_capital_pct = ?,
                    max_loss_usd = ?,
                    min_entry_rr_multiple = ?,
                    rfq_notional_threshold_usd = ?,
                    rfq_anonymous = ?,
                    rfq_counterparties = ?,
                    rfq_quote_timeout_sec = ?,
                    rfq_min_quotes = ?,
                    rfq_fallback_to_orderbook = ?,
                    rfq_max_markup_bps = ?
                WHERE id = 1
            """, (
                config.asset,
                config.spot_symbol,
                config.futures_symbol,
                config.entry_threshold,
                config.exit_threshold,
                config.stop_loss_threshold,
                config.exit_signal_mode,
                config.exit_profit_gate_usd,
                config.exit_profit_gate_pct,
                int(config.lattice_sizing_enabled),
                config.lookback_period,
                config.stats_update_interval,
                int(config.hurst_enabled),
                config.hurst_threshold,
                int(config.std_filter_enabled),
                config.min_std_multiple,
                config.position_size_usd,
                config.max_position_size_usd,
                config.daily_max_loss_usd,
                config.spot_leverage,
                config.futures_leverage,
                config.hedge_ratio,
                config.m2m_buffer_pct,
                int(config.paper_trading),
                int(config.algo_enabled),
                config.order_execution_mode,
                config.entry_execution_mode,
                config.exit_execution_mode,
                config.limit_order_timeout_sec,
                config.limit_order_price_offset_bps,
                config.taker_fee_bps,
                config.maker_fee_bps,
                config.spot_maker_fee_bps,
                config.spot_taker_fee_bps,
                config.futures_maker_fee_bps,
                config.futures_taker_fee_bps,
                config.slippage_bps,
                int(config.auto_tune_enabled),
                config.estimated_costs_bps,
                config.entry_cooldown_seconds,
                int(config.verify_exchange_position),
                config.orphan_recovery_timeout_sec,
                int(config.telegram_enabled),
                config.telegram_bot_token,
                config.telegram_chat_id,
                int(config.telegram_notify_trades),
                int(config.telegram_notify_signals),
                int(config.telegram_notify_errors),
                config.entry_slices,
                config.entry_slice_interval_sec,
                config.min_fill_ratio,
                config.profit_target_sigma_frac,
                config.profit_target_capital_pct,
                config.profit_target_usd,
                config.profit_target_min_cost_mult,
                config.max_hold_halflife_mult,
                config.max_hold_minutes,
                config.max_hold_z_progress_min,
                config.stop_loss_capital_pct,
                config.max_loss_usd,
                config.min_entry_rr_multiple,
                config.rfq_notional_threshold_usd,
                int(config.rfq_anonymous),
                config.rfq_counterparties,
                config.rfq_quote_timeout_sec,
                config.rfq_min_quotes,
                int(config.rfq_fallback_to_orderbook),
                config.rfq_max_markup_bps,
            ))
            logger.info("Config saved")

    # Exchange Methods
    def get_exchanges(self) -> List[Exchange]:
        """Get all exchanges."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM exchanges ORDER BY created_at DESC")
            rows = cursor.fetchall()

            return [
                Exchange(
                    id=row["id"],
                    name=row["name"],
                    exchange_type=row["exchange_type"],
                    api_key=row["api_key"],
                    secret_key=row["secret_key"],
                    passphrase=row["passphrase"],
                    is_testnet=bool(row["is_testnet"]),
                    role=row["role"],
                    is_active=bool(row["is_active"]),
                    status=row["status"],
                    last_error=row["last_error"],
                    created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                )
                for row in rows
            ]

    def get_exchange(self, exchange_id: int) -> Optional[Exchange]:
        """Get exchange by ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM exchanges WHERE id = ?", (exchange_id,))
            row = cursor.fetchone()

            if row:
                return Exchange(
                    id=row["id"],
                    name=row["name"],
                    exchange_type=row["exchange_type"],
                    api_key=row["api_key"],
                    secret_key=row["secret_key"],
                    passphrase=row["passphrase"],
                    is_testnet=bool(row["is_testnet"]),
                    role=row["role"],
                    is_active=bool(row["is_active"]),
                    status=row["status"],
                    last_error=row["last_error"],
                    created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                )

            return None

    def save_exchange(self, exchange: Exchange) -> int:
        """Save or update exchange."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if exchange.id:
                cursor.execute("""
                    UPDATE exchanges SET
                        name = ?,
                        exchange_type = ?,
                        api_key = ?,
                        secret_key = ?,
                        passphrase = ?,
                        is_testnet = ?,
                        role = ?,
                        is_active = ?,
                        status = ?,
                        last_error = ?
                    WHERE id = ?
                """, (
                    exchange.name,
                    exchange.exchange_type,
                    exchange.api_key,
                    exchange.secret_key,
                    exchange.passphrase,
                    int(exchange.is_testnet),
                    exchange.role,
                    int(exchange.is_active),
                    exchange.status,
                    exchange.last_error,
                    exchange.id,
                ))
                return exchange.id
            else:
                cursor.execute("""
                    INSERT INTO exchanges (
                        name, exchange_type, api_key, secret_key, passphrase,
                        is_testnet, role, is_active, status, last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    exchange.name,
                    exchange.exchange_type,
                    exchange.api_key,
                    exchange.secret_key,
                    exchange.passphrase,
                    int(exchange.is_testnet),
                    exchange.role,
                    int(exchange.is_active),
                    exchange.status,
                    exchange.last_error,
                ))
                return cursor.lastrowid

    def delete_exchange(self, exchange_id: int) -> None:
        """Delete exchange."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM exchanges WHERE id = ?", (exchange_id,))
            logger.info("Exchange deleted: %d", exchange_id)

    def update_exchange_status(self, exchange_id: int, status: str, error: str = "") -> None:
        """Update exchange connection status."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE exchanges SET status = ?, last_error = ?
                WHERE id = ?
            """, (status, error, exchange_id))

    def set_active_exchanges(self, spot_id: Optional[int], futures_id: Optional[int]) -> None:
        """Set active exchanges for trading."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Deactivate all
            cursor.execute("UPDATE exchanges SET is_active = 0")

            # Activate selected
            if spot_id:
                cursor.execute(
                    "UPDATE exchanges SET is_active = 1, role = 'SPOT' WHERE id = ?",
                    (spot_id,)
                )
            if futures_id:
                cursor.execute(
                    "UPDATE exchanges SET is_active = 1, role = 'FUTURES' WHERE id = ?",
                    (futures_id,)
                )

            logger.info("Active exchanges set: spot=%s, futures=%s", spot_id, futures_id)

    # Trade Methods
    def save_trade(self, trade: Trade) -> int:
        """Save or update trade."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if trade.id:
                cursor.execute("""
                    UPDATE trades SET
                        exit_time = ?,
                        exit_spot_price = ?,
                        exit_futures_price = ?,
                        exit_spread = ?,
                        exit_zscore = ?,
                        exit_reason = ?,
                        notional_usd = ?,
                        pnl_usd = ?,
                        pnl_percent = ?,
                        pnl_gross_usd = ?,
                        fees_usd = ?,
                        capital_locked_usd = ?,
                        pnl_pct_on_capital = ?,
                        is_open = ?
                    WHERE id = ?
                """, (
                    trade.exit_time.isoformat() if trade.exit_time else None,
                    trade.exit_spot_price,
                    trade.exit_futures_price,
                    trade.exit_spread,
                    trade.exit_zscore,
                    trade.exit_reason,
                    trade.notional_usd,
                    trade.pnl_usd,
                    trade.pnl_percent,
                    trade.pnl_gross_usd,
                    trade.fees_usd,
                    trade.capital_locked_usd,
                    trade.pnl_pct_on_capital,
                    int(trade.is_open),
                    trade.id,
                ))
                return trade.id
            else:
                # Guard against duplicate inserts (e.g., crash-and-restart with same order IDs)
                if trade.spot_order_id or trade.futures_order_id:
                    cursor.execute(
                        "SELECT id FROM trades WHERE spot_order_id = ? AND futures_order_id = ?",
                        (trade.spot_order_id, trade.futures_order_id),
                    )
                    existing = cursor.fetchone()
                    if existing:
                        logger.warning(
                            "Duplicate trade insert blocked: spot_order_id=%s futures_order_id=%s already exists as id=%d",
                            trade.spot_order_id, trade.futures_order_id, existing[0],
                        )
                        return existing[0]

                cursor.execute("""
                    INSERT INTO trades (
                        asset, position_type, entry_time, entry_spot_price,
                        entry_futures_price, entry_spread, entry_zscore,
                        quantity, spot_qty, notional_usd, spot_order_id, futures_order_id,
                        is_open, is_paper
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade.asset,
                    trade.position_type,
                    trade.entry_time.isoformat() if trade.entry_time else None,
                    trade.entry_spot_price,
                    trade.entry_futures_price,
                    trade.entry_spread,
                    trade.entry_zscore,
                    trade.quantity,
                    trade.spot_qty,
                    trade.notional_usd,
                    trade.spot_order_id,
                    trade.futures_order_id,
                    int(trade.is_open),
                    int(trade.is_paper),
                ))
                return cursor.lastrowid

    def save_untracked_close(self, *, source: str, symbol: str, side: str,
                             quantity: float, pnl_usd: float,
                             fee_est_usd: float = 0.0, note: str = "") -> None:
        """Record a close that happened outside a recorded trade (orphan
        auto-close, leg-leak flatten) so cleanup costs are visible."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "INSERT INTO untracked_closes (source, symbol, side, quantity, "
                "pnl_usd, fee_est_usd, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, symbol, side, quantity, pnl_usd, fee_est_usd, note),
            )

    def get_untracked_closes(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM untracked_closes ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in cursor.fetchall()]

    def get_untracked_totals_today(self) -> Dict[str, Any]:
        """Today's (UTC) untracked-close count and P&L, net of estimated fees."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(pnl_usd), 0) AS pnl, "
                "COALESCE(SUM(fee_est_usd), 0) AS fees FROM untracked_closes "
                "WHERE date(timestamp) = date('now')",
            )
            row = cursor.fetchone()
            n, pnl, fees = row["n"], row["pnl"], row["fees"]
            return {"count": n, "pnl_usd": round(pnl, 4),
                    "fee_est_usd": round(fees, 4), "net_usd": round(pnl - fees, 4)}

    def get_trades(self, limit: int = 100, open_only: bool = False) -> List[Trade]:
        """Get trades."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = "SELECT * FROM trades"
            if open_only:
                query += " WHERE is_open = 1"
            query += " ORDER BY entry_time DESC LIMIT ?"

            cursor.execute(query, (limit,))
            rows = cursor.fetchall()

            return [self._row_to_trade(row) for row in rows]

    def get_open_trade(self) -> Optional[Trade]:
        """Get current open trade."""
        trades = self.get_trades(limit=1, open_only=True)
        return trades[0] if trades else None

    def _row_to_trade(self, row) -> Trade:
        """Convert database row to Trade object."""
        return Trade(
            id=row["id"],
            asset=row["asset"],
            position_type=row["position_type"],
            entry_time=datetime.fromisoformat(row["entry_time"]) if row["entry_time"] else None,
            entry_spot_price=row["entry_spot_price"] or 0,
            entry_futures_price=row["entry_futures_price"] or 0,
            entry_spread=row["entry_spread"] or 0,
            entry_zscore=row["entry_zscore"] or 0,
            exit_time=datetime.fromisoformat(row["exit_time"]) if row["exit_time"] else None,
            exit_spot_price=row["exit_spot_price"] or 0,
            exit_futures_price=row["exit_futures_price"] or 0,
            exit_spread=row["exit_spread"] or 0,
            exit_zscore=row["exit_zscore"] or 0,
            exit_reason=row["exit_reason"] or "",
            quantity=row["quantity"] or 0,
            spot_qty=(row["spot_qty"] or 0) if "spot_qty" in row.keys() else 0,
            notional_usd=row["notional_usd"] or 0,
            pnl_usd=row["pnl_usd"] or 0,
            pnl_percent=row["pnl_percent"] or 0,
            pnl_gross_usd=row["pnl_gross_usd"] or 0,
            fees_usd=row["fees_usd"] or 0,
            capital_locked_usd=row["capital_locked_usd"] or 0,
            pnl_pct_on_capital=row["pnl_pct_on_capital"] or 0,
            spot_order_id=row["spot_order_id"] or "",
            futures_order_id=row["futures_order_id"] or "",
            is_open=bool(row["is_open"]),
            is_paper=bool(row["is_paper"]),
        )

    # Logging Methods
    def log_signal(self, signal_data: Dict[str, Any]) -> None:
        """Log a signal event."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO signal_log (
                    asset, signal_type, zscore, spread, spread_mean, spread_std,
                    hurst, hurst_ok, std_filter_ok, regime, current_position
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_data.get("asset"),
                signal_data.get("signal_type"),
                signal_data.get("zscore"),
                signal_data.get("spread"),
                signal_data.get("spread_mean"),
                signal_data.get("spread_std"),
                signal_data.get("hurst"),
                int(signal_data.get("hurst_ok", True)),
                int(signal_data.get("std_filter_ok", True)),
                signal_data.get("regime"),
                signal_data.get("current_position"),
            ))

    def log_std_filter(
        self,
        asset: str,
        std_value: float,
        cost_threshold: float,
        profitability_ratio: float,
        passed: bool,
    ) -> None:
        """Log STD filter event."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO std_filter_log (
                    asset, std_value, cost_threshold, profitability_ratio, passed
                ) VALUES (?, ?, ?, ?, ?)
            """, (asset, std_value, cost_threshold, profitability_ratio, int(passed)))

    def save_trade_analysis(self, trade_id: int, analysis: str, model: str = "claude-opus-4-6") -> None:
        """Save post-trade AI analysis."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trade_analysis (trade_id, analysis, model)
                VALUES (?, ?, ?)
            """, (trade_id, analysis, model))

    def get_trade_analysis(self, trade_id: int) -> Optional[Dict[str, Any]]:
        """Get AI analysis for a specific trade."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM trade_analysis WHERE trade_id = ? ORDER BY timestamp DESC LIMIT 1",
                (trade_id,)
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None

    def save_learning(self, trade_id: int, analysis: Dict[str, Any]) -> int:
        """Persist a structured learning from post-trade analysis."""
        import json as _json
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO learnings
                    (trade_id, root_cause, patterns, execution_quality,
                     regime_assessment, recommendations, health_score,
                     confidence_score, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id,
                analysis.get("verdict") or analysis.get("what_happened") or analysis.get("root_cause", ""),
                analysis.get("why") or analysis.get("patterns", ""),
                analysis.get("what_could_be_better") or analysis.get("execution_quality", ""),
                analysis.get("regime_assessment", ""),
                _json.dumps(analysis.get("recommendations", [])),
                analysis.get("health_score"),
                analysis.get("confidence_score", 0),
                analysis.get("summary", ""),
            ))
            return cursor.lastrowid

    def save_ai_insight(
        self,
        learning_id: Optional[int],
        trade_id: int,
        insight_type: str,
        param: str,
        current_value: str,
        suggested_value: str,
        confidence: float,
        rationale: str,
    ) -> None:
        """Store an AI observation/insight for human review. Deduplicates by param+status."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Skip if the same param already has a pending insight
            cursor.execute(
                "SELECT id FROM ai_insights WHERE param = ? AND status = 'pending' LIMIT 1",
                (param,),
            )
            if cursor.fetchone():
                return
            cursor.execute("""
                INSERT INTO ai_insights
                    (learning_id, trade_id, insight_type, param,
                     current_value, suggested_value, confidence, rationale)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                learning_id, trade_id, insight_type, param,
                current_value, suggested_value, confidence, rationale,
            ))

    def get_pending_insights(self, limit: int = 30) -> List[Dict[str, Any]]:
        """Return pending AI insights (newest first)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM ai_insights WHERE status = 'pending' ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_insight_status(self, insight_id: int, status: str) -> bool:
        """Mark an insight as 'applied' or 'dismissed'. Returns True if found."""
        # Use a whitelist map to avoid any f-string in SQL (injection-safe pattern)
        _col_map = {"applied": "applied_at", "dismissed": "dismissed_at"}
        ts_col = _col_map.get(status, "dismissed_at")
        sql = (
            "UPDATE ai_insights SET status = ?, applied_at = ? WHERE id = ?"
            if ts_col == "applied_at" else
            "UPDATE ai_insights SET status = ?, dismissed_at = ? WHERE id = ?"
        )
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (status, datetime.utcnow().isoformat(), insight_id))
            return cursor.rowcount > 0

    def get_recent_learnings(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the most recent learnings (newest first)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM learnings ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def save_learning_log(
        self,
        param: str,
        old_value: float,
        new_value: float,
        avg_confidence: float,
        rationale: str,
        learning_ids: List[int],
        trigger_trade_id: int,
    ) -> None:
        """Log an auto-tuned parameter change."""
        import json as _json
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO learning_log
                    (param, old_value, new_value, avg_confidence, rationale,
                     learning_ids, trigger_trade_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                param, old_value, new_value, avg_confidence, rationale,
                _json.dumps(learning_ids), trigger_trade_id,
            ))

    def get_learning_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the auto-tune change history (newest first)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM learning_log ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def log_sd_touch(self, event: SDTouchEvent) -> None:
        """Log SD touch event."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO sd_touch_log (
                    asset, sd_level, direction, spread, zscore, spot_price, futures_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                event.asset,
                event.sd_level,
                event.direction,
                event.spread,
                event.zscore,
                event.spot_price,
                event.futures_price,
            ))

    def get_sd_touches(self, asset: Optional[str] = None, limit: int = 1000) -> List[SDTouchEvent]:
        """Get SD touch events."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = "SELECT * FROM sd_touch_log"
            params = []

            if asset:
                query += " WHERE asset = ?"
                params.append(asset)

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            cursor.execute(query, params)
            rows = cursor.fetchall()

            return [
                SDTouchEvent(
                    id=row["id"],
                    asset=row["asset"],
                    timestamp=datetime.fromisoformat(row["timestamp"]) if row["timestamp"] else None,
                    sd_level=row["sd_level"],
                    direction=row["direction"],
                    spread=row["spread"],
                    zscore=row["zscore"],
                    spot_price=row["spot_price"],
                    futures_price=row["futures_price"],
                )
                for row in rows
            ]

    def get_trade_statistics(self) -> Dict[str, Any]:
        """Get trade statistics for analysis."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Real (non-paper) closed trades only
            cursor.execute("SELECT COUNT(*) FROM trades WHERE is_open = 0 AND is_paper = 0")
            total_trades = cursor.fetchone()[0]

            # Winning trades
            cursor.execute("SELECT COUNT(*) FROM trades WHERE is_open = 0 AND is_paper = 0 AND pnl_usd > 0")
            winning_trades = cursor.fetchone()[0]

            # Total P&L
            cursor.execute("SELECT SUM(pnl_usd) FROM trades WHERE is_open = 0 AND is_paper = 0")
            total_pnl = cursor.fetchone()[0] or 0

            # Average P&L
            cursor.execute("SELECT AVG(pnl_usd) FROM trades WHERE is_open = 0 AND is_paper = 0")
            avg_pnl = cursor.fetchone()[0] or 0

            # Win rate
            win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

            return {
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "losing_trades": total_trades - winning_trades,
                "win_rate": round(win_rate, 2),
                "total_pnl": round(total_pnl, 2),
                "avg_pnl": round(avg_pnl, 2),
            }

    # Spread History Methods (for persistence/recovery)
    def save_spread(
        self,
        asset: str,
        spot_price: float,
        futures_price: float,
        spread: float,
    ) -> None:
        """Save a spread data point."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO spread_history (asset, spot_price, futures_price, spread)
                VALUES (?, ?, ?, ?)
            """, (asset, spot_price, futures_price, spread))

    # ── Regime / edge snapshots (long-term tradeability time series) ──────────
    _REGIME_COLS = (
        "asset", "spot_price", "futures_price", "beta_configured", "beta_live",
        "beta_drift_pct", "spread", "spread_mean", "spread_std", "sigma_bps",
        "zscore", "hurst", "half_life", "regime", "cost_bps", "edge_ratio",
        "edge_required", "edge_pass",
    )

    def save_regime_snapshot(self, data: Dict[str, Any]) -> None:
        """Append one regime/edge snapshot. `data` keys mirror _REGIME_COLS;
        missing keys are stored NULL. Append-only, fail-safe for the caller."""
        cols = self._REGIME_COLS
        placeholders = ", ".join("?" for _ in cols)
        with self._get_connection() as conn:
            conn.cursor().execute(
                f"INSERT INTO regime_snapshots ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(data.get(c) for c in cols),
            )

    def get_regime_snapshots(
        self, asset: str, since_iso: Optional[str] = None, limit: int = 100000
    ) -> List[Dict[str, Any]]:
        """Return regime snapshots for an asset, oldest first. Optionally only
        rows at/after `since_iso` (an ISO timestamp string)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if since_iso:
                cursor.execute(
                    "SELECT * FROM regime_snapshots WHERE asset = ? AND timestamp >= ? "
                    "ORDER BY timestamp DESC LIMIT ?", (asset, since_iso, limit))
            else:
                cursor.execute(
                    "SELECT * FROM regime_snapshots WHERE asset = ? "
                    "ORDER BY timestamp DESC LIMIT ?", (asset, limit))
            return [dict(r) for r in reversed(cursor.fetchall())]

    def cleanup_old_regime_snapshots(self, keep_days: int = 120) -> int:
        """Delete snapshots older than keep_days. Returns rows removed."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM regime_snapshots "
                "WHERE timestamp < datetime('now', ?)", (f"-{int(keep_days)} days",))
            return cursor.rowcount

    def get_spread_history(self, asset: str, limit: int = 500) -> List[Dict[str, Any]]:
        """
        Get spread history for an asset (for recovery after reconnection).

        Returns list of dicts with timestamp, spot_price, futures_price, spread.
        Results are ordered oldest first for correct loading order.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT timestamp, spot_price, futures_price, spread
                FROM spread_history
                WHERE asset = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (asset, limit))
            rows = cursor.fetchall()

            # Reverse to get oldest first (for correct loading order)
            return [
                {
                    'timestamp': row['timestamp'],
                    'spot_price': row['spot_price'],
                    'futures_price': row['futures_price'],
                    'spread': row['spread'],
                }
                for row in reversed(rows)
            ]

    def cleanup_old_spread_history(self, asset: str, keep_count: int = 1000) -> None:
        """Remove old spread history entries, keeping only the most recent."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM spread_history
                WHERE asset = ? AND id NOT IN (
                    SELECT id FROM spread_history
                    WHERE asset = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
            """, (asset, asset, keep_count))
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info("Cleaned up %d old spread history entries for %s", deleted, asset)

    # Reset/Clear Methods
    def clear_trades(self, asset: Optional[str] = None) -> int:
        """Clear all trades (or for a specific asset). Returns count deleted."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if asset:
                cursor.execute("DELETE FROM trades WHERE asset = ?", (asset,))
            else:
                cursor.execute("DELETE FROM trades")
            deleted = cursor.rowcount
            logger.info("Cleared %d trades%s", deleted, f" for {asset}" if asset else "")
            return deleted

    def clear_sd_touches(self, asset: Optional[str] = None) -> int:
        """Clear all SD touch events (or for a specific asset). Returns count deleted."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if asset:
                cursor.execute("DELETE FROM sd_touch_log WHERE asset = ?", (asset,))
            else:
                cursor.execute("DELETE FROM sd_touch_log")
            deleted = cursor.rowcount
            logger.info("Cleared %d SD touches%s", deleted, f" for {asset}" if asset else "")
            return deleted

    def clear_signal_log(self, asset: Optional[str] = None) -> int:
        """Clear signal log (or for a specific asset). Returns count deleted."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if asset:
                cursor.execute("DELETE FROM signal_log WHERE asset = ?", (asset,))
            else:
                cursor.execute("DELETE FROM signal_log")
            deleted = cursor.rowcount
            logger.info("Cleared %d signal log entries%s", deleted, f" for {asset}" if asset else "")
            return deleted

    def clear_spread_history(self, asset: Optional[str] = None) -> int:
        """Clear spread history (or for a specific asset). Returns count deleted."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if asset:
                cursor.execute("DELETE FROM spread_history WHERE asset = ?", (asset,))
            else:
                cursor.execute("DELETE FROM spread_history")
            deleted = cursor.rowcount
            logger.info("Cleared %d spread history entries%s", deleted, f" for {asset}" if asset else "")
            return deleted

    def delete_trade(self, trade_id: int) -> bool:
        """Delete a specific trade by ID. Returns True if deleted."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
            deleted = cursor.rowcount > 0
            if deleted:
                logger.info("Deleted trade ID %d", trade_id)
            return deleted

    def close_trade(self, trade_id: int, exit_reason: str = "MANUAL") -> bool:
        """
        Manually close an open trade.
        Returns True if closed, False if not found or already closed.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Check if trade exists and is open
            cursor.execute("SELECT * FROM trades WHERE id = ? AND is_open = 1", (trade_id,))
            row = cursor.fetchone()
            if not row:
                return False

            # Update to closed
            cursor.execute("""
                UPDATE trades SET
                    exit_time = ?,
                    exit_reason = ?,
                    is_open = 0
                WHERE id = ?
            """, (datetime.utcnow().isoformat(), exit_reason, trade_id))
            logger.info("Manually closed trade ID %d", trade_id)
            return True
