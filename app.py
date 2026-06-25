"""
Flask web application for the Crypto Statistical Arbitrage Trading System.
"""

import os
import sys
import time
import signal
import asyncio
import concurrent.futures
import logging
import atexit
from threading import Thread
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from flask import Flask, render_template, jsonify, request, redirect, url_for
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

from models import TradingConfig, Exchange, Trade, MarketTick, Signal, CRYPTO_ASSETS
from core.signals import SignalGenerator
from core.trading_engine import TradingEngine
from core.post_trade_analyzer import PostTradeAnalyzer
from core.auto_tuner import AutoTuner
from core.telegram_bot import get_notifier
from database.manager import DatabaseManager
from adapters import OKXAdapter, BinanceAdapter, BybitAdapter, OKXWebSocketManager, OKXWebSocketAdapter
from adapters.base import is_derivative

# Load environment variables
load_dotenv()

# Configure logging — console + rotating daily file so AI monitor can tail logs
import os as _os
from logging.handlers import TimedRotatingFileHandler as _TRFH
_LOG_DIR = _os.path.join(_os.path.dirname(__file__), "logs")
_os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FMT = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
_file_handler = _TRFH(
    _os.path.join(_LOG_DIR, "trading.log"),
    when="midnight", backupCount=7, encoding="utf-8",
)
_file_handler.suffix = "%Y%m%d"
_file_handler.setFormatter(_LOG_FMT)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger().addHandler(_file_handler)
logger = logging.getLogger(__name__)

# Suppress noisy HTTP request logs - use ERROR to hide all routine requests
logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.getLogger('engineio').setLevel(logging.ERROR)
logging.getLogger('socketio').setLevel(logging.ERROR)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'crypto-arb-secret-key')

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Initialize database
db = DatabaseManager(os.getenv('DATABASE_PATH', 'trading.db'))

# Auto-tuner: applies Claude's recommendations when auto_tune_enabled=True
# engine is not yet initialised here — set after engine creation below
auto_tuner = AutoTuner(db, engine=None, socketio=socketio)

# Post-trade AI analyzer (fires after every real closed trade)
post_trade_analyzer = PostTradeAnalyzer(db, socketio, auto_tuner=auto_tuner)

# Initialize trading engine
config = db.get_config()
engine = TradingEngine(config)

# Persist any self-corrected config values (e.g. leverage capped by exchange)
engine.on_config_corrected = lambda cfg: db.save_config(cfg)

# Async event loop for trading engine
loop: Optional[asyncio.AbstractEventLoop] = None
engine_thread: Optional[Thread] = None
ws_manager: Optional[OKXWebSocketManager] = None
shutdown_in_progress = False
_execution_backend: str = "rest"  # updated by start_engine_loop; "rest" or "websocket"


def run_async_loop(loop: asyncio.AbstractEventLoop):
    """Run the async event loop in a separate thread."""
    logger.info("Async event loop thread starting...")
    asyncio.set_event_loop(loop)
    logger.info("Async event loop running")
    loop.run_forever()


def _backfill_capital_metrics() -> None:
    """Recompute notional/margin/capital metrics for closed trades using the
    trade's stored entry prices, quantity, and current β + leverage config.
    Exact when config hasn't changed; approximate otherwise.

    Three things get re-derived per closed trade:

    1. notional_usd → total (Leg A + Leg B). Was previously only Leg A
       (position_size_usd), under-reporting by ~2× on dollar-neutral pairs.
    2. margin_usd → total margin across both legs (was futures-only).
    3. capital_locked_usd + pnl_pct_on_capital (the locked-capital % metric).

    pnl_percent is also recomputed against the new total notional so the
    Trade Journal % column is internally consistent.
    """
    try:
        leg_a_deriv = is_derivative(config.spot_symbol)
        leg_b_deriv = is_derivative(config.futures_symbol)
        leg_a_lev = max(config.spot_leverage    if leg_a_deriv else 1, 1)
        leg_b_lev = max(config.futures_leverage if leg_b_deriv else 1, 1)
        beta = max(getattr(config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
        buffer_pct = getattr(config, 'm2m_buffer_pct', 0.0) or 0.0
        buffer_mult = 1 + buffer_pct / 100.0

        n = 0
        for trade in db.get_trades(limit=10000, open_only=False):
            if trade.is_open:
                continue
            if not (trade.entry_spot_price and trade.entry_futures_price and trade.quantity):
                continue

            # Recompute leg notionals from the stored entry fills.
            leg_a_notional = trade.entry_spot_price * trade.quantity * beta
            leg_b_notional = trade.entry_futures_price * trade.quantity
            new_total_notional = leg_a_notional + leg_b_notional

            # Margin totals across both legs.
            margin_a = leg_a_notional / leg_a_lev
            margin_b = leg_b_notional / leg_b_lev
            new_total_margin = margin_a + margin_b

            # Capital metric (margin + M2M buffer).
            capital = new_total_margin * buffer_mult

            # Skip rows where nothing would change (e.g. already back-filled).
            old_notional_close = abs((trade.notional_usd or 0) - new_total_notional) < 0.01
            old_margin_close   = abs((trade.margin_usd or 0)   - new_total_margin)   < 0.01
            old_capital_close  = abs((trade.capital_locked_usd or 0) - capital)      < 0.01
            if old_notional_close and old_margin_close and old_capital_close:
                continue

            trade.notional_usd        = round(new_total_notional, 2)
            trade.margin_usd          = round(new_total_margin, 2)
            trade.capital_locked_usd  = round(capital, 2)
            if new_total_notional > 0:
                trade.pnl_percent = (trade.pnl_usd / new_total_notional) * 100
            if capital > 0:
                trade.pnl_pct_on_capital = (trade.pnl_usd / capital) * 100
            db.save_trade(trade)
            n += 1
        if n:
            logger.info(
                "Back-filled notional/margin/capital metrics on %d closed trade(s)", n
            )
    except Exception as e:
        logger.warning("Trade metric back-fill skipped: %s", e)


def _get_balance_for_telegram() -> Dict[str, Any]:
    """Fetch account balance data for Telegram /balance command."""
    try:
        adapter = engine.spot_adapter or engine.futures_adapter
        if not adapter or not loop:
            return {}

        async def _fetch():
            if hasattr(adapter, 'get_account_info'):
                return await adapter.get_account_info()
            return None

        future = asyncio.run_coroutine_threadsafe(_fetch(), loop)
        account = future.result(timeout=10)
        if account:
            stats = db.get_trade_statistics()
            return {
                'connected': True,
                'exchange': getattr(adapter, 'exchange_type', 'OKX').upper(),
                'is_demo': getattr(adapter, 'is_testnet', True),
                'total_equity': account.total_equity,
                'available_margin': account.available_margin,
                'margin_used': account.margin_used,
                'margin_ratio': account.margin_ratio,
                'unrealized_pnl': account.unrealized_pnl,
                'margin_health': (
                    'SAFE' if account.margin_ratio > 500
                    else 'WARNING' if account.margin_ratio > 150
                    else 'DANGER' if account.margin_ratio > 0
                    else 'N/A'
                ),
                'daily_pnl': stats.get('daily_pnl', 0) if stats else 0,
            }
    except Exception as e:
        logger.warning("Error fetching balance for Telegram: %s", e)
    return {}


def _build_trade_adapters(api_key: str, secret_key: str, passphrase: str, is_demo: bool):
    """
    Create the (spot, futures) execution adapters per the EXCHANGE_BACKEND flag.

    EXCHANGE_BACKEND=websocket selects OKXWebSocketAdapter (private-channel WS,
    sub-500ms order placement) and connects both legs on the engine loop. If the
    WS connect fails, falls back to the REST adapters so the bot still runs.
    The default (rest) preserves the original REST behaviour exactly.

    Returns (spot_adapter, futures_adapter, backend_label).
    """
    backend = os.getenv('EXCHANGE_BACKEND', 'rest').strip().lower()

    if backend == 'websocket':
        spot = OKXWebSocketAdapter(
            api_key=api_key, secret_key=secret_key, passphrase=passphrase,
            is_testnet=is_demo, spot_leverage=config.spot_leverage,
        )
        futures = OKXWebSocketAdapter(
            api_key=api_key, secret_key=secret_key, passphrase=passphrase,
            is_testnet=is_demo,
        )
        try:
            ws_ok = True
            for label, ad in (("spot", spot), ("futures", futures)):
                connected = asyncio.run_coroutine_threadsafe(
                    ad.connect(), loop).result(timeout=20)
                if not connected:
                    logger.error("WS %s adapter connect returned False: %s", label, ad.last_error)
                    ws_ok = False
                    break
            if ws_ok:
                logger.info("WebSocket execution adapters connected (demo=%s)", is_demo)
                return spot, futures, "websocket"
        except Exception as e:
            logger.error("WS adapter connect failed: %s", e)

        # Connect failed — tear down any partial WS and fall back to REST.
        for ad in (spot, futures):
            try:
                asyncio.run_coroutine_threadsafe(ad.disconnect(), loop).result(timeout=5)
            except Exception:
                pass
        logger.error("EXCHANGE_BACKEND=websocket connect failed — falling back to REST adapters")

    spot = OKXAdapter(
        api_key=api_key, secret_key=secret_key, passphrase=passphrase,
        is_testnet=is_demo, spot_leverage=config.spot_leverage,
    )
    futures = OKXAdapter(
        api_key=api_key, secret_key=secret_key, passphrase=passphrase,
        is_testnet=is_demo,
    )
    return spot, futures, "rest"


def start_engine_loop():
    """Start the trading engine in a background thread."""
    global loop, engine_thread, ws_manager

    if loop is None:
        loop = asyncio.new_event_loop()
        engine_thread = Thread(target=run_async_loop, args=(loop,), daemon=True)
        engine_thread.start()
        # Wait for the event loop to actually start running
        time.sleep(0.1)
        logger.info("Event loop thread started, scheduling engine.start()")

    # Set up callbacks
    engine.on_tick = on_tick_callback
    engine.on_signal = on_signal_callback
    engine.on_trade = on_trade_callback
    engine.on_error = on_error_callback

    # Give the auto-tuner a reference to the live engine so it can update config in-process
    auto_tuner.engine = engine

    # Set up SD touch callback on signal generator
    engine.signal_generator.on_sd_touch = on_sd_touch_callback

    # Configure Telegram notifier with current config and wire up data callbacks
    _telegram = get_notifier()
    _telegram.update_config(config)
    _telegram.get_status_cb = lambda: engine.get_status()
    _telegram.get_trades_cb = lambda: [t.to_dict() for t in db.get_trades(limit=20)]
    _telegram.get_balance_cb = _get_balance_for_telegram
    _telegram.get_config_cb = lambda: engine.config.to_dict()
    _telegram.optimize_cb = lambda: engine.signal_generator.optimize_parameters()

    def _toggle_algo_from_telegram(enabled: bool) -> bool:
        engine.toggle_algo(enabled)
        # Persist so the new state survives restarts.
        try:
            config.algo_enabled = enabled
            db.save_config(config)
        except Exception as e:
            logger.warning("Persisting algo toggle failed: %s", e)
        return engine.state.algo_enabled
    _telegram.toggle_algo_cb = _toggle_algo_from_telegram
    # Start command polling in a background daemon thread
    _telegram.start_polling()

    # One-time back-fill: closed trades from before the capital-locked column
    # existed have capital_locked_usd = 0 (default). Recompute from each
    # trade's stored entry prices + quantity using the CURRENT leverage and
    # M2M buffer config. This is an approximation when leverage has changed
    # mid-history, but covers the common case.
    _backfill_capital_metrics()

    # Load spread history from database for recovery.
    # Pass the raw spot/futures prices so the signal generator recomputes every
    # spread under the CURRENT hedge ratio — the persisted ``spread`` column
    # was written with whatever β was set at save time and can't be trusted.
    spread_history = db.get_spread_history(config.asset, limit=config.lookback_period)
    if spread_history:
        spreads = [h['spread'] for h in spread_history]
        spot_prices = [h['spot_price'] for h in spread_history]
        futures_prices = [h['futures_price'] for h in spread_history]
        engine.signal_generator.load_spread_history(
            spreads, spot_prices=spot_prices, futures_prices=futures_prices,
        )
        logger.info("Loaded %d spread values from database", len(spreads))

    # Cleanup old spread history to prevent database bloat
    # Keep at least 2x lookback period to ensure sufficient data after restart
    keep_count = max(config.lookback_period * 2, 2000)
    db.cleanup_old_spread_history(config.asset, keep_count=keep_count)

    # Recover open position from database (real trades only — paper positions
    # are not carried over after a restart since they have no real exchange state)
    open_trades = db.get_trades(limit=1, open_only=True)
    if open_trades:
        open_trade = open_trades[0]
        if open_trade.is_paper:
            logger.info("Ignoring open paper trade in recovery (id=%s)", open_trade.id)
        elif open_trade.asset == config.asset:
            engine.open_trade = open_trade
            engine.state.current_position = open_trade.position_type
            engine.signal_generator.set_position(open_trade.position_type)
            logger.info("Recovered open %s position from database (trade_id=%d, entry_zscore=%.2f)",
                       open_trade.position_type, open_trade.id, open_trade.entry_zscore)
        else:
            logger.warning("Open trade exists for different asset (%s vs %s), not recovering",
                          open_trade.asset, config.asset)

    # Set up WebSocket streaming if enabled
    use_websocket = os.getenv('USE_WEBSOCKET', 'true').lower() == 'true'
    if use_websocket:
        is_demo = os.getenv('OKX_DEMO_MODE', 'false').lower() == 'true'
        ws_manager = OKXWebSocketManager(is_demo=is_demo)
        engine.set_websocket_manager(ws_manager)
        logger.debug("WebSocket streaming enabled (demo=%s)", is_demo)

    # Initialize REST adapters for account info and order execution
    # This allows us to use WebSocket for fast price updates and REST for account data + orders
    api_key = os.getenv('OKX_API_KEY', '')
    secret_key = os.getenv('OKX_SECRET_KEY', '')
    passphrase = os.getenv('OKX_PASSPHRASE', '')
    is_demo = os.getenv('OKX_DEMO_MODE', 'false').lower() == 'true'

    if api_key and secret_key and passphrase:
        # Backend selected by EXCHANGE_BACKEND (rest | websocket); default rest.
        # Used for account info always, order execution only if not paper trading.
        spot_adapter, futures_adapter, backend_label = _build_trade_adapters(
            api_key, secret_key, passphrase, is_demo)
        engine.set_adapters(spot_adapter, futures_adapter)
        global _execution_backend
        _execution_backend = backend_label
        logger.info("%s adapters configured: demo=%s, paper=%s, symbols=(%s, %s)",
                   backend_label, is_demo, config.paper_trading, config.spot_symbol, config.futures_symbol)

        # RFQ executor — wired when rfq_notional_threshold_usd > 0.
        # Routes large-notional trades to OKX atomic RFQ instead of the order book,
        # eliminating legging risk. Disabled by default (threshold=0).
        if getattr(config, 'rfq_notional_threshold_usd', 0.0) > 0:
            from adapters.okx_rfq_adapter import OKXRFQAdapter
            from core.rfq_executor import RFQExecutor
            rfq_adapter = OKXRFQAdapter(api_key, secret_key, passphrase, is_demo)
            rfq_exec = RFQExecutor(rfq_adapter, config)
            engine.set_rfq_executor(rfq_exec)
            logger.info("RFQ executor configured (threshold=$%.0f, timeout=%ss, fallback=%s)",
                        config.rfq_notional_threshold_usd,
                        config.rfq_quote_timeout_sec,
                        config.rfq_fallback_to_orderbook)
    else:
        logger.warning("API keys not configured - using paper trading simulation only")

    # Start engine - schedule the coroutine and give it time to start
    logger.info("Scheduling engine.start() coroutine...")
    future = asyncio.run_coroutine_threadsafe(engine.start(), loop)
    # Give the async task time to start running
    time.sleep(0.2)
    logger.info("Trading engine started (future done=%s)", future.done())


def stop_engine_loop():
    """Stop the trading engine gracefully."""
    global loop, shutdown_in_progress

    if shutdown_in_progress:
        return
    shutdown_in_progress = True

    logger.info("Shutting down trading engine...")

    if loop:
        try:
            # Stop the engine (which stops WebSocket)
            future = asyncio.run_coroutine_threadsafe(engine.stop(), loop)
            future.result(timeout=5)  # Wait up to 5 seconds
            logger.info("Trading engine stopped")
        except Exception as e:
            logger.warning("Error stopping engine: %s", e)

        # Disconnect execution adapters (engine.stop handles price streaming, not
        # the WS trade connection). Closes the WS and its background tasks cleanly.
        for ad in (getattr(engine, 'spot_adapter', None), getattr(engine, 'futures_adapter', None)):
            if ad is not None and hasattr(ad, 'disconnect'):
                try:
                    asyncio.run_coroutine_threadsafe(ad.disconnect(), loop).result(timeout=5)
                except Exception as e:
                    logger.warning("Error disconnecting adapter: %s", e)

        try:
            # Stop the event loop
            loop.call_soon_threadsafe(loop.stop)
            logger.info("Event loop stopped")
        except Exception as e:
            logger.warning("Error stopping loop: %s", e)


def graceful_shutdown(signum=None, frame=None):
    """Handle graceful shutdown on SIGINT/SIGTERM."""
    logger.info("Received shutdown signal, cleaning up...")
    stop_engine_loop()
    logger.info("Shutdown complete")
    sys.exit(0)


# Register shutdown handlers
atexit.register(stop_engine_loop)
signal.signal(signal.SIGINT, graceful_shutdown)
# SIGTERM not available on Windows
if hasattr(signal, 'SIGTERM'):
    signal.signal(signal.SIGTERM, graceful_shutdown)


# Callback functions for engine events
def on_tick_callback(spot_tick: MarketTick, futures_tick: MarketTick):
    """Handle tick updates."""
    # Engine Reset (and the brief window during adapter rebuild) can leave one
    # or both ticks momentarily None. Drop the tick rather than crash — the
    # next valid tick will refresh state.
    if spot_tick is None or futures_tick is None:
        return
    try:
        tick_data = {
            'spot': spot_tick.to_dict(),
            'futures': futures_tick.to_dict(),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
        # Use socketio.emit with explicit namespace for background thread
        socketio.emit('tick', tick_data, namespace='/')
    except Exception as e:
        logger.error("Error emitting tick: %s", e)

    # Save spread to database for persistence/recovery
    spread = futures_tick.mid - spot_tick.mid
    db.save_spread(
        asset=config.asset,
        spot_price=spot_tick.mid,
        futures_price=futures_tick.mid,
        spread=spread,
    )


def on_signal_callback(signal: Signal):
    """Handle signal updates."""
    try:
        signal_data = signal.to_dict()
        signal_data['asset'] = config.asset
        # Add data_points and lookback from signal generator state
        sg_state = engine.signal_generator.get_state()
        signal_data['data_points'] = sg_state.get('data_points', 0)
        signal_data['lookback'] = sg_state.get('lookback', config.lookback_period)
        signal_data['data_ready'] = sg_state.get('data_ready', False)
        signal_data['std_ratio'] = sg_state.get('std_ratio')
        signal_data['std_ratio_required'] = sg_state.get('std_ratio_required')
        signal_data['last_blocked_signal'] = sg_state.get('last_blocked_signal')
        # β + converted prices ship on EVERY tick so the dashboard doesn't
        # flicker between the periodic status fetch (has these) and the tick
        # emit (used to be missing them)
        signal_data['hedge_ratio'] = sg_state.get('hedge_ratio', 1.0)
        signal_data['beta_x_spot'] = sg_state.get('beta_x_spot')
        signal_data['fut_div_beta'] = sg_state.get('fut_div_beta')
        # Sizing fields needed by the dashboard's per-leg notional / leverage /
        # margin readout. Cheap to ship every tick, keeps the dashboard in sync
        # the instant the user saves a new size or leverage.
        signal_data['position_size_usd'] = config.position_size_usd
        signal_data['leg_a_leverage'] = config.spot_leverage
        signal_data['leg_b_leverage'] = config.futures_leverage
        signal_data['leg_a_symbol'] = config.spot_symbol
        signal_data['leg_b_symbol'] = config.futures_symbol
        # Used by the always-visible 'Last Signal Blocked' card so it can render
        # the idle-state hint ("Waiting for z to cross ±X") and know when to
        # pause block tracking (engine doesn't generate signals while in position).
        signal_data['entry_threshold'] = config.entry_threshold
        signal_data['current_position'] = engine.state.current_position
        socketio.emit('signal', signal_data, namespace='/')
    except Exception as e:
        logger.error("Error emitting signal: %s", e)

    # Log significant signals
    if signal.signal_type != "NONE":
        db.log_signal(signal_data)


def on_trade_callback(trade: Trade):
    """Handle trade updates."""
    try:
        # Save all trades (paper and real) so Telegram commands like /trades,
        # /pnl, /eod work in paper-trading mode. The is_paper flag lets stats
        # queries exclude paper trades from real P&L calculations.
        trade.id = db.save_trade(trade)
        # Fire post-trade AI analysis for real closed trades only
        if not trade.is_paper and not trade.is_open:
            post_trade_analyzer.analyze_async(trade)
        # Always emit to socket so the dashboard shows real-time updates
        socketio.emit('trade', trade.to_dict(), namespace='/')
    except Exception as e:
        logger.error("Error emitting trade: %s", e)


def on_error_callback(error: str):
    """Handle error updates."""
    try:
        socketio.emit('error', {'message': error}, namespace='/')
    except Exception as e:
        logger.error("Error emitting error event: %s", e)


def on_sd_touch_callback(event):
    """Handle SD touch events - log to database."""
    db.log_sd_touch(event)
    logger.debug("SD touch: level=%s, direction=%s, zscore=%.4f",
                 event.sd_level, event.direction, event.zscore)


# Routes
@app.route('/')
def index():
    """Redirect to dashboard."""
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
def dashboard():
    """Main trading dashboard."""
    config = db.get_config()
    exchanges = db.get_exchanges()
    is_demo = os.getenv('OKX_DEMO_MODE', 'false').lower() == 'true'
    return render_template('dashboard.html',
                           config=config,
                           exchanges=exchanges,
                           assets=CRYPTO_ASSETS,
                           is_demo=is_demo)


@app.route('/settings')
def settings():
    """Configuration page."""
    config = db.get_config()
    is_demo = os.getenv('OKX_DEMO_MODE', 'false').lower() == 'true'
    return render_template('settings.html',
                           config=config,
                           assets=CRYPTO_ASSETS,
                           is_demo=is_demo)


@app.route('/setup')
def setup():
    """Exchange management page."""
    exchanges = db.get_exchanges()
    is_demo = os.getenv('OKX_DEMO_MODE', 'false').lower() == 'true'
    config = db.get_config()
    return render_template('setup.html', exchanges=exchanges, is_demo=is_demo, config=config)


@app.route('/analysis')
def analysis():
    """SD touch analysis page."""
    config = db.get_config()
    sd_touches = db.get_sd_touches(asset=config.asset, limit=500)
    stats = db.get_trade_statistics()
    is_demo = os.getenv('OKX_DEMO_MODE', 'false').lower() == 'true'
    return render_template('analysis.html',
                           config=config,
                           is_demo=is_demo,
                           sd_touches=[t.to_dict() for t in sd_touches],
                           stats=stats,
                           assets=CRYPTO_ASSETS)


# API Routes
@app.route('/api/config', methods=['GET'])
def get_config():
    """Get current configuration."""
    config = db.get_config()
    return jsonify(config.to_dict())


@app.route('/api/config', methods=['POST'])
def save_config():
    """Save configuration."""
    global config, engine

    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data received'}), 400

        # Telegram settings live in a separate panel — the main Settings form
        # doesn't include them in its payload. Without this merge, from_dict()
        # below would reset every missing telegram_* field to its dataclass
        # default (enabled→False, chat_id→""), silently killing the bot every
        # time the user saves any unrelated setting. Also honor the '***'
        # sentinel sent by the Telegram panel to mean "keep the saved token".
        existing = db.get_config()
        telegram_fields = (
            'telegram_enabled', 'telegram_bot_token', 'telegram_chat_id',
            'telegram_notify_trades', 'telegram_notify_signals', 'telegram_notify_errors',
        )
        for field in telegram_fields:
            if field not in data or data.get(field) == '***':
                data[field] = getattr(existing, field)

        # Validate leverage bounds. OKX BTC/ETH perpetual SWAPs support up to 50x.
        for lev_key in ('spot_leverage', 'futures_leverage'):
            if lev_key in data:
                try:
                    v = int(float(data[lev_key]))
                    data[lev_key] = max(1, min(v, 50))
                except (TypeError, ValueError):
                    data[lev_key] = 1

        # Derive the pair label (used as the spread-history / trade key) from
        # the chosen legs if the client didn't supply one. Same underlying ->
        # base symbol (e.g. "BTC"); different legs -> "ETH/SOL".
        if not (data.get('asset') or '').strip():
            spot_base = (data.get('spot_symbol') or '').split('-')[0].upper()
            fut_base = (data.get('futures_symbol') or '').split('-')[0].upper()
            if spot_base and fut_base:
                data['asset'] = spot_base if spot_base == fut_base else f"{spot_base}/{fut_base}"
            else:
                data['asset'] = spot_base or fut_base

        # algo_enabled is controlled via /api/algo/toggle, not the settings form.
        # Preserve the live engine state so saving settings never turns the algo off.
        if 'algo_enabled' not in data:
            data['algo_enabled'] = engine.state.algo_enabled

        config = TradingConfig.from_dict(data)
        db.save_config(config)

        # Update engine (also pushes Telegram config to notifier via update_config)
        engine.update_config(config)

        return jsonify({'success': True, 'config': config.to_dict()})
    except Exception as e:
        logger.error("Error saving config: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# In-process cache for the instrument universe (rarely changes; refresh hourly)
_instruments_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_INSTRUMENTS_TTL = 3600  # seconds


@app.route('/api/instruments', methods=['GET'])
def list_instruments():
    """List all tradable spot and futures instruments for leg selection.

    Returns {spot: [...], futures: [...]} where each entry is
    {instId, base, quote, label}. This lets the user pair any spot
    instrument with any futures instrument — the same underlying (basis
    trade) or two entirely different instruments (cross-instrument spread).

    Cached for an hour; pass ?refresh=1 to force a re-fetch.
    """
    force = request.args.get('refresh') == '1'
    now = time.time()
    cached = _instruments_cache["data"]
    if (not force and cached is not None
            and now - _instruments_cache["ts"] < _INSTRUMENTS_TTL):
        return jsonify(cached)

    spot_adapter = engine.spot_adapter or engine.futures_adapter
    futures_adapter = engine.futures_adapter or engine.spot_adapter
    if not (spot_adapter or futures_adapter) or not loop:
        return jsonify({'success': False,
                        'error': 'No exchange adapter connected',
                        'spot': [], 'futures': []}), 503

    async def _fetch():
        spot = await spot_adapter.get_instruments("SPOT") if spot_adapter else []
        swap = await futures_adapter.get_instruments("SWAP") if futures_adapter else []
        dated = await futures_adapter.get_instruments("FUTURES") if futures_adapter else []
        # Swap + dated futures share the "futures" leg in the UI; the
        # category tag on each entry distinguishes them.
        fut = swap + dated
        fut.sort(key=lambda x: (x.get("category") != "usdt_linear", x["instId"]))
        return spot, fut

    try:
        future = asyncio.run_coroutine_threadsafe(_fetch(), loop)
        spot, fut = future.result(timeout=20)
        payload = {
            'success': True,
            'spot': spot,
            'futures': fut,
            'spot_count': len(spot),
            'futures_count': len(fut),
        }
        # Only cache a non-empty, successful result
        if spot or fut:
            _instruments_cache.update(ts=now, data=payload)
        return jsonify(payload)
    except Exception as e:
        logger.error("Error listing instruments: %s", e)
        return jsonify({'success': False, 'error': str(e),
                        'spot': [], 'futures': []}), 500


# Short cache so rapid Leg A / Leg B edits don't hammer OKX
_leg_prices_cache: Dict[str, Any] = {}
_LEG_PRICES_TTL = 5  # seconds


@app.route('/api/leg-prices', methods=['GET'])
def get_leg_prices():
    """Live mid prices for any two OKX instruments + suggested hedge ratio.

    Used by the Settings page to suggest β. For dollar-neutral hedging the
    canonical β is mid(Leg B) / mid(Leg A) — same formula the dashboard's
    × β converted-price hint uses for display. The OLS cointegration slope
    (the textbook stat-arb β) is similar but not identical.

    Cached 5 seconds to keep rapid debounced calls from rate-limiting OKX.
    Returns 503 if no exchange adapter is connected, 502 if either symbol
    has no valid ticker (delisted / typo), 400 on missing params.
    """
    leg_a = (request.args.get('leg_a') or '').strip()
    leg_b = (request.args.get('leg_b') or '').strip()
    if not leg_a or not leg_b:
        return jsonify({'success': False,
                        'error': 'leg_a and leg_b query params required'}), 400

    now = time.time()
    cache_key = f"{leg_a}|{leg_b}"
    cached = _leg_prices_cache.get(cache_key)
    if cached and now - cached['ts'] < _LEG_PRICES_TTL:
        return jsonify(cached['payload'])

    adapter = engine.spot_adapter or engine.futures_adapter
    if not adapter or not loop:
        return jsonify({'success': False,
                        'error': 'No exchange adapter connected'}), 503

    async def _fetch():
        # Fetch both tickers in parallel
        return await asyncio.gather(
            adapter.get_tick(leg_a),
            adapter.get_tick(leg_b),
            return_exceptions=True,
        )

    try:
        future = asyncio.run_coroutine_threadsafe(_fetch(), loop)
        tick_a, tick_b = future.result(timeout=10)
    except Exception as e:
        logger.error("leg-prices fetch error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500

    def safe_mid(tick):
        if not tick or isinstance(tick, Exception):
            return None
        m = getattr(tick, 'mid', 0)
        return float(m) if m and m > 0 else None

    mid_a = safe_mid(tick_a)
    mid_b = safe_mid(tick_b)
    if mid_a is None or mid_b is None:
        return jsonify({
            'success': False,
            'error': f"Could not fetch valid prices (leg_a={leg_a}, leg_b={leg_b})",
            'leg_a_price': mid_a,
            'leg_b_price': mid_b,
        }), 502

    suggested_beta = mid_b / mid_a
    payload = {
        'success': True,
        'leg_a': leg_a,
        'leg_b': leg_b,
        'leg_a_price': round(mid_a, 8),
        'leg_b_price': round(mid_b, 8),
        'suggested_beta': round(suggested_beta, 6),
    }
    _leg_prices_cache[cache_key] = {'ts': now, 'payload': payload}
    return jsonify(payload)


@app.route('/api/balance-debug', methods=['GET'])
def balance_debug():
    """Diagnostic for 'why does my balance show \\$0?' situations.

    OKX has separate wallets for Trading (Unified) and Funding (assets/deposit).
    The algo's get_account_info only reads Trading. New deposits — especially
    non-USDT ones — land in Funding by default and stay invisible until you
    transfer + (often) convert. This endpoint returns both side by side plus
    a cross-account valuation so it's obvious where the money actually is.
    """
    adapter = engine.futures_adapter or engine.spot_adapter
    if not adapter or not loop:
        return jsonify({'success': False,
                        'error': 'No exchange adapter connected'}), 503

    async def _fetch():
        return await asyncio.gather(
            adapter.get_account_info(),
            adapter.get_trading_balances_detailed() if hasattr(adapter, 'get_trading_balances_detailed') else asyncio.sleep(0, result=[]),
            adapter.get_funding_balances() if hasattr(adapter, 'get_funding_balances') else asyncio.sleep(0, result=[]),
            adapter.get_asset_valuation('USDT') if hasattr(adapter, 'get_asset_valuation') else asyncio.sleep(0, result=None),
            return_exceptions=True,
        )

    try:
        future = asyncio.run_coroutine_threadsafe(_fetch(), loop)
        trading, trading_breakdown, funding, total_usd = future.result(timeout=15)
    except Exception as e:
        logger.error("balance-debug fetch failed: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500

    def _ok(x):
        return x if not isinstance(x, Exception) else None

    trading_info     = _ok(trading)
    trading_currs    = _ok(trading_breakdown) or []
    funding_list     = _ok(funding) or []
    valuation        = _ok(total_usd)

    trading_eq = trading_info.total_equity if trading_info else 0.0
    trading_av = trading_info.available_balance_usd if trading_info else 0.0
    funding_total = sum(b['bal'] for b in funding_list) if funding_list else 0.0

    # Currencies that count toward equity but contribute zero to available
    # margin — the 'has value but not margin-eligible' case. The canonical
    # example is fiat (AED/EUR) on a unified account.
    non_margin = [c for c in trading_currs if c['eq'] > 0.01 and c['availEq'] < 0.01]

    diagnosis = []
    if trading_eq > 0.01 and trading_av < 0.01 and non_margin:
        ccys = ", ".join(f"{c['ccy']} ({c['cashBal']:g})" for c in non_margin)
        diagnosis.append(
            f"Trading wallet has ${trading_eq:.2f} equity but $0 available "
            f"because your funds are in currencies that count toward equity "
            f"but cannot be used as margin: {ccys}."
        )
        diagnosis.append(
            "Fix: in OKX, convert these to USDT. On unified accounts the "
            "Convert feature usually lives in Funding — you may need to "
            "transfer the non-margin currency back to Funding, convert it "
            "to USDT there, then transfer USDT back to Trading."
        )
    elif trading_eq < 0.01 and funding_total > 0:
        diagnosis.append(
            "Funds detected in your Funding wallet but Trading is empty. "
            "OKX deposits land in Funding by default. The algo can only use "
            "the Trading (Unified) wallet."
        )
        diagnosis.append("Fix: in OKX, go to Assets → Transfer, move funds "
                         "from Funding to Trading. Non-USDT currencies "
                         "(AED, EUR, BTC) often need to be converted to "
                         "USDT first before they count as available margin.")
    elif trading_eq < 0.01 and funding_total < 0.01:
        diagnosis.append(
            "Both Trading and Funding wallets are empty. If you've made a "
            "recent deposit, OKX may take a few minutes (and sometimes "
            "blockchain confirmations) to credit it. Check the OKX 'Deposits' "
            "history page."
        )
    elif trading_av > 0.01:
        diagnosis.append(f"Trading wallet has ${trading_eq:.2f} equity and "
                         f"${trading_av:.2f} available — the algo can use this.")

    return jsonify({
        'success': True,
        'trading': {
            'equity_usd':    round(trading_eq, 4),
            'available_usd': round(trading_av, 4),
            'currencies':    trading_currs,
            'non_margin_eligible': [c['ccy'] for c in non_margin],
            'note': 'equity_usd is what the dashboard shows; available_usd is what guard #10 uses.',
        },
        'funding': {
            'currencies':    funding_list,
            'total_native':  round(funding_total, 4),
            'note': 'Deposits land here. Algo CANNOT use this directly.',
        },
        'cross_account_valuation_usdt': round(valuation, 4) if valuation else None,
        'diagnosis': diagnosis,
    })


@app.route('/api/engine/set-demo-mode', methods=['POST'])
def set_demo_mode():
    """Switch between OKX Demo and Live server modes.

    Writes OKX_DEMO_MODE to .env, updates os.environ in-process, then
    stops and restarts the engine so new adapters and WebSocket pick up
    the change — no server restart required.
    """
    global ws_manager
    data = request.json or {}
    enable_demo = bool(data.get('demo', False))

    env_path = os.path.join(os.path.dirname(__file__), '.env')
    _upsert_env_var(env_path, 'OKX_DEMO_MODE', 'true' if enable_demo else 'false')
    os.environ['OKX_DEMO_MODE'] = 'true' if enable_demo else 'false'

    # Stop engine and WebSocket, then restart with new mode
    try:
        if loop:
            future = asyncio.run_coroutine_threadsafe(engine.stop(), loop)
            future.result(timeout=10)
            logger.info("Engine stopped for demo-mode switch (demo=%s)", enable_demo)
    except Exception as e:
        logger.warning("Error stopping engine during demo switch: %s", e)

    # Restart — loop already exists so start_engine_loop() reuses it
    try:
        start_engine_loop()
        mode_label = "Demo" if enable_demo else "Live"
        logger.info("Engine restarted in %s mode", mode_label)
        return jsonify({'success': True, 'demo': enable_demo})
    except Exception as e:
        logger.exception("Failed to restart engine after demo switch: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/engine/toggle-algo', methods=['POST'])
def toggle_algo():
    """Toggle algorithmic trading."""
    data = request.json
    enabled = data.get('enabled', False)

    engine.toggle_algo(enabled)
    # Re-read config from DB to avoid stale in-memory state, then update
    current_config = db.get_config()
    current_config.algo_enabled = enabled
    db.save_config(current_config)
    global config
    config = current_config

    socketio.emit('status', engine.get_status())

    return jsonify({'success': True, 'algo_enabled': enabled})


@app.route('/api/engine/status', methods=['GET'])
def get_engine_status():
    """Get engine status."""
    status = engine.get_status()
    status['execution_backend'] = _execution_backend
    return jsonify(status)


@app.route('/api/engine/reset', methods=['POST'])
def reset_engine():
    """Reset engine state."""
    engine.reset()
    return jsonify({'success': True})


@app.route('/api/engine/sync-position', methods=['POST'])
def sync_position():
    """Sync engine position state with database.

    This recovers the position if the engine lost track of it (e.g., after restart).
    Can also be used to force-clear the position if it's stuck.
    """
    data = request.json or {}
    action = data.get('action', 'recover')  # 'recover' or 'clear'

    if action == 'clear':
        # Force clear the position state (useful if position was manually closed on exchange)
        old_position = engine.state.current_position
        engine.state.current_position = "NONE"
        engine.signal_generator.set_position("NONE")
        engine.open_trade = None

        # Also mark any open trades in DB as closed
        open_trades = db.get_trades(limit=10, open_only=True)
        for trade in open_trades:
            db.close_trade(trade.id, exit_reason="MANUAL_SYNC")

        socketio.emit('status', engine.get_status())

        return jsonify({
            'success': True,
            'action': 'clear',
            'previous_position': old_position,
            'current_position': 'NONE',
            'trades_closed': len(open_trades),
        })

    elif action == 'recover':
        # Recover position from database
        open_trades = db.get_trades(limit=1, open_only=True)

        if not open_trades:
            return jsonify({
                'success': True,
                'action': 'recover',
                'message': 'No open trades in database',
                'current_position': engine.state.current_position,
            })

        open_trade = open_trades[0]

        # Check if it matches the current asset
        if open_trade.asset != config.asset:
            return jsonify({
                'success': False,
                'error': f'Open trade is for {open_trade.asset}, current asset is {config.asset}',
            })

        # Recover the position
        old_position = engine.state.current_position
        engine.open_trade = open_trade
        engine.state.current_position = open_trade.position_type
        engine.signal_generator.set_position(open_trade.position_type)

        socketio.emit('status', engine.get_status())

        return jsonify({
            'success': True,
            'action': 'recover',
            'previous_position': old_position,
            'recovered_position': open_trade.position_type,
            'trade_id': open_trade.id,
            'entry_zscore': open_trade.entry_zscore,
            'entry_time': open_trade.entry_time.isoformat() if open_trade.entry_time else None,
        })

    else:
        return jsonify({'success': False, 'error': f'Unknown action: {action}'}), 400


# Any exchange position worth less than this is treated as rounding dust —
# kept visible in /api/exchange-positions for sweeping, but excluded from
# mismatch detection so test-trade residue doesn't flip the warning banner.
MIN_POSITION_USD = 1.0


@app.route('/api/exchange-positions', methods=['GET'])
def get_exchange_positions():
    """
    Get actual positions from the exchange.

    This helps detect orphaned futures positions that the engine lost track of.
    """
    adapter = engine.futures_adapter
    if not adapter:
        return jsonify({'positions': [], 'error': 'No futures adapter available'})

    async def fetch_positions():
        positions = await adapter.get_positions()
        # Enrich every position with min_qty so the caller knows whether the
        # quantity is large enough to close via API. Dust below min_qty is
        # locked at the exchange and can't be swept by us.
        spot_adapter = engine.spot_adapter or adapter
        out = []
        for pos in positions:
            usd_value = abs(pos.quantity * pos.entry_price) if pos.entry_price else 0
            is_swap = is_derivative(pos.symbol)
            picker = adapter if is_swap else spot_adapter
            min_qty = 0.0
            try:
                if hasattr(picker, 'get_symbol_info'):
                    info = await picker.get_symbol_info(pos.symbol)
                    if info:
                        min_qty = float(info.get('min_qty') or 0)
            except Exception:
                pass
            out.append((pos, usd_value, is_swap, min_qty))
        return out

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(fetch_positions(), loop)
            positions = future.result(timeout=10)

            # Format positions for response.
            # get_positions() now normalises quantities and sides for all instTypes:
            #   SWAP/FUTURES: qty = contracts, side from sign
            #   MARGIN SHORT: qty converted from USDT→BTC via avgPx, side = SHORT
            #   MARGIN LONG:  qty in BTC, side = LONG
            #
            # Anything worth less than MIN_POSITION_USD is rounding residue from
            # closed test/real trades on cross margin, not a tradeable position.
            # We split it into a separate `dust_positions` bucket so the UI can
            # still show & sweep it, but it doesn't flip the mismatch banner.
            position_list = []
            dust_list = []
            for pos, usd_value, is_swap, min_qty in positions:
                is_dust = usd_value < MIN_POSITION_USD
                # Dust is "closeable" only if the quantity meets the exchange
                # minimum lot size — otherwise the close order would be rejected.
                closeable = (min_qty <= 0) or (abs(pos.quantity) >= min_qty)
                entry = {
                    'symbol': pos.symbol,
                    'side': pos.side,
                    'quantity': pos.quantity,
                    'entry_price': pos.entry_price,
                    'unrealized_pnl': pos.unrealized_pnl,
                    'leverage': pos.leverage,
                    'is_swap': is_swap,  # hint for UI: SWAP = bot-managed
                    'usd_value': round(usd_value, 4),
                    'is_dust': is_dust,
                    'min_qty': min_qty,
                    'closeable': closeable,
                }
                (dust_list if is_dust else position_list).append(entry)

            # Compare with engine state.
            # Guard: during entry execution the engine position is still NONE
            # while the faster futures leg may already be filled on the exchange.
            # Treat entry-in-progress as "has position" to suppress false alerts.
            entry_in_progress = getattr(engine, '_executing_trade', False)
            engine_position = engine.state.current_position if engine.state else "NONE"
            engine_has_position = engine_position != "NONE" or entry_in_progress
            exchange_has_position = len(position_list) > 0

            # Detect mismatch — dust is excluded so sub-$1 residue from closed
            # test trades never raises a false alarm.
            mismatch = False
            mismatch_reason = None

            if engine_position != "NONE" and not exchange_has_position:
                mismatch = True
                mismatch_reason = "Engine thinks position is open but exchange has no position"
            elif not engine_has_position and exchange_has_position:
                mismatch = True
                mismatch_reason = "Exchange has position but engine shows FLAT"

            return jsonify({
                'success': True,
                'positions': position_list,
                'dust_positions': dust_list,
                'engine_position': engine_position,
                'engine_has_position': engine_has_position,
                'exchange_has_position': exchange_has_position,
                'mismatch': mismatch,
                'mismatch_reason': mismatch_reason,
            })

        except Exception as e:
            logger.error("Error fetching exchange positions: %s", e)
            return jsonify({'success': False, 'positions': [], 'error': str(e)})

    return jsonify({'positions': [], 'error': 'Event loop not running'})


@app.route('/api/close-exchange-position', methods=['POST'])
def close_exchange_position():
    """
    Close a position directly on the exchange.

    Use this to close orphaned positions that the engine lost track of.
    """
    data = request.json or {}
    symbol = data.get('symbol')

    if not symbol:
        return jsonify({'success': False, 'error': 'Symbol is required'})

    # Use the correct adapter: spot for MARGIN symbols, futures for SWAP / dated FUTURES
    is_swap = is_derivative(symbol)
    adapter = engine.futures_adapter if is_swap else (engine.spot_adapter or engine.futures_adapter)
    if not adapter:
        return jsonify({'success': False, 'error': 'No adapter available'})

    async def close_position():
        return await adapter.close_position(symbol)

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(close_position(), loop)
            result = future.result(timeout=30)

            if result.success:
                logger.info("Closed exchange position for %s", symbol)
                return jsonify({
                    'success': True,
                    'message': f'Position closed for {symbol}',
                    'order_id': result.order_id,
                })
            else:
                return jsonify({'success': False, 'error': result.error})

        except Exception as e:
            logger.error("Error closing exchange position: %s", e)
            return jsonify({'success': False, 'error': str(e)})

    return jsonify({'success': False, 'error': 'Event loop not running'})


@app.route('/api/ai-monitor/status', methods=['GET'])
def ai_monitor_status():
    """Return the AI monitor's last verdict and run time."""
    monitor = getattr(engine, 'ai_monitor', None)
    if monitor is None:
        return jsonify({'enabled': False, 'reason': 'monitor not attached'})
    return jsonify(monitor.get_status())


@app.route('/api/key-env-probe', methods=['POST'])
def key_env_probe():
    """
    Definitive diagnostic for 50101: try the stored credentials against BOTH
    the live and the demo OKX endpoints and report which one accepts them.
    The result tells the operator exactly which environment their API key
    belongs to, removing any ambiguity from OKX's UI.
    """
    adapter = engine.spot_adapter or engine.futures_adapter
    if adapter is None:
        return jsonify({'success': False, 'error': 'no adapter configured'}), 400
    api_key = getattr(adapter, 'api_key', None)
    secret = getattr(adapter, 'secret_key', None)
    passphrase = getattr(adapter, 'passphrase', None)
    if not (api_key and secret and passphrase):
        return jsonify({'success': False,
                        'error': 'no API credentials stored on the adapter'}), 400

    async def probe(is_testnet: bool):
        probe_adapter = OKXAdapter(
            api_key=api_key, secret_key=secret, passphrase=passphrase,
            is_testnet=is_testnet,
        )
        try:
            await probe_adapter.connect()
            result = await probe_adapter._request("GET", "/api/v5/account/config")
            return {
                'accepted': bool(result and result.get('code') == '0'),
                'code': result.get('code') if result else None,
                'msg': result.get('msg') if result else None,
            }
        except Exception as exc:
            return {'accepted': False, 'code': None, 'msg': str(exc)}
        finally:
            try:
                await probe_adapter.disconnect()
            except Exception:
                pass

    async def run_both():
        live = await probe(is_testnet=False)
        demo = await probe(is_testnet=True)
        return live, demo

    if not loop:
        return jsonify({'success': False, 'error': 'event loop not running'}), 500
    try:
        future = asyncio.run_coroutine_threadsafe(run_both(), loop)
        live, demo = future.result(timeout=20)
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500

    if live['accepted'] and not demo['accepted']:
        verdict = 'LIVE — key is correctly issued for the live environment'
    elif demo['accepted'] and not live['accepted']:
        verdict = ('DEMO — key was issued in OKX demo trading. '
                   'Recreate at https://www.okx.com (Live mode) to use with paper_trading=False.')
    elif live['accepted'] and demo['accepted']:
        verdict = 'BOTH (unexpected) — investigate manually'
    else:
        verdict = ('NEITHER — credentials are rejected by both environments. '
                   'Possible causes: deleted/disabled key, IP not whitelisted, '
                   'or typo in stored secret/passphrase.')

    # First 6 chars of the API key as a fingerprint so the operator can
    # cross-check which key the bot has in memory (full key never logged/returned).
    key_fingerprint = (api_key[:6] + '…' + api_key[-4:]) if len(api_key) > 12 else 'short'

    return jsonify({
        'success': True,
        'key_fingerprint': key_fingerprint,
        'live':  live,
        'demo':  demo,
        'verdict': verdict,
    })


@app.route('/api/sweep-dust', methods=['POST'])
def sweep_dust_positions():
    """
    Close every sub-MIN_POSITION_USD exchange position in one shot.

    These are typically rounding residue left in the cross-margin ledger
    after closed test/real trades. Closing them on demand keeps the ledger
    tidy without spamming the position-mismatch banner.
    """
    spot_adapter = engine.spot_adapter or engine.futures_adapter
    futures_adapter = engine.futures_adapter
    if not (spot_adapter or futures_adapter):
        return jsonify({'success': False, 'error': 'No adapter available'}), 400
    if not loop:
        return jsonify({'success': False, 'error': 'Event loop not running'}), 500

    async def fetch_and_sweep():
        positions = await (futures_adapter or spot_adapter).get_positions()
        results = []
        for pos in positions:
            usd_value = abs(pos.quantity * pos.entry_price) if pos.entry_price else 0
            if usd_value >= MIN_POSITION_USD:
                continue
            is_swap = is_derivative(pos.symbol)
            adapter = futures_adapter if is_swap else spot_adapter
            if not adapter:
                results.append({'symbol': pos.symbol, 'success': False,
                                'error': 'No adapter for symbol', 'locked': False})
                continue

            # Pre-flight: if the dust quantity is below the exchange minimum
            # lot size the order would be rejected. Mark it as locked and skip
            # — repeatedly hitting the API only fills the log with errors.
            min_qty = 0.0
            try:
                if hasattr(adapter, 'get_symbol_info'):
                    info = await adapter.get_symbol_info(pos.symbol)
                    if info:
                        min_qty = float(info.get('min_qty') or 0)
            except Exception:
                pass
            if min_qty > 0 and abs(pos.quantity) < min_qty:
                results.append({
                    'symbol': pos.symbol,
                    'usd_value': round(usd_value, 4),
                    'quantity': pos.quantity,
                    'min_qty': min_qty,
                    'success': False,
                    'locked': True,
                    'error': (f'Below exchange min lot size '
                              f'({pos.quantity:.8f} < {min_qty:.8f}) — '
                              'sweep manually on OKX'),
                })
                continue

            try:
                res = await adapter.close_position(pos.symbol)
                results.append({
                    'symbol': pos.symbol,
                    'usd_value': round(usd_value, 4),
                    'success': bool(res.success),
                    'locked': False,
                    'error': None if res.success else res.error,
                })
            except Exception as exc:
                results.append({'symbol': pos.symbol, 'success': False,
                                'locked': False, 'error': str(exc)})
        return results

    try:
        future = asyncio.run_coroutine_threadsafe(fetch_and_sweep(), loop)
        results = future.result(timeout=60)
        ok_count = sum(1 for r in results if r['success'])
        locked_count = sum(1 for r in results if r.get('locked'))
        fail_count = len(results) - ok_count - locked_count
        logger.info(
            "Dust sweep: %d closed, %d locked (below exchange min), %d failed, %d total",
            ok_count, locked_count, fail_count, len(results),
        )
        return jsonify({
            'success': True,
            'swept': ok_count,
            'locked': locked_count,
            'failed': fail_count,
            'total': len(results),
            'results': results,
        })
    except Exception as e:
        logger.exception("Dust sweep failed")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/spot-holdings', methods=['GET'])
def get_spot_holdings():
    """
    Get current spot holdings (non-USDT assets).

    This helps detect orphaned spot positions from incomplete trades.
    """
    adapter = engine.spot_adapter or engine.futures_adapter
    if not adapter or not hasattr(adapter, 'get_spot_balances'):
        return jsonify({'holdings': [], 'error': 'No adapter available'})

    async def fetch_balances():
        return await adapter.get_spot_balances()

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(fetch_balances(), loop)
            balances = future.result(timeout=10)

            # Filter out stablecoins, keep only crypto assets
            stablecoins = {'USDT', 'USDC', 'BUSD', 'DAI', 'TUSD'}
            holdings = []

            # Minimum USD value to consider as orphan (ignore dust < $1)
            MIN_USD_ORPHAN_THRESHOLD = 1.0

            for currency, bal_info in balances.items():
                if currency not in stablecoins:
                    available = bal_info.get('available', 0)
                    frozen = bal_info.get('frozen', 0)
                    total = bal_info.get('total', 0) or (available + frozen)

                    if total > 0.00000001:  # Filter out zero
                        # Get USD value
                        usd_value = 0
                        if engine.spot_tick and currency == config.asset:
                            usd_value = total * engine.spot_tick.mid

                        # Only include if above minimum USD threshold (ignore dust)
                        if usd_value >= MIN_USD_ORPHAN_THRESHOLD:
                            holdings.append({
                                'currency': currency,
                                'available': available,
                                'frozen': frozen,
                                'total': total,
                                'usd_value': usd_value,
                                'is_trading_asset': currency == config.asset,
                                'can_sell': available > 0.00000001,
                            })

            # Check if there's an orphan (holding without active position).
            # Guard: during entry execution the position is still NONE but BTC
            # is legitimately being bought — never flag as orphan mid-execution.
            has_orphan = False
            entry_in_progress = getattr(engine, '_executing_trade', False)
            for h in holdings:
                if h['is_trading_asset'] and engine.state.current_position == "NONE" and not entry_in_progress:
                    has_orphan = True
                    h['is_orphan'] = True

            return jsonify({
                'holdings': holdings,
                'has_orphan': has_orphan,
                'current_position': engine.state.current_position,
            })

        except Exception as e:
            logger.error("Error fetching spot holdings: %s", e)
            return jsonify({'holdings': [], 'error': str(e)})

    return jsonify({'holdings': [], 'error': 'Event loop not running'})


@app.route('/api/close-orphaned-spot', methods=['POST'])
def close_orphaned_spot():
    """
    Sell orphaned spot holdings back to USDT.

    Use this when a trade exit only closed the futures leg, leaving spot behind.
    """
    data = request.json or {}
    currency = data.get('currency', config.asset)  # Default to trading asset

    adapter = engine.spot_adapter
    if not adapter or not hasattr(adapter, 'sell_spot_to_usdt'):
        return jsonify({'success': False, 'error': 'No spot adapter available'})

    async def sell_to_usdt():
        # Get current balance (returns dict with 'available', 'total', etc.)
        balance_info = await adapter.get_asset_balance(currency)
        available = balance_info.get('available', 0) if isinstance(balance_info, dict) else 0

        if available <= 0:
            return None, f"No {currency} balance to sell (available: {available})"

        # Sell to USDT
        result = await adapter.sell_spot_to_usdt(currency)
        return result, available

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(sell_to_usdt(), loop)
            result, amount = future.result(timeout=30)

            if result is None:
                return jsonify({'success': True, 'message': amount})  # amount is error message here

            if result.success:
                return jsonify({
                    'success': True,
                    'currency': currency,
                    'amount_sold': amount,
                    'order_id': result.order_id,
                })
            else:
                return jsonify({'success': False, 'error': result.error})

        except Exception as e:
            logger.error("Error closing orphaned spot: %s", e)
            return jsonify({'success': False, 'error': str(e)})

    return jsonify({'success': False, 'error': 'Event loop not running'})


@app.route('/api/exchanges', methods=['GET'])
def get_exchanges():
    """Get all exchanges."""
    exchanges = db.get_exchanges()
    return jsonify([e.to_dict() for e in exchanges])


@app.route('/api/exchanges', methods=['POST'])
def add_exchange():
    """Add new exchange."""
    data = request.json

    exchange = Exchange(
        name=data.get('name', ''),
        exchange_type=data.get('exchange_type', ''),
        api_key=data.get('api_key', ''),
        secret_key=data.get('secret_key', ''),
        passphrase=data.get('passphrase', ''),
        is_testnet=data.get('is_testnet', True),
        role=data.get('role', 'BOTH'),
    )

    exchange_id = db.save_exchange(exchange)
    exchange.id = exchange_id

    return jsonify({'success': True, 'exchange': exchange.to_dict()})


@app.route('/api/exchanges/<int:exchange_id>', methods=['DELETE'])
def delete_exchange(exchange_id):
    """Delete exchange."""
    db.delete_exchange(exchange_id)
    return jsonify({'success': True})


@app.route('/api/exchanges/<int:exchange_id>/test', methods=['POST'])
def test_exchange(exchange_id):
    """Test exchange connection."""
    exchange = db.get_exchange(exchange_id)
    if not exchange:
        return jsonify({'success': False, 'error': 'Exchange not found'}), 404

    # Create adapter based on type
    adapter = create_adapter(exchange)
    if not adapter:
        return jsonify({'success': False, 'error': 'Unknown exchange type'}), 400

    # Test connection
    async def test_connection():
        try:
            connected = await adapter.connect()
            if connected:
                account = await adapter.get_account_info()
                await adapter.disconnect()
                return True, account.to_dict() if account else {}
            else:
                return False, adapter.last_error
        except Exception as e:
            return False, str(e)

    if loop:
        future = asyncio.run_coroutine_threadsafe(test_connection(), loop)
        success, result = future.result(timeout=30)
    else:
        success, result = False, "Engine not started"

    # Update status
    db.update_exchange_status(
        exchange_id,
        "CONNECTED" if success else "ERROR",
        "" if success else str(result)
    )

    return jsonify({
        'success': success,
        'account': result if success else None,
        'error': result if not success else None
    })


@app.route('/api/set-active-exchanges', methods=['POST'])
def set_active_exchanges():
    """Set active exchanges for trading."""
    data = request.json
    spot_id = data.get('spot_id')
    futures_id = data.get('futures_id')

    db.set_active_exchanges(spot_id, futures_id)

    # Update engine adapters
    spot_adapter = None
    futures_adapter = None

    if spot_id:
        exchange = db.get_exchange(spot_id)
        if exchange:
            spot_adapter = create_adapter(exchange, is_futures=False)

    if futures_id:
        exchange = db.get_exchange(futures_id)
        if exchange:
            futures_adapter = create_adapter(exchange, is_futures=True)

    engine.set_adapters(spot_adapter, futures_adapter)

    return jsonify({'success': True})


@app.route('/api/trades', methods=['GET'])
def get_trades():
    """Get recent trades."""
    limit = min(request.args.get('limit', 100, type=int), 1000)
    trades = db.get_trades(limit=limit)
    return jsonify([t.to_dict() for t in trades])


# Cache for account config (UID + account level) — refreshed at most once per 60 s
_account_config_cache: dict = {}
_account_config_cache_ts: float = 0.0
_ACCOUNT_CONFIG_TTL = 60.0  # seconds


@app.route('/api/account-info', methods=['GET'])
def get_account_info():
    """Get detailed account information including margin requirements."""
    # Determine exchange type and demo mode from environment or adapter
    is_demo = os.getenv('OKX_DEMO_MODE', 'false').lower() == 'true'
    exchange_type = os.getenv('EXCHANGE_TYPE', 'OKX').upper()

    # Check if API keys are configured
    api_key = os.getenv('OKX_API_KEY', '')
    has_api_keys = bool(api_key and os.getenv('OKX_SECRET_KEY', '') and os.getenv('OKX_PASSPHRASE', ''))

    account_data = {
        'connected': False,
        'exchange': exchange_type,
        'uid': '',
        'account_level': '',
        'balance': 0,
        'available': 0,
        'margin_used': 0,
        'unrealized_pnl': 0,
        'daily_pnl': 0,
        'is_demo': is_demo,
        # Enhanced margin details
        'total_equity': 0,
        'initial_margin': 0,
        'maintenance_margin': 0,
        'margin_ratio': 0,
        'available_margin': 0,
        'leverage_used': 0,
        # Position margin breakdown
        'spot_margin_used': 0,
        'futures_margin_used': 0,
        'spot_unrealized_pnl': 0,
        'futures_unrealized_pnl': 0,
        # Risk metrics
        'liquidation_price': None,
        'mark_price': None,
        'margin_health': 'N/A',  # SAFE, WARNING, DANGER
        # Debug info
        'has_api_keys': has_api_keys,
        'has_adapters': bool(engine.spot_adapter or engine.futures_adapter),
    }

    # Check if we have adapters connected
    if engine.spot_adapter or engine.futures_adapter:
        adapter = engine.spot_adapter or engine.futures_adapter

        # Get demo mode from adapter if available
        if hasattr(adapter, 'is_testnet'):
            account_data['is_demo'] = adapter.is_testnet

        # Get exchange type from adapter
        adapter_type = type(adapter).__name__.replace('Adapter', '').upper()
        account_data['exchange'] = adapter_type

        try:
            # Get account info from adapter
            async def fetch_account():
                if hasattr(adapter, 'get_account_info'):
                    return await adapter.get_account_info()
                return None

            async def fetch_position_margin():
                if hasattr(adapter, 'get_position_margin_info'):
                    return await adapter.get_position_margin_info(config.futures_symbol)
                return None

            async def fetch_account_config():
                if hasattr(adapter, 'get_account_config'):
                    return await adapter.get_account_config()
                return None

            if loop:
                # Fetch account info
                future = asyncio.run_coroutine_threadsafe(fetch_account(), loop)
                account = future.result(timeout=10)

                if account:
                    account_data['connected'] = True
                    if account.exchange:
                        account_data['exchange'] = account.exchange
                    account_data['balance'] = account.balance_usd
                    account_data['available'] = account.available_balance_usd
                    account_data['margin_used'] = account.margin_used
                    account_data['unrealized_pnl'] = account.unrealized_pnl
                    account_data['total_equity'] = account.total_equity
                    account_data['initial_margin'] = account.initial_margin
                    account_data['maintenance_margin'] = account.maintenance_margin
                    account_data['margin_ratio'] = account.margin_ratio
                    account_data['available_margin'] = account.available_margin
                    account_data['leverage_used'] = account.leverage_used

                    # Determine margin health
                    if account.margin_ratio > 500:
                        account_data['margin_health'] = 'SAFE'
                    elif account.margin_ratio > 150:
                        account_data['margin_health'] = 'WARNING'
                    elif account.margin_ratio > 0:
                        account_data['margin_health'] = 'DANGER'

                # Fetch position margin info
                pos_future = asyncio.run_coroutine_threadsafe(fetch_position_margin(), loop)
                pos_margin = pos_future.result(timeout=10)

                if pos_margin:
                    account_data['liquidation_price'] = pos_margin.get('liquidation_price')
                    account_data['mark_price'] = pos_margin.get('mark_price')
                    account_data['futures_margin_used'] = pos_margin.get('imr', 0)
                    account_data['futures_unrealized_pnl'] = pos_margin.get('unrealized_pnl', 0)

                # Fetch account config for UID — cached to avoid rate-limiting
                import time as _time
                global _account_config_cache, _account_config_cache_ts
                try:
                    if _account_config_cache and (_time.monotonic() - _account_config_cache_ts) < _ACCOUNT_CONFIG_TTL:
                        account_config = _account_config_cache
                    else:
                        config_future = asyncio.run_coroutine_threadsafe(fetch_account_config(), loop)
                        account_config = config_future.result(timeout=10)
                        if account_config:
                            _account_config_cache = account_config
                            _account_config_cache_ts = _time.monotonic()

                    if account_config:
                        account_data['uid'] = account_config.get('uid', '')
                        account_data['account_level'] = account_config.get('level', '')
                        logger.debug("UID fetched: %s, Level: %s", account_data['uid'], account_data['account_level'])
                    else:
                        logger.warning("Account config returned None")
                except Exception as config_err:
                    logger.warning("Error fetching account config: %s", config_err)

                # Fetch actual position leverage from exchange
                try:
                    async def fetch_positions():
                        if hasattr(adapter, 'get_positions'):
                            return await adapter.get_positions()
                        return []

                    pos_future = asyncio.run_coroutine_threadsafe(fetch_positions(), loop)
                    positions = pos_future.result(timeout=10)

                    # Add actual leverage info from positions
                    account_data['positions'] = []
                    for pos in positions:
                        pos_data = pos.to_dict()
                        account_data['positions'].append(pos_data)
                        # Track leverage from actual positions. is_derivative
                        # covers SWAP + dated FUTURES; everything else is spot/margin.
                        if is_derivative(pos.symbol):
                            account_data['actual_futures_leverage'] = pos.leverage
                            account_data['futures_leverage_source'] = 'exchange'
                        elif pos.symbol:
                            # This is a spot/margin position - get its leverage
                            account_data['actual_spot_leverage'] = pos.leverage
                            account_data['spot_leverage_source'] = 'exchange'

                    # Account-balance upl is often 0 on demo / non-portfolio-margin accounts.
                    # Fall back to summing unrealized_pnl across individual positions.
                    if account_data['unrealized_pnl'] == 0 and positions:
                        account_data['unrealized_pnl'] = sum(p.unrealized_pnl for p in positions)
                except Exception as pos_err:
                    logger.warning("Error fetching positions: %s", pos_err)

        except Exception as e:
            logger.warning("Error fetching account info: %s", e)

    # Add configured leverage for comparison
    account_data['configured_spot_leverage'] = config.spot_leverage
    account_data['configured_futures_leverage'] = config.futures_leverage

    # Set actual leverage — prefer exchange data, use sensible defaults that
    # respect the LEG'S instrument shape (not a blanket 'spot = cash' assumption).
    # Leg A can be a derivative too (e.g. ETH-USDT-260626), in which case it uses
    # the configured spot_leverage from settings, not the 'cash' fallback.
    if 'actual_spot_leverage' not in account_data:
        if is_derivative(config.spot_symbol):
            # Leg A is a derivative — use the user's configured leverage
            account_data['actual_spot_leverage'] = config.spot_leverage
            account_data['spot_leverage_source'] = 'configured'
        else:
            # Genuine spot leg — cash trading, no leverage
            account_data['actual_spot_leverage'] = 1
            account_data['spot_leverage_source'] = 'cash'

    # For futures: Check if we need to fetch leverage from exchange settings
    if 'actual_futures_leverage' not in account_data:
        # Try to get leverage setting from exchange for the futures symbol
        try:
            async def fetch_futures_leverage():
                adapter = engine.futures_adapter or engine.spot_adapter
                if adapter and hasattr(adapter, 'get_leverage_info'):
                    return await adapter.get_leverage_info(config.futures_symbol)
                return None

            if loop:
                lev_future = asyncio.run_coroutine_threadsafe(fetch_futures_leverage(), loop)
                lev_info = lev_future.result(timeout=5)
                if lev_info and lev_info.get('leverage'):
                    account_data['actual_futures_leverage'] = lev_info['leverage']
                    account_data['futures_leverage_source'] = 'exchange'
                else:
                    account_data['actual_futures_leverage'] = config.futures_leverage
                    account_data['futures_leverage_source'] = 'configured'
        except Exception as lev_err:
            logger.debug("Could not fetch futures leverage: %s", lev_err)
            account_data['actual_futures_leverage'] = config.futures_leverage
            account_data['futures_leverage_source'] = 'configured'

    # Calculate daily P&L from trades
    stats = db.get_trade_statistics()
    if stats:
        account_data['daily_pnl'] = stats.get('daily_pnl', 0)

    return jsonify(account_data)


@app.route('/api/trade-journal', methods=['GET'])
def get_trade_journal():
    """Get trade journal with statistics."""
    trades = db.get_trades(limit=500)
    stats = db.get_trade_statistics()

    return jsonify({
        'trades': [t.to_dict() for t in trades],
        'statistics': stats
    })


@app.route('/api/spread-history', methods=['GET'])
def get_spread_history():
    """Get spread history for charting."""
    n = request.args.get('n', 100, type=int)
    spreads = engine.get_spread_history(n)
    zscores = engine.get_zscore_history(n)

    return jsonify({
        'spreads': spreads,
        'zscores': zscores
    })


@app.route('/api/sd-touches', methods=['GET'])
def get_sd_touches():
    """Get SD touch events."""
    asset = request.args.get('asset')
    limit = min(request.args.get('limit', 500, type=int), 5000)

    touches = db.get_sd_touches(asset=asset, limit=limit)
    return jsonify([t.to_dict() for t in touches])


# ============== Reset/Delete API Endpoints ==============

@app.route('/api/trades/clear', methods=['POST'])
def clear_trades():
    """Clear all trades (or for specific asset)."""
    data = request.json or {}
    asset = data.get('asset')  # Optional - if provided, clear only for this asset

    deleted = db.clear_trades(asset=asset)
    return jsonify({'success': True, 'deleted': deleted})


@app.route('/api/trades/<int:trade_id>', methods=['DELETE'])
def delete_trade(trade_id):
    """Delete a specific trade."""
    success = db.delete_trade(trade_id)
    if success:
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Trade not found'}), 404


@app.route('/api/trades/<int:trade_id>/close', methods=['POST'])
def close_trade_manually(trade_id):
    """Manually close an open trade."""
    # Get current prices for the close
    if engine.spot_tick and engine.futures_tick:
        spot_price = engine.spot_tick.mid
        futures_price = engine.futures_tick.mid
        spread = futures_price - spot_price
        zscore = engine.signal_generator.current_zscore

        # Update the trade with exit details
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trades SET
                    exit_time = ?,
                    exit_spot_price = ?,
                    exit_futures_price = ?,
                    exit_spread = ?,
                    exit_zscore = ?,
                    exit_reason = 'MANUAL',
                    is_open = 0
                WHERE id = ? AND is_open = 1
            """, (
                datetime.now(timezone.utc).isoformat(),
                spot_price, futures_price, spread, zscore, trade_id
            ))
            if cursor.rowcount > 0:
                # Reset engine position
                engine.state.current_position = "NONE"
                engine.signal_generator.set_position("NONE")
                engine.open_trade = None
                logger.info("Manually closed trade %d at spread=%.2f, zscore=%.4f", trade_id, spread, zscore)
                return jsonify({'success': True})

    # Fallback - just mark as closed without prices
    success = db.close_trade(trade_id, exit_reason="MANUAL")
    if success:
        engine.state.current_position = "NONE"
        engine.signal_generator.set_position("NONE")
        engine.open_trade = None
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Trade not found or already closed'}), 404


@app.route('/api/sd-touches/clear', methods=['POST'])
def clear_sd_touches():
    """Clear all SD touch events (or for specific asset)."""
    data = request.json or {}
    asset = data.get('asset')

    deleted = db.clear_sd_touches(asset=asset)
    # Also clear from signal generator memory
    engine.signal_generator.sd_touch_events.clear()
    engine.signal_generator.last_sd_level = 0.0

    return jsonify({'success': True, 'deleted': deleted})


@app.route('/api/spread-history/clear', methods=['POST'])
def clear_spread_history():
    """Clear spread history and reset signal generator."""
    data = request.json or {}
    asset = data.get('asset')

    deleted = db.clear_spread_history(asset=asset)
    # Reset signal generator
    engine.signal_generator.reset()

    return jsonify({'success': True, 'deleted': deleted})


@app.route('/api/engine/close-position', methods=['POST'])
def close_current_position():
    """Close the current open position manually."""
    if engine.state.current_position == "NONE" or not engine.open_trade:
        return jsonify({'success': False, 'error': 'No open position'}), 400

    if not engine.spot_tick or not engine.futures_tick:
        return jsonify({'success': False, 'error': 'No price data available'}), 400

    # Build a manual signal using current market state.  signal_type="MANUAL"
    # is stored directly as trade.exit_reason by _close_position.
    manual_signal = Signal(
        signal_type="MANUAL",
        zscore=engine.signal_generator.current_zscore,
        spread=engine.signal_generator.current_spread,
        spread_mean=engine.signal_generator.current_mean,
        spread_std=engine.signal_generator.current_std,
        hurst=engine.signal_generator.current_hurst,
        regime="MANUAL_CLOSE",
        current_position=engine.state.current_position,
        timestamp=datetime.now(timezone.utc),
    )

    async def do_close():
        # Capture reference before _close_position nulls engine.open_trade.
        # _close_position modifies the trade object in-place: it stamps fill
        # prices, recalculates P&L from fills (with correct maker/taker fees),
        # marks is_open=False, saves via on_trade callback, and resets state.
        trade = engine.open_trade
        await engine._close_position(manual_signal)
        return trade

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(do_close(), loop)
            # 120s covers the worst-case orphan-recovery path (~60s per leg +
            # headroom).  The old 30s limit fired before BTC recovery filled,
            # causing the trade to record mid-price P&L instead of fill P&L.
            trade = future.result(timeout=120)
            if trade and not trade.is_open:
                return jsonify({
                    'success': True,
                    'trade': trade.to_dict(),
                    'message': f"Position closed. P&L: ${trade.pnl_usd:.2f} ({trade.pnl_percent:.2f}%)"
                })
            # _close_position returned without closing (orders failed); engine
            # will retry on the next tick.
            return jsonify({
                'success': False,
                'error': 'Exit orders did not complete — engine will retry. Check logs.'
            }), 500
        except concurrent.futures.TimeoutError:
            # Recovery is still running in the background.  The trade will be
            # saved correctly once fills arrive — no data loss, just a slow exit.
            logger.warning("Manual close timeout (>120s) — orphan recovery still running")
            return jsonify({
                'success': False,
                'error': 'Close taking longer than expected (recovery in progress). '
                         'Check Trade Journal for the final result once recovery completes.'
            }), 504
        except Exception as e:
            logger.error("Error during manual close: %s", e)
            return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'success': False, 'error': 'Event loop not running'}), 500


@app.route('/api/exchange-orders', methods=['GET'])
def get_exchange_orders():
    """Fetch real order history from the exchange (OKX)."""
    limit = min(request.args.get('limit', 50, type=int), 500)

    adapter = engine.futures_adapter or engine.spot_adapter
    if not adapter or not hasattr(adapter, 'get_order_history'):
        return jsonify({'orders': [], 'error': 'No adapter available'})

    async def fetch_orders():
        return await adapter.get_order_history(limit=limit)

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(fetch_orders(), loop)
            orders = future.result(timeout=15)
            return jsonify({'orders': orders})
        except Exception as e:
            logger.error("Error fetching exchange orders: %s", e)
            return jsonify({'orders': [], 'error': str(e)})

    return jsonify({'orders': [], 'error': 'Event loop not running'})


@app.route('/api/exchange-orders/csv', methods=['GET'])
def download_exchange_orders_csv():
    """Download exchange order history as CSV."""
    import csv
    import io
    from flask import Response

    limit = min(request.args.get('limit', 100, type=int), 1000)

    adapter = engine.futures_adapter or engine.spot_adapter
    if not adapter or not hasattr(adapter, 'get_order_history'):
        return Response("No adapter available", status=400)

    async def fetch_orders():
        return await adapter.get_order_history(limit=limit)

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(fetch_orders(), loop)
            orders = future.result(timeout=15)

            # Create CSV
            output = io.StringIO()
            if orders:
                fieldnames = ['created_at', 'symbol', 'inst_type', 'side', 'pos_side',
                              'order_type', 'quantity', 'fill_qty', 'fill_price',
                              'leverage', 'fee', 'fee_ccy', 'pnl', 'state', 'order_id']
                writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                for order in orders:
                    writer.writerow(order)

            output.seek(0)
            return Response(
                output.getvalue(),
                mimetype='text/csv',
                headers={'Content-Disposition': 'attachment; filename=exchange_orders.csv'}
            )
        except Exception as e:
            logger.error("Error exporting exchange orders: %s", e)
            return Response(f"Error: {e}", status=500)

    return Response("Event loop not running", status=500)


@app.route('/api/anthropic/status', methods=['GET'])
def anthropic_status():
    """Return whether the Anthropic API key is configured."""
    key = os.getenv('ANTHROPIC_API_KEY', '')
    if key:
        preview = key[:8] + '...' + key[-4:] if len(key) > 12 else '***'
        return jsonify({'configured': True, 'preview': preview})
    return jsonify({'configured': False, 'preview': None})


@app.route('/api/anthropic/key', methods=['POST'])
def save_anthropic_key():
    """Save Anthropic API key to .env file and update running environment."""
    try:
        data = request.json or {}
        key = (data.get('key') or '').strip()
        if not key:
            return jsonify({'success': False, 'error': 'No key provided'})
        if not key.startswith('sk-ant-'):
            return jsonify({'success': False, 'error': 'Invalid key — must start with sk-ant-'})

        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
        _upsert_env_var(env_path, 'ANTHROPIC_API_KEY', key)

        os.environ['ANTHROPIC_API_KEY'] = key
        post_trade_analyzer._api_key = key
        logger.info("Anthropic API key updated via dashboard")

        preview = key[:8] + '...' + key[-4:]
        return jsonify({'success': True, 'preview': preview})
    except Exception as exc:
        logger.exception("Error saving Anthropic API key")
        return jsonify({'success': False, 'error': str(exc)})


@app.route('/api/anthropic/test', methods=['POST'])
def test_anthropic_key():
    """Test the Anthropic API key with a minimal API call."""
    key = os.getenv('ANTHROPIC_API_KEY', '')
    if not key:
        return jsonify({'success': False, 'error': 'No API key configured'}), 400
    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=5,
            messages=[{'role': 'user', 'content': 'hi'}],
        )
        return jsonify({'success': True, 'model': msg.model})
    except ImportError:
        return jsonify({'success': False, 'error': 'anthropic package not installed — run: pip install anthropic'}), 500
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400


def _upsert_env_var(env_path: str, var_name: str, value: str) -> None:
    """Write or update a single variable in a .env file."""
    # Strip newlines/carriage-returns to prevent variable injection into .env
    safe_value = value.replace('\r', '').replace('\n', '')
    # Quote the value if it contains spaces or special shell characters
    if any(c in safe_value for c in (' ', '#', '$', '`', '"', "'")):
        safe_value = f'"{safe_value}"'

    lines: list = []
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            lines = f.readlines()

    key_prefix = f'{var_name}='
    found = False
    for i, line in enumerate(lines):
        if line.startswith(key_prefix):
            lines[i] = f'{key_prefix}{safe_value}\n'
            found = True
            break
    if not found:
        if lines and not lines[-1].endswith('\n'):
            lines.append('\n')
        lines.append(f'{key_prefix}{safe_value}\n')

    with open(env_path, 'w') as f:
        f.writelines(lines)


@app.route('/api/learnings', methods=['GET'])
def get_learnings():
    """Return recent structured learnings from post-trade AI analysis."""
    limit = min(request.args.get('limit', 20, type=int), 500)
    learnings = db.get_recent_learnings(limit=limit)
    import json as _json
    for lrn in learnings:
        try:
            lrn['recommendations'] = _json.loads(lrn.get('recommendations') or '[]')
        except Exception:
            lrn['recommendations'] = []
    return jsonify(learnings)


@app.route('/api/learning-log', methods=['GET'])
def get_learning_log():
    """Return the auto-tune parameter change history."""
    limit = min(request.args.get('limit', 50, type=int), 500)
    log = db.get_learning_log(limit=limit)
    import json as _json
    for entry in log:
        try:
            entry['learning_ids'] = _json.loads(entry.get('learning_ids') or '[]')
        except Exception:
            entry['learning_ids'] = []
    return jsonify(log)


@app.route('/api/ai-insights', methods=['GET'])
def get_ai_insights():
    """Return pending AI insights (observations + high-evidence filter/position suggestions)."""
    status = request.args.get('status', 'pending')
    limit = min(request.args.get('limit', 30, type=int), 500)
    if status == 'pending':
        return jsonify(db.get_pending_insights(limit=limit))
    # All statuses
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM ai_insights ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        return jsonify([dict(r) for r in cursor.fetchall()])


@app.route('/api/ai-insights/<int:insight_id>/apply', methods=['POST'])
def apply_ai_insight(insight_id: int):
    """
    Apply a FILTER_TOGGLE insight directly from the dashboard.
    Updates DB config + live engine config.
    """
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM ai_insights WHERE id = ?", (insight_id,))
        row = cursor.fetchone()

    if not row:
        return jsonify({'success': False, 'error': 'Insight not found'}), 404
    insight = dict(row)

    if insight['status'] != 'pending':
        return jsonify({'success': False, 'error': 'Insight already acted on'}), 400

    param           = insight['param']
    suggested_value = insight['suggested_value']

    try:
        current_config = db.get_config()

        # Determine the right type cast for this param
        existing = getattr(current_config, param, None)
        if isinstance(existing, bool):
            new_val = suggested_value.lower() in ('1', '1.0', 'true', 'yes')
        elif isinstance(existing, int):
            new_val = int(float(suggested_value))
        else:
            new_val = float(suggested_value)

        # Hard bounds for safety-critical parameters
        PARAM_BOUNDS = {
            'spot_leverage':     (1, 20),
            'futures_leverage':  (1, 20),
            'position_size_usd': (10, 1_000_000),
            'entry_threshold':   (0.1, 10.0),
            'exit_threshold':    (0.0, 10.0),
            'stop_loss_threshold': (0.1, 20.0),
            'min_std_multiple':  (0.1, 10.0),
        }
        if param in PARAM_BOUNDS:
            lo, hi = PARAM_BOUNDS[param]
            clamped = max(lo, min(new_val, hi))
            if clamped != new_val:
                logger.warning("AI insight clipped %s from %s to %s (bounds %s–%s)", param, new_val, clamped, lo, hi)
                new_val = clamped

        setattr(current_config, param, new_val)
        db.save_config(current_config)
        if engine:
            setattr(engine.config, param, new_val)

        db.update_insight_status(insight_id, 'applied')
        logger.info("AI insight applied: %s = %s (from dashboard)", param, new_val)
        return jsonify({'success': True, 'param': param, 'new_value': new_val})

    except Exception as e:
        logger.error("apply_ai_insight error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ai-insights/<int:insight_id>/dismiss', methods=['DELETE'])
def dismiss_ai_insight(insight_id: int):
    """Dismiss an AI insight."""
    found = db.update_insight_status(insight_id, 'dismissed')
    return jsonify({'success': found})


@app.route('/api/trades/csv', methods=['GET'])
def download_trades_csv():
    """Download trade journal as CSV."""
    import csv
    import io
    from flask import Response

    limit = min(request.args.get('limit', 500, type=int), 5000)
    trades = db.get_trades(limit=limit)

    output = io.StringIO()
    if trades:
        fieldnames = ['id', 'asset', 'position_type', 'entry_time', 'entry_spot_price',
                      'entry_futures_price', 'entry_spread', 'entry_zscore',
                      'exit_time', 'exit_spot_price', 'exit_futures_price',
                      'exit_spread', 'exit_zscore', 'exit_reason',
                      'quantity', 'notional_usd', 'pnl_usd', 'pnl_percent',
                      'spot_order_id', 'futures_order_id', 'is_open', 'is_paper']
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for trade in trades:
            writer.writerow(trade.to_dict())

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=trade_journal.csv'}
    )


@app.route('/api/active-orders', methods=['GET'])
def get_active_orders():
    """Get currently active/pending orders."""
    orders = []

    # Check if there's an active order in the order executor
    if engine.order_executor and engine.order_executor.active_order:
        spread_order = engine.order_executor.active_order
        orders.append({
            'type': 'ENTRY' if spread_order.is_entry else 'EXIT',
            'position_type': spread_order.position_type,
            'created_at': spread_order.created_at.isoformat() if spread_order.created_at else None,
            'timeout_at': spread_order.timeout_at.isoformat() if spread_order.timeout_at else None,
            'spot_leg': {
                'symbol': spread_order.spot_leg.symbol,
                'side': spread_order.spot_leg.side,
                'quantity': spread_order.spot_leg.quantity,
                'target_price': spread_order.spot_leg.target_price,
                'order_id': spread_order.spot_leg.order_id,
                'status': spread_order.spot_leg.status.value,
                'filled_qty': spread_order.spot_leg.filled_qty,
                'filled_price': spread_order.spot_leg.filled_price,
            },
            'futures_leg': {
                'symbol': spread_order.futures_leg.symbol,
                'side': spread_order.futures_leg.side,
                'quantity': spread_order.futures_leg.quantity,
                'target_price': spread_order.futures_leg.target_price,
                'order_id': spread_order.futures_leg.order_id,
                'status': spread_order.futures_leg.status.value,
                'filled_qty': spread_order.futures_leg.filled_qty,
                'filled_price': spread_order.futures_leg.filled_price,
            },
            'is_complete': spread_order.is_complete,
            'has_partial_fill': spread_order.has_partial_fill,
        })

    return jsonify({
        'orders': orders,
        'execution_mode': config.order_execution_mode,
        'is_executing': engine.order_executor._executing if engine.order_executor else False,
    })


# ============== Telegram API Endpoints ==============

@app.route('/api/telegram/test', methods=['POST'])
def test_telegram():
    """Test Telegram connection by sending a test message."""
    import requests as _requests
    data = request.json or {}
    token = data.get('token', '').strip()
    chat_id = data.get('chat_id', '').strip()

    if not token or not chat_id:
        return jsonify({'success': False, 'error': 'token and chat_id are required'}), 400

    from datetime import datetime, timezone as _tz
    ts = datetime.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    msg = (
        "<b>Nexus Stat-Arb</b>\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"Connected and ready.  {ts}\n\n"
        "<b>Commands</b>\n"
        "/status     engine &amp; algo state\n"
        "/positions  open positions\n"
        "/trades     recent closed trades\n"
        "/balance    account balance\n"
        "/pnl        P&amp;L summary\n"
        "/eod        end-of-day report"
    )
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = _requests.post(url, json={
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        if resp.status_code == 200:
            return jsonify({'success': True, 'error': None})
        try:
            err_desc = resp.json().get('description', resp.text[:300])
        except Exception:
            err_desc = resp.text[:300]
        return jsonify({'success': False, 'error': f"Telegram error ({resp.status_code}): {err_desc}"})
    except Exception as e:
        return jsonify({'success': False, 'error': f"Request failed: {e}"})


@app.route('/api/telegram/config', methods=['POST'])
def save_telegram_config():
    """Save Telegram settings independently (without a full config save)."""
    global config
    data = request.json or {}

    try:
        config.telegram_enabled = bool(data.get('telegram_enabled', False))
        # '***' sentinel from the panel means "leave the saved token alone" —
        # the input field is rendered blank by design so the user doesn't have
        # to re-paste the token on every visit. Treat blank the same way.
        token_in = str(data.get('telegram_bot_token', '')).strip()
        if token_in and token_in != '***':
            config.telegram_bot_token = token_in
        config.telegram_chat_id = str(data.get('telegram_chat_id', '')).strip()
        config.telegram_notify_trades = bool(data.get('telegram_notify_trades', True))
        config.telegram_notify_signals = bool(data.get('telegram_notify_signals', False))
        config.telegram_notify_errors = bool(data.get('telegram_notify_errors', True))

        db.save_config(config)
        engine.update_config(config)  # also updates the notifier

        return jsonify({'success': True})
    except Exception as e:
        logger.error("Error saving Telegram config: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============== Manual Order Testing ==============
# Store multiple test positions
import uuid
test_positions = {}  # id -> position data


@app.route('/api/test-order/open', methods=['POST'])
def open_test_order():
    """Open a manual test order - single leg or spread."""
    global test_positions

    if not engine.spot_adapter and not engine.futures_adapter:
        return jsonify({'success': False, 'error': 'No exchange adapters available. Check API keys.'}), 400

    if not engine.spot_tick or not engine.futures_tick:
        return jsonify({'success': False, 'error': 'No price data available. Wait for connection.'}), 400

    data = request.json
    order_type = data.get('order_type', 'BUY_SPOT')
    size_usd = data.get('size_usd', 100)

    spot_price = engine.spot_tick.mid
    futures_price = engine.futures_tick.mid
    quantity = size_usd / spot_price

    logger.info("Executing test order: %s, size=$%.2f, qty=%.6f", order_type, size_usd, quantity)

    def calc_limit_price(side: str, tick) -> float:
        """
        Calculate a passive limit price matching the real order executor logic.
        BUY  → bid + offset_bps  (capped just below ask to stay maker)
        SELL → ask - offset_bps  (floored just above bid to stay maker)
        """
        offset_bps = config.limit_order_price_offset_bps / 10000
        SAFETY_BUFFER_BPS = 0.00005  # 0.5 bps safety buffer
        if side.upper() == "BUY":
            target = tick.bid * (1 + offset_bps)
            max_price = tick.ask * (1 - SAFETY_BUFFER_BPS)
            return round(min(target, max_price), 2)
        else:
            target = tick.ask * (1 - offset_bps)
            min_price = tick.bid * (1 + SAFETY_BUFFER_BPS)
            return round(max(target, min_price), 2)

    async def execute_single_leg(market_type: str, side: str, price: float, qty: float = quantity):
        """Execute a single leg order."""
        adapter = engine.spot_adapter if market_type == "SPOT" else engine.futures_adapter
        if not adapter:
            return None, f"No {market_type} adapter available", None

        symbol = config.spot_symbol if market_type == "SPOT" else config.futures_symbol
        order_type_str = config.order_execution_mode  # MARKET or LIMIT

        # For LIMIT orders use proper bid+offset / ask-offset pricing (same as OrderExecutor)
        # instead of mid price, so orders behave as maker orders
        if order_type_str == "LIMIT":
            tick = engine.spot_tick if market_type == "SPOT" else engine.futures_tick
            limit_price = calc_limit_price(side, tick)
            logger.info("LIMIT %s %s: bid=%.2f ask=%.2f offset=%.1f bps → price=%.2f",
                       side, market_type, tick.bid, tick.ask,
                       config.limit_order_price_offset_bps, limit_price)
        else:
            limit_price = None

        # For futures, determine pos_side for long_short_mode
        pos_side = None
        if market_type == "FUTURES":
            pos_side = "long" if side == "BUY" else "short"

        # Cross-margin SPOT MARKET BUY: pass USDT notional so adapter uses correct sz.
        notional = size_usd if (market_type == "SPOT" and order_type_str == "MARKET" and side == "BUY") else None
        result = await adapter.place_order(
            symbol=symbol,
            side=side,
            order_type=order_type_str,
            quantity=qty,
            price=limit_price,
            pos_side=pos_side,
            notional_usdt=notional,
        )

        if result.success:
            return result, None, pos_side
        return None, result.error, None

    async def execute_order():
        results = []

        # For futures legs, quantity must be at least 1 contract (ct_val BTC).
        # Fetch the actual ct_val; fall back to 0.01 (standard BTC-USDT-SWAP).
        futures_info = None
        if engine.futures_adapter:
            futures_info = await engine.futures_adapter.get_symbol_info(config.futures_symbol)
        ct_val = float(futures_info.get('ct_val', 0.01)) if futures_info else 0.01
        futures_qty = max(quantity, ct_val)

        if order_type == "BUY_SPOT":
            result, error, pos_side = await execute_single_leg("SPOT", "BUY", spot_price)
            if result:
                results.append(("SPOT", "BUY", spot_price, result, quantity, pos_side))
            else:
                return None, error

        elif order_type == "SELL_SPOT":
            result, error, pos_side = await execute_single_leg("SPOT", "SELL", spot_price)
            if result:
                results.append(("SPOT", "SELL", spot_price, result, quantity, pos_side))
            else:
                return None, error

        elif order_type == "BUY_FUTURES":
            result, error, pos_side = await execute_single_leg("FUTURES", "BUY", futures_price, qty=futures_qty)
            if result:
                results.append(("FUTURES", "BUY", futures_price, result, futures_qty, pos_side))
            else:
                return None, error

        elif order_type == "SELL_FUTURES":
            result, error, pos_side = await execute_single_leg("FUTURES", "SELL", futures_price, qty=futures_qty)
            if result:
                results.append(("FUTURES", "SELL", futures_price, result, futures_qty, pos_side))
            else:
                return None, error

        elif order_type == "LONG_SPREAD":
            # Buy Spot + Sell Futures
            spot_result, spot_error, _ = await execute_single_leg("SPOT", "BUY", spot_price)
            if not spot_result:
                return None, f"Spot order failed: {spot_error}"
            results.append(("SPOT", "BUY", spot_price, spot_result, quantity, None))

            futures_result, futures_error, pos_side = await execute_single_leg("FUTURES", "SELL", futures_price, qty=futures_qty)
            if not futures_result:
                return None, f"Futures order failed: {futures_error}"
            results.append(("FUTURES", "SELL", futures_price, futures_result, futures_qty, pos_side))

        elif order_type == "SHORT_SPREAD":
            # Sell Spot + Buy Futures
            spot_result, spot_error, _ = await execute_single_leg("SPOT", "SELL", spot_price)
            if not spot_result:
                return None, f"Spot order failed: {spot_error}"
            results.append(("SPOT", "SELL", spot_price, spot_result, quantity, None))

            futures_result, futures_error, pos_side = await execute_single_leg("FUTURES", "BUY", futures_price, qty=futures_qty)
            if not futures_result:
                return None, f"Futures order failed: {futures_error}"
            results.append(("FUTURES", "BUY", futures_price, futures_result, futures_qty, pos_side))

        return results, None

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(execute_order(), loop)
            results, error = future.result(timeout=60)

            if error:
                return jsonify({'success': False, 'error': error}), 400

            # Store positions
            for market_type, side, entry_price, result, qty, pos_side in results:
                pos_id = str(uuid.uuid4())[:8]
                test_positions[pos_id] = {
                    'id': pos_id,
                    'market_type': market_type,
                    'side': side,
                    'quantity': qty,
                    'entry_price': entry_price,
                    'order_id': result.order_id,
                    'entry_time': datetime.now(timezone.utc).isoformat(),
                    'pos_side': pos_side,  # Store for closing futures in long_short_mode
                }
                logger.info("Test position opened: %s %s %s @ $%.2f, order_id=%s, pos_side=%s",
                           pos_id, side, market_type, entry_price, result.order_id, pos_side)

            return jsonify({'success': True, 'positions_opened': len(results)})

        except Exception as e:
            logger.error("Error executing test order: %s", e)
            return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'success': False, 'error': 'Event loop not running'}), 500


@app.route('/api/test-order/close', methods=['POST'])
def close_test_order():
    """Close a specific test position."""
    global test_positions

    data = request.json
    position_id = data.get('position_id')

    if not position_id or position_id not in test_positions:
        return jsonify({'success': False, 'error': 'Position not found'}), 404

    pos = test_positions[position_id]

    if not engine.spot_tick or not engine.futures_tick:
        return jsonify({'success': False, 'error': 'No price data available'}), 400

    # Determine closing side (opposite of entry)
    close_side = "SELL" if pos['side'] == "BUY" else "BUY"
    market_type = pos['market_type']
    quantity = pos['quantity']
    current_price = engine.spot_tick.mid if market_type == "SPOT" else engine.futures_tick.mid

    logger.info("Closing test position %s: %s %s @ $%.2f", position_id, close_side, market_type, current_price)

    # Get stored pos_side for futures (critical for long_short_mode!)
    stored_pos_side = pos.get('pos_side')
    original_order_id = pos.get('order_id')

    async def close_position():
        adapter = engine.spot_adapter if market_type == "SPOT" else engine.futures_adapter
        if not adapter:
            return None, f"No {market_type} adapter available"

        symbol = config.spot_symbol if market_type == "SPOT" else config.futures_symbol

        # First, check if the original order is still pending (not filled)
        # If pending, we should CANCEL it, not place an opposite order
        if original_order_id:
            order_status = await adapter.get_order_status(symbol, original_order_id)
            if order_status:
                state = order_status.get("state", "")
                filled_qty = order_status.get("filled_qty", 0)

                if state in ("live", "partially_filled") or filled_qty == 0:
                    # Order is still pending - cancel it instead of placing a close order
                    logger.info("Original order %s still pending (state=%s, filled=%.6f) - cancelling",
                               original_order_id, state, filled_qty)
                    cancelled = await adapter.cancel_order(symbol, original_order_id)
                    if cancelled:
                        # Return a "mock" successful result for cancelled order
                        from models import OrderResult
                        return OrderResult(success=True, order_id=original_order_id), "cancelled"
                    else:
                        return None, f"Failed to cancel pending order {original_order_id}"

                elif state == "filled":
                    # Order was filled - use actual filled qty for close order
                    if filled_qty > 0:
                        quantity = filled_qty
                    logger.info("Original order %s was filled (filled_qty=%.8f) - placing close order",
                               original_order_id, quantity)
                else:
                    # Order was already cancelled or in unknown state
                    logger.info("Original order %s already in state '%s' - removing position", original_order_id, state)
                    from models import OrderResult
                    return OrderResult(success=True, order_id=original_order_id), "already_closed"

        # Place closing order (only if original was filled)
        order_type_str = config.order_execution_mode

        # For LIMIT closes, use bid+offset / ask-offset (same as entry logic)
        # close_side is opposite of entry: BUY close uses ask-offset, SELL close uses bid+offset
        close_limit_price = None
        if order_type_str == "LIMIT":
            tick = engine.spot_tick if market_type == "SPOT" else engine.futures_tick
            offset_bps = config.limit_order_price_offset_bps / 10000
            SAFETY_BUFFER_BPS = 0.00005
            if close_side.upper() == "BUY":
                target = tick.bid * (1 + offset_bps)
                max_price = tick.ask * (1 - SAFETY_BUFFER_BPS)
                close_limit_price = round(min(target, max_price), 2)
            else:
                target = tick.ask * (1 - offset_bps)
                min_price = tick.bid * (1 + SAFETY_BUFFER_BPS)
                close_limit_price = round(max(target, min_price), 2)
            logger.info("LIMIT close %s %s: bid=%.2f ask=%.2f offset=%.1f bps → price=%.2f",
                       close_side, market_type, tick.bid, tick.ask,
                       config.limit_order_price_offset_bps, close_limit_price)

        # Cross-margin SPOT MARKET BUY (closing a SELL position) needs sz in USDT.
        close_notional = round(quantity * current_price, 2) if (
            market_type == "SPOT" and order_type_str == "MARKET" and close_side == "BUY"
        ) else None
        result = await adapter.place_order(
            symbol=symbol,
            side=close_side,
            order_type=order_type_str,
            quantity=quantity,
            price=close_limit_price,
            pos_side=stored_pos_side,  # Use original pos_side for closing!
            reduce_only=True if market_type == "FUTURES" else False,
            notional_usdt=close_notional,
        )

        return result, None if result.success else result.error

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(close_position(), loop)
            result, status_or_error = future.result(timeout=60)

            if not result or not result.success:
                return jsonify({'success': False, 'error': status_or_error or 'Close order failed'}), 400

            # Handle different close scenarios
            if status_or_error == "cancelled":
                # Order was pending and got cancelled - no P&L
                logger.info("Test position %s cancelled (was pending, never filled)", position_id)
                del test_positions[position_id]
                return jsonify({
                    'success': True,
                    'pnl_usd': 0,
                    'close_price': 0,
                    'order_id': result.order_id,
                    'message': 'Pending order cancelled (not filled)'
                })
            elif status_or_error == "already_closed":
                # Order was already cancelled/unknown state
                logger.info("Test position %s was already closed/cancelled", position_id)
                del test_positions[position_id]
                return jsonify({
                    'success': True,
                    'pnl_usd': 0,
                    'close_price': 0,
                    'order_id': result.order_id,
                    'message': 'Position was already closed'
                })

            # Normal close - calculate P&L
            entry_price = pos['entry_price']
            if pos['side'] == "BUY":
                pnl = (current_price - entry_price) * quantity
            else:
                pnl = (entry_price - current_price) * quantity

            logger.info("Test position %s closed: P&L=$%.2f", position_id, pnl)

            # Remove position
            del test_positions[position_id]

            return jsonify({
                'success': True,
                'pnl_usd': pnl,
                'close_price': current_price,
                'order_id': result.order_id,
            })

        except Exception as e:
            logger.error("Error closing test position: %s", e)
            return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'success': False, 'error': 'Event loop not running'}), 500


@app.route('/api/test-order/close-all', methods=['POST'])
def close_all_test_orders():
    """Close all test positions."""
    global test_positions

    if not test_positions:
        return jsonify({'success': True, 'total_pnl': 0, 'closed': 0})

    if not engine.spot_tick or not engine.futures_tick:
        return jsonify({'success': False, 'error': 'No price data available'}), 400

    total_pnl = 0
    closed = 0
    errors = []

    async def close_all():
        nonlocal total_pnl, closed, errors

        for pos_id, pos in list(test_positions.items()):
            close_side = "SELL" if pos['side'] == "BUY" else "BUY"
            market_type = pos['market_type']
            quantity = pos['quantity']
            current_price = engine.spot_tick.mid if market_type == "SPOT" else engine.futures_tick.mid
            stored_pos_side = pos.get('pos_side')
            original_order_id = pos.get('order_id')

            adapter = engine.spot_adapter if market_type == "SPOT" else engine.futures_adapter
            if not adapter:
                errors.append(f"No {market_type} adapter for {pos_id}")
                continue

            symbol = config.spot_symbol if market_type == "SPOT" else config.futures_symbol

            # Check if original order is still pending - if so, cancel instead of close
            was_cancelled = False
            if original_order_id:
                order_status = await adapter.get_order_status(symbol, original_order_id)
                if order_status:
                    state = order_status.get("state", "")
                    filled_qty = order_status.get("filled_qty", 0)

                    if state in ("live", "partially_filled") or filled_qty == 0:
                        # Order still pending - cancel it
                        logger.info("Position %s order still pending - cancelling", pos_id)
                        cancelled = await adapter.cancel_order(symbol, original_order_id)
                        if cancelled:
                            was_cancelled = True
                            closed += 1
                            del test_positions[pos_id]
                            logger.info("Cancelled pending position %s", pos_id)
                            continue
                        else:
                            errors.append(f"{pos_id}: Failed to cancel pending order")
                            continue
                    elif state == "filled":
                        # Use actual filled quantity for close order
                        if filled_qty > 0:
                            quantity = filled_qty
                    elif state == "canceled":
                        # Already cancelled
                        closed += 1
                        del test_positions[pos_id]
                        logger.info("Position %s was already cancelled", pos_id)
                        continue

            # Place closing order (original was filled)
            order_type_str = config.order_execution_mode

            # For LIMIT closes, use bid+offset / ask-offset matching OrderExecutor logic
            close_limit_price = None
            if order_type_str == "LIMIT":
                tick = engine.spot_tick if market_type == "SPOT" else engine.futures_tick
                offset_bps = config.limit_order_price_offset_bps / 10000
                SAFETY_BUFFER_BPS = 0.00005
                if close_side.upper() == "BUY":
                    target = tick.bid * (1 + offset_bps)
                    max_price = tick.ask * (1 - SAFETY_BUFFER_BPS)
                    close_limit_price = round(min(target, max_price), 2)
                else:
                    target = tick.ask * (1 - offset_bps)
                    min_price = tick.bid * (1 + SAFETY_BUFFER_BPS)
                    close_limit_price = round(max(target, min_price), 2)

            result = await adapter.place_order(
                symbol=symbol,
                side=close_side,
                order_type=order_type_str,
                quantity=quantity,
                price=close_limit_price,
                pos_side=stored_pos_side,  # Use original pos_side for closing!
                reduce_only=True if market_type == "FUTURES" else False,
            )

            if result.success:
                entry_price = pos['entry_price']
                if pos['side'] == "BUY":
                    pnl = (current_price - entry_price) * quantity
                else:
                    pnl = (entry_price - current_price) * quantity

                total_pnl += pnl
                closed += 1
                del test_positions[pos_id]
                logger.info("Closed position %s: P&L=$%.2f", pos_id, pnl)
            else:
                errors.append(f"{pos_id}: {result.error}")

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(close_all(), loop)
            future.result(timeout=120)

            return jsonify({
                'success': len(errors) == 0,
                'total_pnl': total_pnl,
                'closed': closed,
                'errors': errors if errors else None,
            })

        except Exception as e:
            logger.error("Error closing all test positions: %s", e)
            return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'success': False, 'error': 'Event loop not running'}), 500


@app.route('/api/test-order/status', methods=['GET'])
def get_test_order_status():
    """Get all test positions with current prices and P&L."""
    positions = []

    for pos_id, pos in test_positions.items():
        market_type = pos['market_type']
        current_price = 0
        unrealized_pnl = 0

        if engine.spot_tick and engine.futures_tick:
            current_price = engine.spot_tick.mid if market_type == "SPOT" else engine.futures_tick.mid
            entry_price = pos['entry_price']
            quantity = pos['quantity']

            if pos['side'] == "BUY":
                unrealized_pnl = (current_price - entry_price) * quantity
            else:
                unrealized_pnl = (entry_price - current_price) * quantity

        positions.append({
            'id': pos_id,
            'market_type': market_type,
            'side': pos['side'],
            'quantity': pos['quantity'],
            'entry_price': pos['entry_price'],
            'current_price': current_price,
            'unrealized_pnl': unrealized_pnl,
            'order_id': pos['order_id'],
            'entry_time': pos['entry_time'],
        })

    return jsonify({'positions': positions})


# ─────────────────────────────────────────────────────────────────────────────
# Full Test Suite – runs 18 scenarios in the background, emits live WS events
# ─────────────────────────────────────────────────────────────────────────────

_test_suite_cancel: bool = False
_test_suite_running: bool = False
_single_running: bool = False          # True while a single-scenario run is in progress
_test_suite_state: Dict[str, Any] = {
    'running': False, 'current': 0, 'total': 36,
    'pass': 0, 'fail': 0, 'scenarios': [], 'start_time': None, 'order_mode': '',
    'single_running': False,
}

# 36 standard scenarios: 6 order-types × (fill-test, fill-test, cancel-test) × (LIMIT + MARKET)
# + 4 partial-fill recovery scenarios (always MARKET – simulates one leg filling, other failing)
_SUITE_SCENARIOS = [
    {'id': '1a', 'label': 'BUY_SPOT #1',         'order_type': 'BUY_SPOT',      'cancel_test': False},
    {'id': '1b', 'label': 'BUY_SPOT #2',         'order_type': 'BUY_SPOT',      'cancel_test': False},
    {'id': '1c', 'label': 'BUY_SPOT #3 (cancel)','order_type': 'BUY_SPOT',      'cancel_test': True},
    {'id': '2a', 'label': 'SELL_FUTURES #1',      'order_type': 'SELL_FUTURES',  'cancel_test': False},
    {'id': '2b', 'label': 'SELL_FUTURES #2',      'order_type': 'SELL_FUTURES',  'cancel_test': False},
    {'id': '2c', 'label': 'SELL_FUTURES #3 (cancel)', 'order_type': 'SELL_FUTURES', 'cancel_test': True},
    {'id': '3a', 'label': 'BUY_FUTURES #1',       'order_type': 'BUY_FUTURES',   'cancel_test': False},
    {'id': '3b', 'label': 'BUY_FUTURES #2',       'order_type': 'BUY_FUTURES',   'cancel_test': False},
    {'id': '3c', 'label': 'BUY_FUTURES #3 (cancel)', 'order_type': 'BUY_FUTURES', 'cancel_test': True},
    {'id': '4a', 'label': 'SELL_SPOT #1',         'order_type': 'SELL_SPOT',     'cancel_test': False},
    {'id': '4b', 'label': 'SELL_SPOT #2',         'order_type': 'SELL_SPOT',     'cancel_test': False},
    {'id': '4c', 'label': 'SELL_SPOT #3 (cancel)','order_type': 'SELL_SPOT',     'cancel_test': True},
    {'id': '5a', 'label': 'LONG_SPREAD #1',       'order_type': 'LONG_SPREAD',   'cancel_test': False},
    {'id': '5b', 'label': 'LONG_SPREAD #2',       'order_type': 'LONG_SPREAD',   'cancel_test': False},
    {'id': '5c', 'label': 'LONG_SPREAD #3 (cancel)', 'order_type': 'LONG_SPREAD', 'cancel_test': True},
    {'id': '6a', 'label': 'SHORT_SPREAD #1',      'order_type': 'SHORT_SPREAD',  'cancel_test': False},
    {'id': '6b', 'label': 'SHORT_SPREAD #2',      'order_type': 'SHORT_SPREAD',  'cancel_test': False},
    {'id': '6c', 'label': 'SHORT_SPREAD #3 (cancel)', 'order_type': 'SHORT_SPREAD', 'cancel_test': True},
    # ── 18 MARKET-order scenarios (forced_mode overrides config) ──────────────
    {'id': 'm1a', 'label': 'MKT BUY_SPOT #1',              'order_type': 'BUY_SPOT',      'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm1b', 'label': 'MKT BUY_SPOT #2',              'order_type': 'BUY_SPOT',      'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm1c', 'label': 'MKT BUY_SPOT #3 (quick-close)','order_type': 'BUY_SPOT',      'cancel_test': True,  'forced_mode': 'MARKET'},
    {'id': 'm2a', 'label': 'MKT SELL_FUTURES #1',           'order_type': 'SELL_FUTURES',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm2b', 'label': 'MKT SELL_FUTURES #2',           'order_type': 'SELL_FUTURES',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm2c', 'label': 'MKT SELL_FUTURES #3 (quick-close)', 'order_type': 'SELL_FUTURES', 'cancel_test': True, 'forced_mode': 'MARKET'},
    {'id': 'm3a', 'label': 'MKT BUY_FUTURES #1',            'order_type': 'BUY_FUTURES',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm3b', 'label': 'MKT BUY_FUTURES #2',            'order_type': 'BUY_FUTURES',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm3c', 'label': 'MKT BUY_FUTURES #3 (quick-close)', 'order_type': 'BUY_FUTURES', 'cancel_test': True, 'forced_mode': 'MARKET'},
    {'id': 'm4a', 'label': 'MKT SELL_SPOT #1',              'order_type': 'SELL_SPOT',     'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm4b', 'label': 'MKT SELL_SPOT #2',              'order_type': 'SELL_SPOT',     'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm4c', 'label': 'MKT SELL_SPOT #3 (quick-close)','order_type': 'SELL_SPOT',     'cancel_test': True,  'forced_mode': 'MARKET'},
    {'id': 'm5a', 'label': 'MKT LONG_SPREAD #1',            'order_type': 'LONG_SPREAD',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm5b', 'label': 'MKT LONG_SPREAD #2',            'order_type': 'LONG_SPREAD',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm5c', 'label': 'MKT LONG_SPREAD #3 (quick-close)', 'order_type': 'LONG_SPREAD', 'cancel_test': True, 'forced_mode': 'MARKET'},
    {'id': 'm6a', 'label': 'MKT SHORT_SPREAD #1',           'order_type': 'SHORT_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm6b', 'label': 'MKT SHORT_SPREAD #2',           'order_type': 'SHORT_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm6c', 'label': 'MKT SHORT_SPREAD #3 (quick-close)', 'order_type': 'SHORT_SPREAD', 'cancel_test': True, 'forced_mode': 'MARKET'},
    # ── Partial-fill / leg-failure recovery (4 scenarios, always MARKET) ──────────────────────
    # Each test: place only the named leg at MARKET (fills), skip the other (simulated failure),
    # then immediately market-close the filled leg as the recovery action.
    {'id': 'pf-1', 'label': 'LONG_SPREAD partial: spot fills, futures fails → market-close spot',
     'order_type': 'LONG_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET',
     'partial_fail_test': True, 'filled_leg': 'SPOT'},
    {'id': 'pf-2', 'label': 'LONG_SPREAD partial: futures fills, spot fails → market-close futures',
     'order_type': 'LONG_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET',
     'partial_fail_test': True, 'filled_leg': 'FUTURES'},
    {'id': 'pf-3', 'label': 'SHORT_SPREAD partial: spot fills, futures fails → market-close spot',
     'order_type': 'SHORT_SPREAD', 'cancel_test': False, 'forced_mode': 'MARKET',
     'partial_fail_test': True, 'filled_leg': 'SPOT'},
    {'id': 'pf-4', 'label': 'SHORT_SPREAD partial: futures fills, spot fails → market-close futures',
     'order_type': 'SHORT_SPREAD', 'cancel_test': False, 'forced_mode': 'MARKET',
     'partial_fail_test': True, 'filled_leg': 'FUTURES'},
]


async def _suite_open_order(order_type: str, quantity: float, forced_mode: str | None = None):
    """
    Place the opening leg(s) for a suite scenario.
    Returns (list_of_leg_tuples, error_str).  error_str is None on success.
    Each leg tuple: (market_type, side, entry_price, OrderResult, qty, pos_side, place_ms, lp)
      place_ms  – placement API round-trip in milliseconds
      lp        – limit price used (None for MARKET orders)
    forced_mode overrides config.entry_execution_mode when set (e.g. 'MARKET').
    """
    order_mode = forced_mode or config.entry_execution_mode

    def calc_limit_price(side: str, tick) -> float:
        offset_bps = config.limit_order_price_offset_bps / 10000
        SAFETY = 0.00005
        if side.upper() == "BUY":
            return round(min(tick.bid * (1 + offset_bps), tick.ask * (1 - SAFETY)), 2)
        return round(max(tick.ask * (1 - offset_bps), tick.bid * (1 + SAFETY)), 2)

    async def single_leg(market_type: str, side: str):
        adapter = engine.spot_adapter if market_type == "SPOT" else engine.futures_adapter
        if not adapter:
            return None, f"No {market_type} adapter"
        symbol   = config.spot_symbol if market_type == "SPOT" else config.futures_symbol
        tick     = engine.spot_tick    if market_type == "SPOT" else engine.futures_tick
        pos_side = ("long" if side == "BUY" else "short") if market_type == "FUTURES" else None
        lp       = calc_limit_price(side, tick) if order_mode == "LIMIT" else None
        if order_mode == "LIMIT":
            logger.info("[SUITE] LIMIT %s %s: bid=%.2f ask=%.2f offset=%.1fbps → px=%.2f",
                        side, market_type, tick.bid, tick.ask,
                        config.limit_order_price_offset_bps, lp)
        # Cross-margin SPOT MARKET BUY requires sz in USDT (not BTC qty).
        notional = round(quantity * tick.mid, 2) if (
            market_type == "SPOT" and order_mode == "MARKET" and side == "BUY"
        ) else None
        t_place_start = datetime.now(timezone.utc)
        result = await adapter.place_order(
            symbol=symbol, side=side, order_type=order_mode,
            quantity=quantity, price=lp, pos_side=pos_side,
            notional_usdt=notional,
        )
        place_ms = int((datetime.now(timezone.utc) - t_place_start).total_seconds() * 1000)
        if result.success:
            entry_price = tick.mid
            sprd_now = round(engine.futures_tick.mid - engine.spot_tick.mid, 4) \
                       if engine.spot_tick and engine.futures_tick else None
            z_now    = round(engine.signal_generator.current_zscore, 4) \
                       if engine.signal_generator else None
            std_now  = round(engine.signal_generator.current_std,  4) \
                       if engine.signal_generator else None
            mean_now = round(engine.signal_generator.current_mean, 4) \
                       if engine.signal_generator else None
            return (market_type, side, entry_price, result, quantity, pos_side, place_ms, lp,
                    tick.bid, tick.ask, sprd_now, z_now, std_now, mean_now), None
        return None, result.error

    legs: list = []
    if order_type in ("BUY_SPOT",):
        leg, err = await single_leg("SPOT", "BUY")
        if err: return None, err
        legs.append(leg)
    elif order_type == "SELL_SPOT":
        leg, err = await single_leg("SPOT", "SELL")
        if err: return None, err
        legs.append(leg)
    elif order_type == "BUY_FUTURES":
        leg, err = await single_leg("FUTURES", "BUY")
        if err: return None, err
        legs.append(leg)
    elif order_type == "SELL_FUTURES":
        leg, err = await single_leg("FUTURES", "SELL")
        if err: return None, err
        legs.append(leg)
    elif order_type == "LONG_SPREAD":
        spot_leg, err = await single_leg("SPOT", "BUY")
        if err: return None, f"Spot: {err}"
        legs.append(spot_leg)
        fut_leg, err = await single_leg("FUTURES", "SELL")
        if err: return legs, f"Futures: {err}"   # return spot leg so caller can clean up
        legs.append(fut_leg)
    elif order_type == "SHORT_SPREAD":
        spot_leg, err = await single_leg("SPOT", "SELL")
        if err: return None, f"Spot: {err}"
        legs.append(spot_leg)
        fut_leg, err = await single_leg("FUTURES", "BUY")
        if err: return legs, f"Futures: {err}"
        legs.append(fut_leg)
    else:
        return None, f"Unknown order_type: {order_type}"

    return legs, None


async def _suite_close_position(pos_id: str):
    """
    Close one test position (cancel if pending, close if filled).
    Returns (success: bool, detail_str: str).
    """
    global test_positions
    if pos_id not in test_positions:
        return False, "position not found"

    pos             = test_positions[pos_id]
    market_type     = pos['market_type']
    close_side      = "SELL" if pos['side'] == "BUY" else "BUY"
    quantity        = pos['quantity']
    symbol          = config.spot_symbol if market_type == "SPOT" else config.futures_symbol
    stored_pos_side = pos.get('pos_side')
    original_oid    = pos.get('order_id')
    adapter         = engine.spot_adapter if market_type == "SPOT" else engine.futures_adapter

    # Extra context captured when the position was opened
    target_price    = pos.get('target_price')      # limit price submitted at open (None = MARKET)
    open_mode       = pos.get('open_mode', 'MARKET')
    bid_at_open     = pos.get('bid_at_open')       # book state at open time
    ask_at_open     = pos.get('ask_at_open')
    spread_at_open  = pos.get('spread_at_open')    # futures_mid - spot_mid at open time
    zscore_at_open  = pos.get('zscore_at_open')
    std_at_open     = pos.get('std_at_open')       # spread rolling std at open time
    mean_at_open    = pos.get('mean_at_open')      # spread rolling mean at open time
    leg_label       = pos.get('leg_label', f"{market_type} {pos['side']}")
    entry_time_iso  = pos.get('entry_time', '')    # ISO timestamp of when open was placed

    # Format open timestamp for display (UTC HH:MM:SS)
    try:
        _dt_open    = datetime.fromisoformat(entry_time_iso.replace('Z', '+00:00'))
        open_ts_str = _dt_open.strftime('%H:%M:%S UTC')
    except Exception:
        open_ts_str = ''

    # Elapsed time from order placement to close/cancel
    try:
        entry_dt = datetime.fromisoformat(pos['entry_time'])
        elapsed  = (datetime.now(timezone.utc) - entry_dt).total_seconds()
        elapsed_str = f" ({elapsed:.1f}s)"
    except Exception:
        elapsed_str = ""

    if not adapter:
        return False, f"no {market_type} adapter"

    # Actual fill price of the opening order (captured below if available)
    open_fill_price: Optional[float] = None

    # Cancel if original order still pending
    if original_oid:
        status = await adapter.get_order_status(symbol, original_oid)
        if status:
            state      = status.get("state", "")
            filled_qty = status.get("filled_qty", 0)
            if state in ("live", "partially_filled") or filled_qty == 0:
                cancelled = await adapter.cancel_order(symbol, original_oid)
                # If a partial fill occurred, the already-filled qty is still open on
                # the exchange.  Market-close it so we don't leave an orphan position.
                if filled_qty and filled_qty > 0 and state == "partially_filled":
                    close_tick = engine.spot_tick if market_type == "SPOT" else engine.futures_tick
                    close_notional = round(filled_qty * close_tick.mid, 2) if (
                        market_type == "SPOT" and close_side == "BUY"
                    ) else None
                    await adapter.place_order(
                        symbol=symbol, side=close_side, order_type="MARKET",
                        quantity=filled_qty,
                        pos_side=stored_pos_side,
                        reduce_only=(market_type == "FUTURES"),
                        notional_usdt=close_notional,
                    )
                    del test_positions[pos_id]
                    return True, f"partial fill: cancelled remaining, market-closed {filled_qty:.6f} BTC{elapsed_str}"
                del test_positions[pos_id]
                return True, f"cancelled (was pending){elapsed_str}" if cancelled else f"cancel-failed{elapsed_str}"
            elif state == "filled":
                if filled_qty > 0 and market_type != "FUTURES":
                    # SPOT: accFillSz is in BTC — use actual fill qty for partial-fill accuracy.
                    # FUTURES: accFillSz is in contracts (not BTC); trust pos['quantity'] which
                    # is already stored in BTC from _suite_open_order.
                    quantity = filled_qty
                open_fill_price = status.get("filled_price")  # actual fill price of open leg
            elif state == "canceled":
                del test_positions[pos_id]
                return True, f"already cancelled{elapsed_str}"

    # Place closing order
    order_mode       = config.exit_execution_mode  # Use exit mode for closing legs
    close_lp: Optional[float] = None
    if order_mode == "LIMIT":
        tick        = engine.spot_tick if market_type == "SPOT" else engine.futures_tick
        offset_bps  = config.limit_order_price_offset_bps / 10000
        SAFETY      = 0.00005
        if close_side.upper() == "BUY":
            close_lp = round(min(tick.bid * (1 + offset_bps), tick.ask * (1 - SAFETY)), 2)
        else:
            close_lp = round(max(tick.ask * (1 - offset_bps), tick.bid * (1 + SAFETY)), 2)

    # Snapshot market state at the moment we decide to close (before placing the order)
    _ts = engine.spot_tick
    _tf = engine.futures_tick
    close_dt        = datetime.now(timezone.utc)
    close_ts_str    = close_dt.strftime('%H:%M:%S UTC')
    _close_tick     = _ts if market_type == "SPOT" else _tf
    bid_at_close    = _close_tick.bid if _close_tick else None
    ask_at_close    = _close_tick.ask if _close_tick else None
    spread_at_close = round(_tf.mid - _ts.mid, 4) if (_ts and _tf) else None
    zscore_at_close = round(engine.signal_generator.current_zscore, 4) \
                      if engine.signal_generator else None
    std_at_close    = round(engine.signal_generator.current_std,  4) \
                      if engine.signal_generator else None
    mean_at_close   = round(engine.signal_generator.current_mean, 4) \
                      if engine.signal_generator else None

    # Cross-margin SPOT MARKET BUY (closing a SELL position) needs sz in USDT.
    tick = engine.spot_tick if market_type == "SPOT" else engine.futures_tick
    close_notional = round(quantity * tick.mid, 2) if (
        market_type == "SPOT" and order_mode == "MARKET" and close_side == "BUY"
    ) else None
    result = await adapter.place_order(
        symbol=symbol, side=close_side, order_type=order_mode,
        quantity=quantity, price=close_lp,
        pos_side=stored_pos_side,
        reduce_only=(market_type == "FUTURES"),
        notional_usdt=close_notional,
    )
    if result.success:
        # Fetch actual close fill price (short wait for exchange to record the fill)
        close_fill_price: Optional[float] = None
        if result.order_id:
            await asyncio.sleep(0.4)
            try:
                cs = await adapter.get_order_status(symbol, result.order_id)
                if cs and cs.get("state") == "filled":
                    close_fill_price = cs.get("filled_price")
            except Exception:
                pass

        tick      = engine.spot_tick if market_type == "SPOT" else engine.futures_tick
        mid_close = tick.mid if tick else pos['entry_price']
        mid_open  = pos['entry_price']   # tick.mid at the moment the open order was placed

        eff_open  = open_fill_price  or mid_open
        eff_close = close_fill_price or mid_close

        pnl = (eff_close - eff_open) * quantity if pos['side'] == "BUY" \
              else (eff_open - eff_close) * quantity

        # ── Fee calculation ──────────────────────────────────────────────────
        close_exec_mode = config.exit_execution_mode
        if market_type == "SPOT":
            open_fee_bps  = config.spot_maker_fee_bps  if open_mode        == "LIMIT" else config.spot_taker_fee_bps
            close_fee_bps = config.spot_maker_fee_bps  if close_exec_mode  == "LIMIT" else config.spot_taker_fee_bps
        else:
            open_fee_bps  = config.futures_maker_fee_bps if open_mode       == "LIMIT" else config.futures_taker_fee_bps
            close_fee_bps = config.futures_maker_fee_bps if close_exec_mode == "LIMIT" else config.futures_taker_fee_bps
        open_fee_usd  = (open_fee_bps  / 10_000) * eff_open  * quantity
        close_fee_usd = (close_fee_bps / 10_000) * eff_close * quantity
        total_fee_usd = open_fee_usd + close_fee_usd
        net_pnl       = pnl - total_fee_usd

        # ── Open-leg detail ──────────────────────────────────────────────────
        # bid/ask context at open
        ba_open_str = (f"  bid=${bid_at_open:.2f} ask=${ask_at_open:.2f}"
                       if bid_at_open is not None and ask_at_open is not None else "")
        open_vs_mid_str = f"{eff_open - mid_open:+.2f}" if open_fill_price is not None else "n/a"
        if target_price is not None:
            open_vs_tgt = eff_open - target_price
            open_str = (
                f"[{leg_label}] open @ {open_ts_str}{ba_open_str}"
                f"  tgt=${target_price:.2f}  fill=${eff_open:.2f}"
                f"  (Δtgt={open_vs_tgt:+.2f}, Δmid={open_vs_mid_str})"
            )
        else:
            open_str = (
                f"[{leg_label}] open @ {open_ts_str}{ba_open_str}"
                f"  MARKET fill=${eff_open:.2f}  (Δmid={open_vs_mid_str})"
            )

        # ── Close-leg detail ─────────────────────────────────────────────────
        ba_close_str = (f"  bid=${bid_at_close:.2f} ask=${ask_at_close:.2f}"
                        if bid_at_close is not None and ask_at_close is not None else "")
        close_vs_mid_str = f"{eff_close - mid_close:+.2f}" if close_fill_price is not None else "n/a"
        if close_lp is not None:
            close_vs_tgt = eff_close - close_lp
            close_str = (
                f"[{leg_label}] close @ {close_ts_str}{ba_close_str}"
                f"  tgt=${close_lp:.2f}  fill=${eff_close:.2f}"
                f"  (Δtgt={close_vs_tgt:+.2f}, Δmid={close_vs_mid_str})"
            )
        else:
            close_str = (
                f"[{leg_label}] close @ {close_ts_str}{ba_close_str}"
                f"  MARKET fill=${eff_close:.2f}  (Δmid={close_vs_mid_str})"
            )

        # ── Spread + σ/μ/z at open ───────────────────────────────────────────
        # spread = futures_mid − spot_mid; σ = rolling std; μ = rolling mean
        # z = (spread − μ) / σ — shows distance from mean in standard deviations
        spread_open_str = None
        if spread_at_open is not None:
            parts_o: list = [f"${spread_at_open:.2f}"]
            if mean_at_open  is not None: parts_o.append(f"μ=${mean_at_open:.2f}")
            if std_at_open   is not None: parts_o.append(f"σ=${std_at_open:.2f}")
            if zscore_at_open is not None: parts_o.append(f"z={zscore_at_open:+.2f}")
            spread_open_str = "spread@open: " + "  ".join(parts_o)

        spread_close_str = None
        if spread_at_close is not None:
            parts_c: list = [f"${spread_at_close:.2f}"]
            if mean_at_close  is not None: parts_c.append(f"μ=${mean_at_close:.2f}")
            if std_at_close   is not None: parts_c.append(f"σ=${std_at_close:.2f}")
            if zscore_at_close is not None: parts_c.append(f"z={zscore_at_close:+.2f}")
            spread_close_str = "spread@close: " + "  ".join(parts_c)

        # ── Fees & P&L ───────────────────────────────────────────────────────
        fee_str = (
            f"fees: ${open_fee_usd:.3f}+${close_fee_usd:.3f}=${total_fee_usd:.3f}"
            f"  ({open_fee_bps}+{close_fee_bps} bps)"
        )
        pnl_str = f"gross=${pnl:+.2f}  net=${net_pnl:+.2f}{elapsed_str}"

        detail_parts = [open_str, close_str]
        if spread_open_str:
            detail_parts.append(spread_open_str)
        if spread_close_str:
            detail_parts.append(spread_close_str)
        detail_parts.extend([fee_str, pnl_str])

        del test_positions[pos_id]
        return True, "  |  ".join(detail_parts)
    return False, result.error


async def _suite_partial_fill_test(order_type: str, quantity: float, filled_leg: str) -> tuple[bool, str]:
    """
    Simulate a partial spread fill: place ONLY the named leg at MARKET (fills immediately),
    skip the second leg (simulated failure), then immediately market-close the filled leg
    as the recovery action.  Verifies the emergency close path works and times each step.

    Returns (success: bool, detail_str: str).
    detail_str is always self-explanatory — times each sub-step on pass, explains error on fail.
    """
    # LONG_SPREAD: buy spot + sell futures.  SHORT_SPREAD: sell spot + buy futures.
    if order_type == "LONG_SPREAD":
        spot_side, futures_side = "BUY", "SELL"
    else:  # SHORT_SPREAD
        spot_side, futures_side = "SELL", "BUY"

    is_spot_filled = (filled_leg == "SPOT")
    open_market    = "SPOT"    if is_spot_filled else "FUTURES"
    open_side      = spot_side if is_spot_filled else futures_side
    fail_market    = "FUTURES" if is_spot_filled else "SPOT"
    fail_side      = futures_side if is_spot_filled else spot_side

    adapter  = engine.spot_adapter    if is_spot_filled else engine.futures_adapter
    symbol   = config.spot_symbol     if is_spot_filled else config.futures_symbol
    tick     = engine.spot_tick       if is_spot_filled else engine.futures_tick

    # pos_side is only used for futures legs
    pos_side: Optional[str] = None
    if not is_spot_filled:
        pos_side = "long" if futures_side == "BUY" else "short"

    if not adapter or not tick:
        return False, f"No {open_market} adapter or price data"

    t_total_start = datetime.now(timezone.utc)

    # ── Step 1: Open the filled leg at MARKET ─────────────────────────────────
    # Cross-margin SPOT MARKET BUY requires sz in USDT notional (not BTC qty).
    notional = round(quantity * tick.mid, 2) if (is_spot_filled and open_side == "BUY") else None

    t_open_start = datetime.now(timezone.utc)
    open_result = await adapter.place_order(
        symbol=symbol, side=open_side, order_type="MARKET",
        quantity=quantity, pos_side=pos_side, notional_usdt=notional,
    )
    t_open_ms = int((datetime.now(timezone.utc) - t_open_start).total_seconds() * 1000)

    if not open_result.success:
        return False, (
            f"Leg 1 ({open_market} {open_side} MARKET) FAILED in {t_open_ms}ms: {open_result.error}  |  "
            f"Leg 2 ({fail_market} {fail_side}) never placed (leg 1 failed first)"
        )

    oid1_short = (open_result.order_id or "?")[:16]

    # ── Step 2: Wait 1 s then fetch actual filled qty ─────────────────────────
    await asyncio.sleep(1)

    filled_qty = quantity
    fill_price = tick.mid
    if open_result.order_id:
        status = await adapter.get_order_status(symbol, open_result.order_id)
        if status:
            filled_qty = status.get("filled_qty") or quantity
            fill_price = status.get("filled_price") or tick.mid

    detail = (
        f"Leg 1 ({open_market} {open_side}) filled {filled_qty:.6f} BTC "
        f"@ ${fill_price:.2f} in {t_open_ms}ms [oid={oid1_short}…]  |  "
        f"Leg 2 ({fail_market} {fail_side}) SIMULATED FAILURE — not placed"
    )

    # ── Step 3: Recovery — market-close the filled leg immediately ────────────
    close_side     = "SELL" if open_side == "BUY" else "BUY"
    close_tick     = engine.spot_tick if is_spot_filled else engine.futures_tick
    # Cross-margin SPOT MARKET BUY (closing a short spot) needs sz in USDT.
    close_notional = round(filled_qty * close_tick.mid, 2) if (is_spot_filled and close_side == "BUY") else None

    t_close_start = datetime.now(timezone.utc)
    close_result = await adapter.place_order(
        symbol=symbol, side=close_side, order_type="MARKET",
        quantity=filled_qty, pos_side=pos_side,
        reduce_only=(not is_spot_filled),
        notional_usdt=close_notional,
    )
    t_close_ms = int((datetime.now(timezone.utc) - t_close_start).total_seconds() * 1000)
    t_total_ms = int((datetime.now(timezone.utc) - t_total_start).total_seconds() * 1000)

    if not close_result.success:
        return False, (
            detail + f"  |  Recovery ({open_market} {close_side} MARKET) FAILED in {t_close_ms}ms: "
            f"{close_result.error}  — ORPHAN RISK: filled leg not closed (taker cost unavoidable)"
        )

    oid2_short = (close_result.order_id or "?")[:16]
    detail += (
        f"  |  Recovery ({open_market} {close_side} MARKET) closed in {t_close_ms}ms "
        f"[oid={oid2_short}…]  |  Total: {t_total_ms}ms"
    )
    return True, detail


async def run_test_suite():
    """
    Full 40-scenario test suite (18 LIMIT + 18 MARKET + 4 partial-fill recovery).
    Runs entirely in the async event loop so it can await adapter calls without
    blocking Flask.  Emits 'test_suite_update' WebSocket events after every
    state change so the UI stays in sync.

    Timing per scenario:
      LIMIT  open: limit_timeout s wait + 20 s cooldown → ~13-15 min for 18 LIMIT
      MARKET open: 4 s wait          +  5 s cooldown → ~3 min for 18 MARKET
    Cancel/quick-close scenarios always close after 3 s.
    """
    global _test_suite_cancel, _test_suite_running, _test_suite_state, test_positions
    import copy

    _test_suite_running = True
    _test_suite_cancel  = False

    order_mode     = config.entry_execution_mode  # default; per-scenario forced_mode may override
    limit_timeout  = config.limit_order_timeout_sec

    scenarios = copy.deepcopy(_SUITE_SCENARIOS)
    for s in scenarios:
        s['status'] = 'pending'
        s['detail'] = ''
        s['mode']   = order_mode

    _test_suite_state = {
        'running':    True,
        'current':    0,
        'total':      len(scenarios),
        'pass':       0,
        'fail':       0,
        'scenarios':  scenarios,
        'start_time': datetime.now(timezone.utc).isoformat(),
        'order_mode': order_mode,
        'single_running': False,
    }
    socketio.emit('test_suite_update', _test_suite_state)

    spot_price = engine.spot_tick.mid if engine.spot_tick else 65000.0
    # Ensure quantity is large enough for at least 1 futures contract.
    # BTC-USDT-SWAP ctVal = 0.01 BTC → $100 notional at $98k is only 0.1 contracts (below min 1).
    futures_info = await engine.futures_adapter.get_symbol_info(config.futures_symbol) if engine.futures_adapter else None
    ct_val = futures_info.get("contract_val", 0.01) if futures_info else 0.01
    quantity = max(100.0 / spot_price, ct_val)  # at least 1 contract worth of BTC

    for idx, scenario in enumerate(scenarios):
        if _test_suite_cancel:
            scenario['status'] = 'cancelled'
            scenario['detail'] = 'suite stopped'
            break

        scenario['status'] = 'running'
        _test_suite_state['current'] = idx + 1
        order_type   = scenario['order_type']
        cancel_test  = scenario['cancel_test']
        scen_mode    = scenario.get('forced_mode') or order_mode  # per-scenario override
        scenario['mode'] = scen_mode  # ensure Mode column is always populated
        inter_pause  = 5 if scen_mode == 'MARKET' else 20
        socketio.emit('test_suite_update', _test_suite_state)
        logger.info("[TEST SUITE] %d/%d  %s  [%s]",
                    idx + 1, len(scenarios), scenario['label'], scen_mode)

        # ── PARTIAL-FILL RECOVERY TEST ─────────────────────────────────────
        # Places one leg at MARKET (fills), skips the other (simulated failure),
        # then immediately market-closes the filled leg — testing the recovery path.
        if scenario.get('partial_fail_test'):
            filled_leg = scenario['filled_leg']
            scenario['detail'] = f"opening {filled_leg} leg at MARKET…"
            socketio.emit('test_suite_update', _test_suite_state)
            try:
                ok, detail = await asyncio.wait_for(
                    _suite_partial_fill_test(order_type, quantity, filled_leg),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                ok, detail = False, "partial-fill test timed out (>60 s) — exchange unresponsive"
            except Exception as exc:
                ok, detail = False, f"unexpected error: {exc}"
            scenario['status'] = 'pass' if ok else 'fail'
            scenario['detail'] = detail
            _test_suite_state['pass' if ok else 'fail'] += 1
            socketio.emit('test_suite_update', _test_suite_state)
            logger.info("[TEST SUITE] %s  %s  %s", scenario['label'], scenario['status'].upper(), detail)
            if idx < len(scenarios) - 1 and not _test_suite_cancel:
                scenario['detail'] += f"  |  cooling {inter_pause} s…"
                socketio.emit('test_suite_update', _test_suite_state)
                for _ in range(inter_pause):
                    if _test_suite_cancel:
                        break
                    await asyncio.sleep(1)
            continue

        # ── OPEN ──────────────────────────────────────────────────────────
        try:
            legs, open_err = await asyncio.wait_for(
                _suite_open_order(order_type, quantity, forced_mode=scen_mode),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            open_err = "open timed out (>30 s)"
            legs = None
        except Exception as exc:
            open_err = str(exc)
            legs = None

        if open_err and not legs:
            # Complete failure — nothing was placed, nothing to close
            scenario['status'] = 'fail'
            scenario['detail'] = f"open failed: {open_err}"
            _test_suite_state['fail'] += 1
            socketio.emit('test_suite_update', _test_suite_state)
            logger.warning("[TEST SUITE] %s FAIL open: %s", scenario['label'], open_err)
            await asyncio.sleep(inter_pause)
            continue

        if open_err and legs:
            # Partial success — first leg placed, second leg failed.
            # Register the placed leg, show its fill/drift, close it, then mark fail.
            opened_ids        = []
            open_detail_parts = []
            for (mtype, side, entry_px, res, qty, ps, place_ms, lp,
                 _bid_o, _ask_o, sprd_o, z_o, std_o, mean_o) in legs:
                pos_id = str(uuid.uuid4())[:8]
                test_positions[pos_id] = {
                    'id': pos_id, 'market_type': mtype, 'side': side,
                    'quantity': qty, 'entry_price': entry_px,
                    'order_id': res.order_id,
                    'entry_time': datetime.now(timezone.utc).isoformat(),
                    'pos_side': ps,
                    'target_price':      lp,
                    'open_mode':         scen_mode,
                    'bid_at_open':       _bid_o,
                    'ask_at_open':       _ask_o,
                    'spread_at_open':    sprd_o,
                    'zscore_at_open':    z_o,
                    'std_at_open':       std_o,
                    'mean_at_open':      mean_o,
                    'leg_label':         f"{mtype} {side}",
                }
                opened_ids.append(pos_id)
                oid_short = (res.order_id or '?')[:12]
                if lp is not None:
                    open_detail_parts.append(
                        f"{mtype} {side} LIMIT @ ${lp:.2f} placed in {place_ms}ms [oid={oid_short}…]"
                    )
                else:
                    open_detail_parts.append(
                        f"{mtype} {side} MARKET @ ~${entry_px:.2f} placed in {place_ms}ms [oid={oid_short}…]"
                    )
            open_detail_parts.append(f"2nd leg FAILED: {open_err}")
            scenario['detail'] = "  |  ".join(open_detail_parts)
            socketio.emit('test_suite_update', _test_suite_state)
            close_details = []
            for pos_id in opened_ids:
                try:
                    _, cd = await asyncio.wait_for(_suite_close_position(pos_id), timeout=30.0)
                    close_details.append(cd)
                except asyncio.TimeoutError:
                    close_details.append("close timed out (>30 s)")
                except Exception as exc:
                    close_details.append(str(exc))
            if close_details:
                scenario['detail'] += "  |  " + "  |  ".join(close_details)
            scenario['status'] = 'fail'
            _test_suite_state['fail'] += 1
            socketio.emit('test_suite_update', _test_suite_state)
            logger.warning("[TEST SUITE] %s FAIL partial open: %s", scenario['label'], scenario['detail'])
            await asyncio.sleep(inter_pause)
            continue

        # Register positions in global test_positions (same dict the UI reads)
        opened_ids  = []
        open_detail_parts = []
        for (mtype, side, entry_px, result, qty, ps, place_ms, lp,
             _bid_o, _ask_o, sprd_o, z_o, std_o, mean_o) in legs:
            pos_id = str(uuid.uuid4())[:8]
            test_positions[pos_id] = {
                'id': pos_id, 'market_type': mtype, 'side': side,
                'quantity': qty, 'entry_price': entry_px,
                'order_id': result.order_id,
                'entry_time': datetime.now(timezone.utc).isoformat(),
                'pos_side': ps,
                'target_price':      lp,
                'open_mode':         scen_mode,
                'bid_at_open':       _bid_o,
                'ask_at_open':       _ask_o,
                'spread_at_open':    sprd_o,
                'zscore_at_open':    z_o,
                'std_at_open':       std_o,
                'mean_at_open':      mean_o,
                'leg_label':         f"{mtype} {side}",
            }
            opened_ids.append(pos_id)
            oid_short = (result.order_id or '?')[:12]
            if lp is not None:
                open_detail_parts.append(
                    f"{mtype} {side} LIMIT @ ${lp:.2f} placed in {place_ms}ms [oid={oid_short}…]"
                )
            else:
                open_detail_parts.append(
                    f"{mtype} {side} MARKET @ ~${entry_px:.2f} placed in {place_ms}ms [oid={oid_short}…]"
                )

        scenario['detail'] = "  |  ".join(open_detail_parts)
        socketio.emit('test_suite_update', _test_suite_state)

        # ── WAIT ──────────────────────────────────────────────────────────
        if cancel_test:
            # LIMIT cancel: cancels unfilled order.  MARKET quick-close: closes filled position.
            label = "cancel test" if scen_mode == "LIMIT" else "quick-close"
            scenario['detail'] += f"  |  {label} – closing in 3 s"
            socketio.emit('test_suite_update', _test_suite_state)
            await asyncio.sleep(3)
        elif scen_mode == "LIMIT":
            # Give the limit order a real chance to fill
            scenario['detail'] += f"  |  waiting {limit_timeout} s for fill…"
            socketio.emit('test_suite_update', _test_suite_state)
            await asyncio.sleep(limit_timeout)
        else:
            # Market order – small wait for exchange confirmation
            await asyncio.sleep(4)

        if _test_suite_cancel:
            scenario['status'] = 'cancelled'
            scenario['detail'] += '  |  suite stopped mid-scenario'
            break

        # ── CLOSE ─────────────────────────────────────────────────────────
        close_ok      = True
        close_details = []
        for pos_id in opened_ids:
            try:
                ok, detail = await asyncio.wait_for(
                    _suite_close_position(pos_id),
                    timeout=30.0,
                )
                close_details.append(detail)
                if not ok:
                    close_ok = False
            except asyncio.TimeoutError:
                close_details.append("close timed out (>30 s)")
                close_ok = False
            except Exception as exc:
                close_details.append(str(exc))
                close_ok = False

        detail_str = "  |  ".join(close_details)
        if close_ok:
            scenario['status'] = 'pass'
            scenario['detail'] = detail_str
            _test_suite_state['pass'] += 1
            logger.info("[TEST SUITE] %s  PASS  %s", scenario['label'], detail_str)
        else:
            scenario['status'] = 'fail'
            scenario['detail'] = detail_str
            _test_suite_state['fail'] += 1
            logger.warning("[TEST SUITE] %s  FAIL  %s", scenario['label'], detail_str)

        socketio.emit('test_suite_update', _test_suite_state)

        # ── INTER-SCENARIO COOLDOWN ────────────────────────────────────────
        if idx < len(scenarios) - 1 and not _test_suite_cancel:
            scenario['detail'] += f"  |  cooling {inter_pause} s…"
            socketio.emit('test_suite_update', _test_suite_state)
            # Sleep in 1-second slices so we can react to cancellation quickly
            for _ in range(inter_pause):
                if _test_suite_cancel:
                    break
                await asyncio.sleep(1)

    # ── always reached, even if an exception short-circuits the loop ──────
    _test_suite_state['running']  = False
    _test_suite_running           = False
    socketio.emit('test_suite_update', _test_suite_state)
    logger.info("[TEST SUITE] Done – pass=%d  fail=%d",
                _test_suite_state['pass'], _test_suite_state['fail'])


@app.route('/api/test-suite/start', methods=['POST'])
def start_test_suite():
    """Start the full 36-scenario test suite (18 LIMIT + 18 MARKET) in the background."""
    global _test_suite_running
    # Auto-clear stale flag: coroutine may have crashed without resetting it
    if _test_suite_running and not _test_suite_state.get('running'):
        logger.warning("[TEST SUITE] Clearing stale _test_suite_running flag")
        _test_suite_running = False
    if _test_suite_running:
        return jsonify({'success': False, 'error': 'Suite already running'}), 400
    if not engine.spot_adapter:
        return jsonify({'success': False, 'error': 'No exchange connected'}), 400
    if not engine.spot_tick or not engine.futures_tick:
        return jsonify({'success': False, 'error': 'No price data – wait for connection'}), 400
    if loop:
        asyncio.run_coroutine_threadsafe(run_test_suite(), loop)
        return jsonify({'success': True, 'message': 'Test suite started'})
    return jsonify({'success': False, 'error': 'Event loop not running'}), 500


@app.route('/api/test-suite/stop', methods=['POST'])
def stop_test_suite():
    """Cancel the running test suite after the current scenario finishes."""
    global _test_suite_cancel
    _test_suite_cancel = True
    return jsonify({'success': True, 'message': 'Stop signal sent'})


@app.route('/api/test-suite/reset', methods=['POST'])
def reset_test_suite():
    """Force-clear all running flags (use when suite is stuck due to a crash)."""
    global _test_suite_running, _single_running, _test_suite_cancel
    _test_suite_running = False
    _single_running     = False
    _test_suite_cancel  = False
    _test_suite_state['running']        = False
    _test_suite_state['single_running'] = False
    socketio.emit('test_suite_update', _test_suite_state)
    logger.warning("[TEST SUITE] Force-reset by user")
    return jsonify({'success': True, 'message': 'Suite state reset'})


@app.route('/api/test-suite/status', methods=['GET'])
def get_test_suite_status():
    """Return the current test suite state.
    If no suite has run yet, pre-populate scenarios so the UI can show Run buttons."""
    state = dict(_test_suite_state)
    if not state.get('scenarios'):
        state['scenarios'] = [
            {**s, 'status': 'pending', 'detail': '', 'mode': s.get('forced_mode', config.entry_execution_mode)}
            for s in _SUITE_SCENARIOS
        ]
    return jsonify(state)


# ─────────────────────────────────────────────────────────────────────────────
# Single-scenario runner
# ─────────────────────────────────────────────────────────────────────────────

async def run_single_scenario_task(scenario_id: str):
    """Run one scenario by ID, emitting live WebSocket updates like the full suite."""
    global _single_running, _test_suite_state, test_positions

    _single_running = True
    _test_suite_state['single_running'] = True

    try:
        scenario_def = next((s for s in _SUITE_SCENARIOS if s['id'] == scenario_id), None)
        if not scenario_def:
            logger.error("[SINGLE] Scenario %s not found", scenario_id)
            return

        # Ensure state has a scenarios list so the UI row can be updated
        if not _test_suite_state.get('scenarios'):
            _test_suite_state['scenarios'] = [
                {**s, 'status': 'pending', 'detail': '', 'mode': s.get('forced_mode', config.entry_execution_mode)}
                for s in _SUITE_SCENARIOS
            ]

        scen_idx = next(
            (i for i, s in enumerate(_test_suite_state['scenarios']) if s['id'] == scenario_id),
            None,
        )
        if scen_idx is None:
            logger.error("[SINGLE] Scenario %s missing from state list", scenario_id)
            return

        scenario = _test_suite_state['scenarios'][scen_idx]

        # Quantity: same logic as full suite
        spot_price = engine.spot_tick.mid if engine.spot_tick else 0
        if not spot_price:
            scenario['status'] = 'fail'
            scenario['detail'] = 'No spot price available'
            socketio.emit('test_suite_update', _test_suite_state)
            return

        symbol_info = None
        if engine.futures_adapter:
            symbol_info = await engine.futures_adapter.get_symbol_info(config.futures_symbol)
        ct_val   = float(symbol_info.get('ct_val', 0.01)) if symbol_info else 0.01
        quantity = max(100.0 / spot_price, ct_val)

        order_mode    = config.entry_execution_mode
        limit_timeout = config.limit_order_timeout_sec
        scen_mode     = scenario_def.get('forced_mode') or order_mode
        scenario['mode'] = scen_mode  # ensure Mode column is always populated
        cancel_test   = scenario_def.get('cancel_test', False)

        # Mark running
        scenario['status'] = 'running'
        scenario['detail'] = ''
        socketio.emit('test_suite_update', _test_suite_state)
        logger.info("[SINGLE] %s [%s]", scenario['label'], scen_mode)

        # Open
        try:
            legs, open_err = await asyncio.wait_for(
                _suite_open_order(scenario_def['order_type'], quantity, forced_mode=scen_mode),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            open_err = "open timed out (>30 s)"
            legs = None
        except Exception as exc:
            open_err = str(exc)
            legs = None

        if open_err and not legs:
            # Complete failure — nothing was placed, nothing to close
            scenario['status'] = 'fail'
            scenario['detail'] = f"open failed: {open_err}"
            socketio.emit('test_suite_update', _test_suite_state)
            return

        if open_err and legs:
            # Partial success — first leg placed, second leg failed.
            # Register the placed leg, show its fill/drift, close it, then mark fail.
            opened_ids        = []
            open_detail_parts = []
            for (mtype, side, entry_px, res, qty, ps, place_ms, lp,
                 _bid_o, _ask_o, sprd_o, z_o, std_o, mean_o) in legs:
                pos_id = str(uuid.uuid4())[:8]
                test_positions[pos_id] = {
                    'id': pos_id, 'market_type': mtype, 'side': side,
                    'quantity': qty, 'entry_price': entry_px,
                    'order_id': res.order_id,
                    'entry_time': datetime.now(timezone.utc).isoformat(),
                    'pos_side': ps,
                    'target_price':      lp,
                    'open_mode':         scen_mode,
                    'bid_at_open':       _bid_o,
                    'ask_at_open':       _ask_o,
                    'spread_at_open':    sprd_o,
                    'zscore_at_open':    z_o,
                    'std_at_open':       std_o,
                    'mean_at_open':      mean_o,
                    'leg_label':         f"{mtype} {side}",
                }
                opened_ids.append(pos_id)
                oid_short = (res.order_id or '?')[:12]
                if lp is not None:
                    open_detail_parts.append(
                        f"{mtype} {side} LIMIT @ ${lp:.2f} placed in {place_ms}ms [oid={oid_short}…]"
                    )
                else:
                    open_detail_parts.append(
                        f"{mtype} {side} MARKET @ ~${entry_px:.2f} placed in {place_ms}ms [oid={oid_short}…]"
                    )
            open_detail_parts.append(f"2nd leg FAILED: {open_err}")
            scenario['detail'] = "  |  ".join(open_detail_parts)
            socketio.emit('test_suite_update', _test_suite_state)
            close_details = []
            for pos_id in opened_ids:
                try:
                    _, cd = await asyncio.wait_for(_suite_close_position(pos_id), timeout=30.0)
                    close_details.append(cd)
                except asyncio.TimeoutError:
                    close_details.append("close timed out (>30 s)")
                except Exception as exc:
                    close_details.append(str(exc))
            if close_details:
                scenario['detail'] += "  |  " + "  |  ".join(close_details)
            scenario['status'] = 'fail'
            socketio.emit('test_suite_update', _test_suite_state)
            logger.warning("[SINGLE] %s FAIL partial open: %s", scenario['label'], scenario['detail'])
            return

        # Register positions
        opened_ids         = []
        open_detail_parts  = []
        for (mtype, side, entry_px, result, qty, ps, place_ms, lp,
             _bid_o, _ask_o, sprd_o, z_o, std_o, mean_o) in legs:
            pos_id = str(uuid.uuid4())[:8]
            test_positions[pos_id] = {
                'id': pos_id, 'market_type': mtype, 'side': side,
                'quantity': qty, 'entry_price': entry_px,
                'order_id': result.order_id,
                'entry_time': datetime.now(timezone.utc).isoformat(),
                'pos_side': ps,
                'target_price':      lp,
                'open_mode':         scen_mode,
                'bid_at_open':       _bid_o,
                'ask_at_open':       _ask_o,
                'spread_at_open':    sprd_o,
                'zscore_at_open':    z_o,
                'std_at_open':       std_o,
                'mean_at_open':      mean_o,
                'leg_label':         f"{mtype} {side}",
            }
            opened_ids.append(pos_id)
            oid_short = (result.order_id or '?')[:12]
            if lp is not None:
                open_detail_parts.append(
                    f"{mtype} {side} LIMIT @ ${lp:.2f} placed in {place_ms}ms [oid={oid_short}…]"
                )
            else:
                open_detail_parts.append(
                    f"{mtype} {side} MARKET @ ~${entry_px:.2f} placed in {place_ms}ms [oid={oid_short}…]"
                )

        scenario['detail'] = "  |  ".join(open_detail_parts)
        socketio.emit('test_suite_update', _test_suite_state)

        # Wait
        if cancel_test:
            label = "cancel test" if scen_mode == "LIMIT" else "quick-close"
            scenario['detail'] += f"  |  {label} – closing in 3 s"
            socketio.emit('test_suite_update', _test_suite_state)
            await asyncio.sleep(3)
        elif scen_mode == "LIMIT":
            scenario['detail'] += f"  |  waiting {limit_timeout} s for fill…"
            socketio.emit('test_suite_update', _test_suite_state)
            await asyncio.sleep(limit_timeout)
        else:
            await asyncio.sleep(4)

        # Close
        close_ok      = True
        close_details = []
        for pos_id in opened_ids:
            try:
                ok, detail = await asyncio.wait_for(
                    _suite_close_position(pos_id),
                    timeout=30.0,
                )
                close_details.append(detail)
                if not ok:
                    close_ok = False
            except asyncio.TimeoutError:
                close_details.append("close timed out (>30 s)")
                close_ok = False
            except Exception as exc:
                close_details.append(str(exc))
                close_ok = False

        detail_str = "  |  ".join(close_details)
        if close_ok:
            scenario['status'] = 'pass'
            scenario['detail'] = detail_str
            logger.info("[SINGLE] %s  PASS  %s", scenario['label'], detail_str)
        else:
            scenario['status'] = 'fail'
            scenario['detail'] = detail_str
            logger.warning("[SINGLE] %s  FAIL  %s", scenario['label'], detail_str)

        socketio.emit('test_suite_update', _test_suite_state)

    finally:
        _single_running = False
        _test_suite_state['single_running'] = False
        socketio.emit('test_suite_update', _test_suite_state)


@app.route('/api/test-suite/run-scenario', methods=['POST'])
def api_run_single_scenario():
    """Run a single test scenario by ID without starting the full suite."""
    global _single_running
    if _test_suite_running:
        return jsonify({'success': False, 'error': 'Full suite is running'}), 400
    if _single_running:
        return jsonify({'success': False, 'error': 'A scenario is already running'}), 400
    if not engine.spot_adapter:
        return jsonify({'success': False, 'error': 'No exchange connected'}), 400
    if not engine.spot_tick or not engine.futures_tick:
        return jsonify({'success': False, 'error': 'No price data – wait for connection'}), 400

    data        = request.json or {}
    scenario_id = data.get('scenario_id')
    if not scenario_id:
        return jsonify({'success': False, 'error': 'scenario_id required'}), 400
    if not any(s['id'] == scenario_id for s in _SUITE_SCENARIOS):
        return jsonify({'success': False, 'error': f'Unknown scenario: {scenario_id}'}), 400

    if loop:
        asyncio.run_coroutine_threadsafe(run_single_scenario_task(scenario_id), loop)
        return jsonify({'success': True, 'scenario_id': scenario_id})
    return jsonify({'success': False, 'error': 'Event loop not running'}), 500


@app.route('/api/test-suite/download-csv', methods=['GET'])
def download_test_suite_csv():
    """Download the last test suite results as a CSV file."""
    import csv, io
    scenarios = _test_suite_state.get('scenarios', [])
    if not scenarios:
        return jsonify({'error': 'No test results available yet'}), 404

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['#', 'Scenario', 'Mode', 'Type', 'Cancel Test', 'Status', 'Detail'])
    for i, s in enumerate(scenarios, 1):
        writer.writerow([
            i,
            s.get('label', ''),
            s.get('mode', ''),
            s.get('order_type', ''),
            'yes' if s.get('cancel_test') else 'no',
            s.get('status', ''),
            s.get('detail', ''),
        ])

    from flask import Response
    ts = _test_suite_state.get('start_time', 'unknown')
    filename = f"test_suite_{ts[:10] if ts else 'results'}.csv"
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@app.route('/api/reset-trades', methods=['POST'])
def reset_trades_only():
    """Reset only trades and SD analysis - preserves spread data collection."""
    data = request.json or {}
    asset = data.get('asset')

    # Clear only trades and SD touches, keep spread history
    trades_deleted = db.clear_trades(asset=asset)
    sd_deleted = db.clear_sd_touches(asset=asset)
    signals_deleted = db.clear_signal_log(asset=asset)

    # Clear SD touch events from signal generator memory but keep spread data
    engine.signal_generator.sd_touch_events.clear()
    engine.signal_generator.last_sd_level = 0.0

    # Reset position state but keep spread history
    engine.state.current_position = "NONE"
    engine.signal_generator.set_position("NONE")
    engine.open_trade = None

    logger.info("Trades/SD reset: trades=%d, sd_touches=%d, signals=%d (spread preserved)",
               trades_deleted, sd_deleted, signals_deleted)

    return jsonify({
        'success': True,
        'deleted': {
            'trades': trades_deleted,
            'sd_touches': sd_deleted,
            'signals': signals_deleted,
        },
        'spread_preserved': True,
    })


@app.route('/api/reset-all', methods=['POST'])
def reset_all():
    """Reset everything - trades, SD touches, spread history, and engine state."""
    data = request.json or {}
    asset = data.get('asset')

    # Clear database
    trades_deleted = db.clear_trades(asset=asset)
    sd_deleted = db.clear_sd_touches(asset=asset)
    signals_deleted = db.clear_signal_log(asset=asset)
    spread_deleted = db.clear_spread_history(asset=asset)

    # Reset engine
    engine.reset()

    return jsonify({
        'success': True,
        'deleted': {
            'trades': trades_deleted,
            'sd_touches': sd_deleted,
            'signals': signals_deleted,
            'spread_history': spread_deleted,
        }
    })


def create_adapter(exchange: Exchange, is_futures: bool = False):
    """Create exchange adapter based on type."""
    if exchange.exchange_type.lower() == 'okx':
        return OKXAdapter(
            api_key=exchange.api_key,
            secret_key=exchange.secret_key,
            passphrase=exchange.passphrase,
            is_testnet=exchange.is_testnet,
        )
    elif exchange.exchange_type.lower() == 'binance':
        return BinanceAdapter(
            api_key=exchange.api_key,
            secret_key=exchange.secret_key,
            is_testnet=exchange.is_testnet,
            is_futures=is_futures,
        )
    elif exchange.exchange_type.lower() == 'bybit':
        return BybitAdapter(
            api_key=exchange.api_key,
            secret_key=exchange.secret_key,
            is_testnet=exchange.is_testnet,
            is_futures=is_futures,
        )
    return None


# SocketIO Events
@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    logger.debug("Client connected")
    emit('status', engine.get_status())


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    logger.debug("Client disconnected")


@socketio.on('get_status')
def handle_get_status():
    """Handle status request."""
    emit('status', engine.get_status())


@socketio.on('toggle_algo')
def handle_toggle_algo(data):
    """Handle algo toggle via WebSocket."""
    enabled = data.get('enabled', False)
    engine.toggle_algo(enabled)
    emit('status', engine.get_status(), broadcast=True)


# Start engine on app start
@app.before_request
def ensure_engine_started():
    """Ensure engine is started before handling requests."""
    global loop
    if loop is None:
        start_engine_loop()


if __name__ == '__main__':
    # ALWAYS start engine before Flask starts serving requests
    # This ensures spread history is loaded and engine is ready
    start_engine_loop()

    # Wait for engine to fully initialize (including API calls)
    logger.info("Waiting for engine initialization to complete...")
    time.sleep(1.0)

    # Log the server address
    port = int(os.getenv("PORT", 5002))
    logger.info("=" * 50)
    logger.info("Dashboard available at: http://localhost:%d", port)
    logger.info("=" * 50)

    # Suppress HTTP request logs
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    logging.getLogger('engineio').setLevel(logging.ERROR)
    logging.getLogger('socketio').setLevel(logging.ERROR)
    app.logger.setLevel(logging.WARNING)

    # Run Flask app with SocketIO (threading mode)
    # use_reloader=False and debug=False for stable single-process operation
    socketio.run(
        app,
        host='0.0.0.0',
        port=port,
        debug=False,  # Disable debug mode for production stability
        use_reloader=False,
        allow_unsafe_werkzeug=True
    )
