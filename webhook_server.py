"""
Claude-Market Webhook Server v8.3
Lukas Ferreira - Pretoria ZA
MCAPI — CLOSE REQUEST FROM FORENSICS
v8.3: /close-request POST — Forensics page sends ticket to close
      /close-request GET  — EA polls every 10s, closes position if pending
      Manual trades can now be closed directly from Engine 10 page
"""

from flask import Flask, request, jsonify
import anthropic
import threading
import time
import json
import urllib.request
from datetime import datetime, timedelta
import os
try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    print("[DB] psycopg2 not installed — add psycopg2-binary to requirements.txt")

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE — Persistent Trade History
# Uses Render free PostgreSQL. Survives every deploy and restart.
# URL stored in environment variable DATABASE_URL — never hardcoded.
# ══════════════════════════════════════════════════════════════════════════════
DB_URL = os.environ.get("DATABASE_URL", "")

def _db_connect():
    """Open a database connection. Returns None if DB not configured."""
    if not DB_URL or not PSYCOPG2_AVAILABLE:
        return None
    try:
        return psycopg2.connect(DB_URL)
    except Exception as e:
        print(f"[DB] Connection failed: {e}")
        return None

def init_db():
    """Create tables on startup if they don't exist."""
    conn = _db_connect()
    if not conn:
        print("[DB] No database — trade history stored in memory only (lost on restart)")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                ticket      TEXT PRIMARY KEY,
                symbol      TEXT,
                trade_type  TEXT,
                profit      REAL,
                data        TEXT,
                received_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS deleted_tickets (
                ticket      TEXT PRIMARY KEY,
                deleted_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brain_config (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brain_daily_report (
                id         SERIAL PRIMARY KEY,
                report     TEXT,
                generated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("[DB] ✅ Tables ready — trade_history + deleted_tickets + brain_config + brain_daily_report")
    except Exception as e:
        print(f"[DB] Init error: {e}")

def load_trades_from_db():
    """Load all persisted trades into memory on startup."""
    global trade_history, deleted_tickets
    conn = _db_connect()
    if not conn:
        return
    try:
        cur = conn.cursor()
        # Load deleted tickets first
        cur.execute("SELECT ticket FROM deleted_tickets")
        deleted_tickets = {row[0] for row in cur.fetchall()}
        print(f"[DB] Loaded {len(deleted_tickets)} deleted ticket(s)")
        # Load trade history — skip deleted
        cur.execute("""
            SELECT data FROM trade_history
            WHERE ticket NOT IN (SELECT ticket FROM deleted_tickets)
            ORDER BY received_at DESC LIMIT 200
        """)
        rows = cur.fetchall()
        trade_history = []
        for row in rows:
            try:
                trade_history.append(json.loads(row[0]))
            except Exception:
                pass
        cur.close()
        conn.close()
        print(f"[DB] ✅ Loaded {len(trade_history)} trade(s) from database")
    except Exception as e:
        print(f"[DB] Load error: {e}")

def db_save_trade(trade):
    """Persist a single closed trade to database."""
    conn = _db_connect()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trade_history (ticket, symbol, trade_type, profit, data)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (ticket) DO UPDATE SET
                profit = EXCLUDED.profit,
                data   = EXCLUDED.data
        """, (
            str(trade.get("ticket", "")),
            trade.get("symbol", ""),
            trade.get("type", ""),
            float(trade.get("profit", 0)),
            json.dumps(trade)
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[DB] Save trade error: {e}")

def db_delete_trade(ticket):
    """Mark a ticket as deleted so it never reappears after restart."""
    conn = _db_connect()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO deleted_tickets (ticket) VALUES (%s) ON CONFLICT DO NOTHING",
            (str(ticket),)
        )
        cur.execute(
            "DELETE FROM trade_history WHERE ticket = %s",
            (str(ticket),)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[DB] Delete trade error: {e}")

# ─── CORS — allow Command Centre to fetch from any origin ─────────────────────
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.route("/", methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def handle_options(path=""):
    return "", 204

# ─── State ────────────────────────────────────────────────────────────────────
pending_signal   = None          # Signal waiting for EA to pick up
close_requests   = []            # [{ticket, symbol, timestamp}] — FIFO queue
close_signal     = None          # Close signal for EA (future auto-close)
trading_enabled  = True          # Kill switch
mt5_status       = {}            # Latest data posted by EA
trade_history    = []            # Closed trades from EA (last 200)
deleted_tickets  = set()         # v5.5: tickets the user deleted — EA cannot re-add these
scan_results     = {             # Scanner intelligence data
    "last_run": None,
    "next_run": None,
    "stage1": {},
    "stage2": {},
    "global_winner": None,
    "history": [],
    "regimes": {}                # Engine 1: regime per symbol
}
recently_traded  = {}  # {symbol: datetime} — 6-hour expiry rotation guard
scan_lock        = threading.Lock()
symbol_regimes   = {}  # Engine 1: {symbol: "BULLISH"/"NEUTRAL"/"BEARISH"}
bb_data          = {}  # Engine 6: {symbol: {upper,middle,lower,price,location,range}}

# ══════════════════════════════════════════════════════════════════════════════
# ENGINE 5 — VOLATILITY
# EA sends H1 ATR(14) per symbol in every /status POST
# Webhook gates signals when volatility is too HIGH (spike/news)
# or too LOW (dead market — no movement, spread too costly)
# ATR evaluated as % of current price — works for all symbols and price levels
# ══════════════════════════════════════════════════════════════════════════════
volatility_data  = {}  # {symbol: {"atr": float, "atr_pct": float, "timestamp": str}}

# ══════════════════════════════════════════════════════════════════════════════
# ENGINE 9 — MEMORY
# Tracks win rate per symbol per regime per session from trade history.
# Adjusts scanner priority hints so proven setups are favoured.
# Score: +1 (strong track record) · 0 (neutral) · -1 (struggling)
# Requires ≥3 trades in a condition before scoring · fails open on sparse data
# Rebuilt from PostgreSQL trade history on every startup.
# ══════════════════════════════════════════════════════════════════════════════
memory_data = {}
# Structure: {SYMBOL: {REGIME: {SESSION: {wins,losses,total}}, overall: {wins,losses,total}}}

# ══════════════════════════════════════════════════════════════════════════════
# ENGINE 3 — CALENDAR / NEWS FILTER
# Source: ForexFactory weekly calendar (free, no API key needed)
# Blocks signals 30 min before HIGH impact events on symbol's currencies
# Blocks signals 15 min after HIGH impact events (volatility still elevated)
# Medium impact: blocks 15 min before only
# Passes through if calendar unavailable — never blocks on network error
# ══════════════════════════════════════════════════════════════════════════════
_news_calendar   = []          # Cached weekly events from ForexFactory
_news_last_fetch = None        # Last fetch timestamp
_NEWS_FETCH_URL  = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Which currencies drive each symbol — used to match news events
NEWS_CURRENCIES = {
    "USDZAR":    ["USD", "ZAR"],
    "EURUSD":    ["EUR", "USD"],
    "GOLD":      ["USD"],         # Gold priced in USD — Fed news moves it
    "SILVER":    ["USD"],
    "BTCUSD":    ["USD"],
    "ETHUSD":    ["USD"],
    "US_TECH100":["USD"],
    "US_500":    ["USD"],
    "US_30":     ["USD"],
}

# ══════════════════════════════════════════════════════════════════════════════
# ENGINE 7 — SESSION RISK STATE
# Tracks portfolio drawdown + per-symbol session losses
# Resets at 04:00 UTC (06:00 SAST) — Lukas's natural session start
# Per-symbol: session losses reset at symbol's natural session open
# Consecutive losses: reset only after a WIN on that symbol
# ══════════════════════════════════════════════════════════════════════════════
e7_risk = {
    "session_start_equity":   None,    # Equity at session start
    "session_start_time":     None,    # When session started
    "current_equity":         None,    # Last known equity from EA
    "portfolio_drawdown_pct": 0.0,     # Current drawdown from session start
    "portfolio_paused":       False,   # True when drawdown limit hit
    "symbol_session_losses":  {},      # {symbol: int} losses this session
    "symbol_consecutive":     {},      # {symbol: int} consecutive losses
    "symbol_paused":          {},      # {symbol: True/False}
    "last_session_reset":     None,    # Last reset timestamp
    "limits": {
        "max_drawdown_pct":   5.0,     # From Config Tab Engine 7
        "max_session_losses": 2,       # Per symbol per session
        "max_consecutive":    3,       # Consecutive losses before pause
        "reset_hour_utc":     4,       # 04:00 UTC = 06:00 SAST
    }
}

# ══════════════════════════════════════════════════════════════════════════════
# MCAPI ENGINE 6 — STRUCTURE: Location Check
# Before any signal fires, check if price is at the RIGHT level on BB
# Data comes from EA's /status POST (GetBBData function in EA v8.9)
# ══════════════════════════════════════════════════════════════════════════════
def _check_structure(symbol, action):
    """
    Engine 6: Is price at the right Bollinger Band location to enter?
    BUY:  price must be in lower zone (near lower BB)
    SELL: price must be in upper zone (near upper BB)
    Returns (allowed, reason)
    """
    data = bb_data.get(symbol.upper())
    if not data or not data.get("range"):
        return False, f"ENGINE6 BLOCK: No BB data for {symbol} — symbol not on active charts"

    location = float(data.get("location", 0.5))  # 0=lower, 0.5=mid, 1=upper
    price    = float(data.get("price", 0))
    upper    = float(data.get("upper", 0))
    lower    = float(data.get("lower", 0))
    middle   = float(data.get("middle", 0))
    bb_range = float(data.get("range", 0))

    p    = _get_personality(symbol)
    zone = p.get("location_zone", 0.33)

    if action == "BUY":
        if location <= zone:
            return True, (f"ENGINE6 ✅ GOOD LOCATION: {symbol} at {location:.1%} of BB range "
                         f"(lower {zone:.0%} zone) Price:{price:.2f} LowerBB:{lower:.2f}")
        else:
            pct_from_lower = (price - lower) / bb_range * 100
            return False, (f"ENGINE6 ❌ POOR LOCATION: {symbol} BUY at {location:.1%} "
                          f"({pct_from_lower:.0f}% from lower BB) — need ≤{zone:.0%} zone. "
                          f"Wait for price near {lower:.2f}")

    elif action == "SELL":
        if location >= (1.0 - zone):
            return True, (f"ENGINE6 ✅ GOOD LOCATION: {symbol} at {location:.1%} of BB range "
                         f"(upper {zone:.0%} zone) Price:{price:.2f} UpperBB:{upper:.2f}")
        else:
            pct_from_upper = (upper - price) / bb_range * 100
            return False, (f"ENGINE6 ❌ POOR LOCATION: {symbol} SELL at {location:.1%} "
                          f"({pct_from_upper:.0f}% from upper BB) — need ≥{(1-zone):.0%} zone. "
                          f"Wait for price near {upper:.2f}")

    return True, "ENGINE6: Unknown action — check passes"

# ─── Asset Universe — Ava broker symbol names ─────────────────────────────────
ASSET_GROUPS = {
    "Forex Majors":  ["EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","USDCAD","NZDUSD","EURGBP","EURJPY"],
    "Forex Minors":  ["GBPJPY","AUDJPY","CADJPY","CHFJPY","EURAUD","EURNZD","GBPAUD","AUDCAD","NZDCAD"],
    "Indices":       ["US_TECH100","US_500","US_30","GERMANY_40","UK_100","JAPAN_225","FRANCE_40"],
    "Commodities":   ["GOLD","SILVER","CrudeOIL","COPPER","USDZAR"],
    "Crypto":        ["BTCUSD","ETHUSD"],
    "SA & Emerging": ["USDZAR","EURZAR","GBPZAR"]
}

# ══════════════════════════════════════════════════════════════════════════════
# MCAPI ENGINE 4 — ASSET PERSONALITY
# Per-symbol settings that override global defaults
# Based on Sprint 0 data + market knowledge
# ══════════════════════════════════════════════════════════════════════════════
PERSONALITY = {
    # ── COMMODITIES ────────────────────────────────────────────────────────────
    "GOLD": {
        "blacklist":      False,
        "priority":       2,          # High — consistent performer
        "min_score":      3,
        "sessions":       ["London","New York"],
        "max_daily":      3,
        "sl_points":      25,
        "tp_ratio":       1.5,
        "trail_pct":      15,
        "location_zone":  0.33,       # Layer 6: lower/upper 33% of BB
        "notes":          "Tight SL. London/NY hours only. Good range trader."
    },
    "SILVER": {
        "blacklist":      False,
        "priority":       1,
        "min_score":      3,
        "sessions":       ["London","New York"],
        "max_daily":      2,
        "sl_points":      2.5,
        "tp_ratio":       1.5,
        "trail_pct":      15,
        "location_zone":  0.33,
        "notes":          "Volatile. Max 2/day. Needs structure check."
    },

    # ── CRYPTO ─────────────────────────────────────────────────────────────────
    "BTCUSD": {
        "blacklist":      False,
        "priority":       1,
        "min_score":      4,          # Higher bar — volatile, direction-sensitive
        "sessions":       ["New York"],
        "max_daily":      1,          # ONE BTC trade per day max
        "sl_points":      500,
        "tp_ratio":       2.0,        # Wider TP to match large SL
        "trail_pct":      10,         # Activate trail faster (10% not 20%)
        "location_zone":  0.25,       # Stricter location — outer 25% only
        "notes":          "Sprint 0: always hit SL. Regime check critical. One shot per day."
    },
    "ETHUSD": {
        "blacklist":      False,
        "priority":       1,
        "min_score":      3,
        "sessions":       ["New York"],
        "max_daily":      2,
        "sl_points":      30,
        "tp_ratio":       1.5,
        "trail_pct":      15,
        "location_zone":  0.33,
        "notes":          "Less volatile than BTC. Follows BTC direction."
    },

    # ── FOREX MAJORS ───────────────────────────────────────────────────────────
    "EURUSD": {
        "blacklist":      False,
        "priority":       2,          # v7.7: Raised to 2 — active scanner symbol
        "min_score":      3,
        "sessions":       ["London","New York"],
        "max_daily":      2,
        "sl_points":      30,
        "tp_ratio":       1.5,
        "trail_pct":      20,
        "location_zone":  0.33,
        "notes":          "Most liquid forex pair. London/NY peak hours. Clean BB signals."
    },
    "GBPUSD": {
        "blacklist":      False,
        "priority":       2,          # Sprint 0: current trade +$135, L3 trail active
        "min_score":      3,
        "sessions":       ["London","New York"],
        "max_daily":      2,
        "sl_points":      35,
        "tp_ratio":       1.5,
        "trail_pct":      20,
        "location_zone":  0.33,
        "notes":          "Volatile during London. Good BB range trader."
    },
    "USDJPY": {
        "blacklist":      False,
        "priority":       2,          # Sprint 0: +$40, Asian hours performer
        "min_score":      3,
        "sessions":       ["Asian","London"],
        "max_daily":      2,
        "sl_points":      35,
        "tp_ratio":       1.5,
        "trail_pct":      20,
        "location_zone":  0.35,
        "notes":          "Asian session specialist. Trends well."
    },
    "AUDUSD": {
        "blacklist":      True,        # ← BLOCKED: Sprint 0: 0% win rate, -$526
        "blacklist_reason": "Sprint 0: 0/3 wins, -$526 total loss",
        "blacklist_review": "2026-06-01",   # Review date — may unblock after Layer 6 live
        "priority":       0,
        "min_score":      5,          # Effectively impossible
        "notes":          "Consistent loser Sprint 0. Re-evaluate after Structure Engine live."
    },
    "USDCAD": {
        "blacklist":      False,
        "priority":       1,
        "min_score":      3,
        "sessions":       ["New York"],
        "max_daily":      2,
        "trail_pct":      20,
        "location_zone":  0.33,
        "notes":          "Oil-correlated. NY session only."
    },

    # ── SA & EMERGING ──────────────────────────────────────────────────────────
    "USDZAR": {
        "blacklist":      False,
        "priority":       3,          # ← TOP PRIORITY: Sprint 0: 3/3 wins, +$339
        "min_score":      3,
        "sessions":       ["London","New York","Asian"],  # All sessions
        "max_daily":      4,
        "sl_points":      63,
        "tp_ratio":       1.5,
        "trail_pct":      15,
        "location_zone":  0.40,       # Slightly more tolerant — trends well
        "notes":          "Sprint 0 STAR: 100% win rate, +$339. Prioritise always."
    },

    # ── US INDICES ─────────────────────────────────────────────────────────────
    "US_TECH100": {
        "blacklist":      False,
        "priority":       2,
        "min_score":      3,
        "sessions":       ["New York"],
        "max_daily":      2,
        "sl_points":      50,
        "tp_ratio":       1.5,
        "trail_pct":      15,
        "location_zone":  0.33,
        "notes":          "NAS100 — tech heavy. US session 14:30-21:00 UTC only."
    },
    "US_500": {
        "blacklist":      False,
        "priority":       2,          # Equal to NAS100 — broader market
        "min_score":      3,
        "sessions":       ["New York"],
        "max_daily":      2,
        "sl_points":      15,
        "tp_ratio":       1.5,
        "trail_pct":      15,
        "location_zone":  0.33,
        "notes":          "S&P500 — 500 stocks, less tech-heavy than NAS100. US session only. Ava symbol: US_500"
    },

    "GBPJPY": {
        "blacklist":      False,
        "priority":       1,
        "min_score":      3,
        "sessions":       ["London","Asian"],
        "max_daily":      2,
        "trail_pct":      15,
        "location_zone":  0.30,
        "notes":          "High volatility. Tight location required."
    },
    "AUDJPY": {
        "blacklist":      False,
        "priority":       1,
        "min_score":      3,
        "sessions":       ["Asian","London"],
        "max_daily":      2,
        "trail_pct":      20,
        "location_zone":  0.33,
        "notes":          "Asian session. Commodity currency."
    },
}

# Daily trade counter — resets at midnight UTC
_daily_trades = {}   # {symbol: count}
_daily_date   = None # Date string for reset check

def _get_personality(symbol):
    """Get personality config for a symbol with safe defaults"""
    return PERSONALITY.get(symbol, {
        "blacklist":     False,
        "priority":      1,
        "min_score":     3,
        "sessions":      ["London","New York","Asian"],
        "max_daily":     3,
        "trail_pct":     20,
        "location_zone": 0.33,
        "notes":         "Default personality — no custom config"
    })

def _personality_check(symbol, action, session):
    """
    ENGINE 4: Personality gate — returns (allowed, reason)
    Checks: blacklist / session / daily limit / min_score threshold
    """
    global _daily_trades, _daily_date

    p = _get_personality(symbol)

    # Reset daily counter at midnight UTC
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _daily_date != today:
        _daily_trades = {}
        _daily_date   = today

    # 1. Blacklist check
    if p.get("blacklist", False):
        reason = p.get("blacklist_reason", "Blacklisted")
        return False, f"BLACKLISTED: {reason}"

    # 2. Session check
    allowed_sessions = p.get("sessions", ["London","New York","Asian"])
    if session not in allowed_sessions:
        return False, f"WRONG SESSION: {symbol} runs in {allowed_sessions}, not {session}"

    # 3. Daily trade limit
    daily_count = _daily_trades.get(symbol, 0)
    max_daily   = p.get("max_daily", 3)
    if daily_count >= max_daily:
        return False, f"DAILY LIMIT: {symbol} already traded {daily_count}/{max_daily} times today"

    return True, f"PERSONALITY OK: priority={p.get('priority',1)}, session={session}"

def _personality_min_score(symbol):
    """Get per-symbol minimum score threshold"""
    return _get_personality(symbol).get("min_score", 3)

def _personality_priority(symbol):
    """Get scanner priority (higher = picked first)"""
    return _get_personality(symbol).get("priority", 1)

def _personality_record_trade(symbol):
    """Record that a trade was fired for this symbol today"""
    global _daily_trades
    _daily_trades[symbol] = _daily_trades.get(symbol, 0) + 1
    print(f"[ENGINE4] {symbol} daily count: {_daily_trades[symbol]}")

# ─── Health Check ─────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "Claude-Market Webhook Server",
        "version": "8.3",
        "developer": "Lukas Ferreira - Pretoria ZA",
        "trading_enabled": trading_enabled,
        "pending_signal": pending_signal is not None,
        "last_scan": scan_results["last_run"],
        "next_scan": scan_results["next_run"],
        "assets_monitored": sum(len(v) for v in ASSET_GROUPS.values()),
        "groups": list(ASSET_GROUPS.keys()),
        "mt5_connected": bool(mt5_status.get("timestamp")),
        "timestamp": datetime.utcnow().isoformat()
    })

# ─── API Key Test ─────────────────────────────────────────────────────────────
@app.route("/test/apikey", methods=["GET"])
def test_apikey():
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=10,
            messages=[{"role":"user","content":"Reply with OK"}]
        )
        return jsonify({"status":"success","response":resp.content[0].text,"key_prefix":os.environ.get("ANTHROPIC_API_KEY","")[:12]+"..."})
    except Exception as e:
        return jsonify({"status":"error","error":str(e),"key_prefix":os.environ.get("ANTHROPIC_API_KEY","")[:12]+"..."})

# ─── TradingView Webhook ───────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    global pending_signal
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "No JSON payload"}), 400

        secret = data.get("secret", "")
        if secret != "claude-market-2026":
            return jsonify({"error": "Invalid secret"}), 401

        symbol = str(data.get("ticker", data.get("symbol", ""))).upper().strip()
        action = str(data.get("action", "BUY")).upper().strip()
        price  = data.get("price", "0")

        if not symbol:
            return jsonify({"error": "Missing symbol"}), 400
        if action not in ["BUY", "SELL"]:
            return jsonify({"error": f"Invalid action: {action}"}), 400

        if not trading_enabled:
            return jsonify({"status": "blocked", "reason": "Kill switch active"}), 200

        score = _claude_validate(symbol, action, price, "BB")
        if score < 3:
            return jsonify({"status": "rejected", "reason": "Score too low", "score": score}), 200

        pending_signal = {
            "symbol": symbol,
            "action": action,
            "price": str(price),
            "score": str(score),
            "signal_type": "BB_BREAKOUT",
            "source": "tradingview",
            "timestamp": datetime.utcnow().isoformat()
        }
        _add_to_history(symbol, action, score, "BB")
        print(f"[SIGNAL] TradingView → {symbol} {action} Score:{score}/5")

        return jsonify({
            "status": "signal_stored",
            "symbol": symbol,
            "action": action,
            "score": f"{score}/5",
            "message": "EA will poll and trade within 10 seconds"
        })
    except Exception as e:
        print(f"[ERROR] /webhook: {e}")
        return jsonify({"error": str(e)}), 500

# ─── EA Signal Poll ────────────────────────────────────────────────────────────
@app.route("/signal", methods=["GET"])
def get_signal():
    global pending_signal
    if not pending_signal:
        return jsonify({"signal": False})
    sig = pending_signal
    pending_signal = None        # Clear once delivered
    print(f"[SIGNAL] Delivered to EA: {sig['symbol']} {sig['action']}")
    return jsonify({"signal": True, **sig})


@app.route("/manual-signal", methods=["POST"])
def post_manual_signal():
    """
    Command Centre fires a manual trade directly — bypasses all engine gates.
    Signal queued into pending_signal → EA picks up within 10 seconds.
    Trade tagged as source=MANUAL in comment so history can filter it.
    Score=5 ensures EA Gate 1 passes. EA Cooldown (Gate 2) still applies —
    if symbol was traded in last 90 min, EA will reject and log why.
    """
    global pending_signal
    try:
        data   = request.get_json(force=True)
        symbol = str(data.get("symbol", "")).upper().strip()
        action = str(data.get("action", "BUY")).upper().strip()
        note   = str(data.get("note", "Manual trade — CC"))

        valid_syms = ["GOLD","SILVER","USDZAR","EURUSD","BTCUSD",
                      "ETHUSD","US_TECH100","US_500"]
        if symbol not in valid_syms:
            return jsonify({"ok": False, "error": f"{symbol} not in active symbol list"}), 400
        if action not in ["BUY","SELL"]:
            return jsonify({"ok": False, "error": "action must be BUY or SELL"}), 400

        if pending_signal:
            return jsonify({"ok": False,
                            "error": "Auto signal already pending — wait for EA to collect it"}), 409

        pending_signal = {
            "symbol":    symbol,
            "action":    action,
            "score":     5,          # Max score — bypasses EA Gate 1
            "type":      "MANUAL",
            "source":    "MANUAL",
            "note":      note,
            "timestamp": datetime.utcnow().isoformat()
        }
        print(f"[MANUAL] Trade queued: {symbol} {action} — EA will pick up within 10s")
        return jsonify({"ok": True, "queued": pending_signal,
                        "note": "EA collects within 10 seconds via /signal poll"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/manual-signal", methods=["GET"])
def get_manual_signal_status():
    """CC checks if manual signal is still pending or was collected by EA"""
    return jsonify({
        "pending":   pending_signal is not None,
        "signal":    pending_signal,
        "timestamp": datetime.utcnow().isoformat()
    })


@app.route("/close-request", methods=["POST"])
def post_close_request():
    """Forensics page sends a close request for a specific ticket."""
    global close_requests
    try:
        data   = request.get_json(force=True)
        ticket = int(data.get("ticket", 0))
        symbol = str(data.get("symbol", "")).upper().strip()
        if not ticket:
            return jsonify({"ok": False, "error": "ticket required"}), 400
        if any(r["ticket"] == ticket for r in close_requests):
            return jsonify({"ok": True, "ticket": ticket, "note": "Already queued"})
        close_requests.append({"ticket": ticket, "symbol": symbol,
                                "timestamp": datetime.utcnow().isoformat()})
        print(f"[CLOSE] Close request queued: ticket={ticket} symbol={symbol}")
        return jsonify({"ok": True, "ticket": ticket, "queued": True,
                        "note": "EA will close within 10 seconds"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/close-request", methods=["GET"])
def get_close_request():
    """EA polls this every 10 seconds. Returns first pending close request."""
    global close_requests
    if close_requests:
        req = close_requests.pop(0)
        print(f"[CLOSE] Delivering to EA: ticket={req['ticket']}")
        return jsonify({"pending": True, **req})
    return jsonify({"pending": False, "timestamp": datetime.utcnow().isoformat()})


# ─── Brain Config — full system and per-symbol settings ───────────────────────

DEFAULT_CONFIG = {
    "system": {
        "scan_asian_min":        45,
        "scan_london_min":       20,
        "scan_overlap_min":      10,
        "scan_newyork_min":      15,
        "heat_limit_pct":         8.0,
        "drawdown_limit_pct":     5.0,
        "max_session_losses":     2,
        "max_consecutive_loss":   3,
        "session_reset_hour":     4,
        "global_cooldown_min":   90,
        "risk_pct":               2.0,
    },
    "symbols": {
        "GOLD": {
            "active": True,
            "confidence_threshold_pct": 65,
            "bb_zone_pct":              30,
            "min_atr_pct":            0.10,
            "max_atr_pct":            2.00,
            "session_asian":          False,
            "session_london":          True,
            "session_newyork":         True,
            "max_daily_trades":           2,
            "sl_mode":              "fixed",
            "sl_points":                300,
            "sl_atr_multiplier":        1.5,
            "sl_min_points":             50,
            "sl_max_points":           1000,
            "tp_ratio":                 1.8,
            "tp_range_ratio":           0.8,
            "e8_enabled":              True,
            "e8_tp1_ratio_pct":         100,
            "e8_close_pct":              50,
            "e8_move_sl_to_be":        True,
            "trail_enabled":           True,
            "trail_activate_pct":        40,
            "trail_lock_pct":            50,
            "trail_step_points":         10,
            "risk_pct":                 2.0,
            "max_lots":                1.00,
            "seasonal_bias":       "neutral",
            "weight_pct":               100,
            "notes": "Trending symbol — BB SELL unreliable in uptrend"
        },
        "SILVER": {
            "active": True,
            "confidence_threshold_pct": 60,
            "bb_zone_pct":              30,
            "min_atr_pct":            0.10,
            "max_atr_pct":            3.00,
            "session_asian":          False,
            "session_london":          True,
            "session_newyork":         True,
            "max_daily_trades":           2,
            "sl_mode":              "fixed",
            "sl_points":                200,
            "sl_atr_multiplier":        1.5,
            "sl_min_points":             30,
            "sl_max_points":            500,
            "tp_ratio":                 1.5,
            "tp_range_ratio":           0.8,
            "e8_enabled":              True,
            "e8_tp1_ratio_pct":         100,
            "e8_close_pct":              50,
            "e8_move_sl_to_be":        True,
            "trail_enabled":           True,
            "trail_activate_pct":        40,
            "trail_lock_pct":            50,
            "trail_step_points":          5,
            "risk_pct":                 2.0,
            "max_lots":                1.00,
            "seasonal_bias":       "neutral",
            "weight_pct":               100,
            "notes": "Follows GOLD but amplified. Best performer May 2026."
        },
        "USDZAR": {
            "active": True,
            "confidence_threshold_pct": 70,
            "bb_zone_pct":              20,
            "min_atr_pct":            0.05,
            "max_atr_pct":            1.50,
            "session_asian":          False,
            "session_london":          True,
            "session_newyork":         True,
            "max_daily_trades":           1,
            "sl_mode":              "fixed",
            "sl_points":                300,
            "sl_atr_multiplier":        1.5,
            "sl_min_points":             50,
            "sl_max_points":            800,
            "tp_ratio":                 2.0,
            "tp_range_ratio":           0.8,
            "e8_enabled":              True,
            "e8_tp1_ratio_pct":         100,
            "e8_close_pct":              50,
            "e8_move_sl_to_be":        True,
            "trail_enabled":           True,
            "trail_activate_pct":        40,
            "trail_lock_pct":            50,
            "trail_step_points":         20,
            "risk_pct":                 2.0,
            "max_lots":                1.00,
            "seasonal_bias":       "neutral",
            "weight_pct":               100,
            "notes": "0% win rate May 2026. Higher bar. SA political risk."
        },
        "EURUSD": {
            "active": True,
            "confidence_threshold_pct": 60,
            "bb_zone_pct":              30,
            "min_atr_pct":            0.05,
            "max_atr_pct":            1.00,
            "session_asian":          False,
            "session_london":          True,
            "session_newyork":         True,
            "max_daily_trades":           2,
            "sl_mode":              "fixed",
            "sl_points":                150,
            "sl_atr_multiplier":        1.5,
            "sl_min_points":             20,
            "sl_max_points":            400,
            "tp_ratio":                 1.5,
            "tp_range_ratio":           0.8,
            "e8_enabled":              True,
            "e8_tp1_ratio_pct":         100,
            "e8_close_pct":              50,
            "e8_move_sl_to_be":        True,
            "trail_enabled":           True,
            "trail_activate_pct":        40,
            "trail_lock_pct":            50,
            "trail_step_points":          5,
            "risk_pct":                 2.0,
            "max_lots":                1.00,
            "seasonal_bias":       "neutral",
            "weight_pct":               100,
            "notes": "Tightest spread. ECB/Fed driven. Reliable signals."
        },
        "BTCUSD": {
            "active": True,
            "confidence_threshold_pct": 75,
            "bb_zone_pct":              20,
            "min_atr_pct":            0.20,
            "max_atr_pct":            4.00,
            "session_asian":          False,
            "session_london":         False,
            "session_newyork":         True,
            "max_daily_trades":           1,
            "sl_mode":              "fixed",
            "sl_points":                500,
            "sl_atr_multiplier":        1.5,
            "sl_min_points":            200,
            "sl_max_points":           2000,
            "tp_ratio":                 2.0,
            "tp_range_ratio":           1.0,
            "e8_enabled":              True,
            "e8_tp1_ratio_pct":         100,
            "e8_close_pct":              50,
            "e8_move_sl_to_be":        True,
            "trail_enabled":           True,
            "trail_activate_pct":        30,
            "trail_lock_pct":            50,
            "trail_step_points":         50,
            "risk_pct":                 1.0,
            "max_lots":                0.10,
            "seasonal_bias":       "neutral",
            "weight_pct":               100,
            "notes": "Halving cycle awareness needed. NY session only. High bar."
        },
        "ETHUSD": {
            "active": True,
            "confidence_threshold_pct": 65,
            "bb_zone_pct":              25,
            "min_atr_pct":            0.15,
            "max_atr_pct":            3.50,
            "session_asian":          False,
            "session_london":         False,
            "session_newyork":         True,
            "max_daily_trades":           2,
            "sl_mode":              "fixed",
            "sl_points":                400,
            "sl_atr_multiplier":        1.5,
            "sl_min_points":            100,
            "sl_max_points":           1500,
            "tp_ratio":                 1.8,
            "tp_range_ratio":           1.0,
            "e8_enabled":              True,
            "e8_tp1_ratio_pct":         100,
            "e8_close_pct":              50,
            "e8_move_sl_to_be":        True,
            "trail_enabled":           True,
            "trail_activate_pct":        30,
            "trail_lock_pct":            50,
            "trail_step_points":         30,
            "risk_pct":                 1.0,
            "max_lots":                0.20,
            "seasonal_bias":       "neutral",
            "weight_pct":               100,
            "notes": "Follows BTC direction. Faster moves. Lower risk than BTC."
        },
        "US_TECH100": {
            "active": True,
            "confidence_threshold_pct": 55,
            "bb_zone_pct":              30,
            "min_atr_pct":            0.10,
            "max_atr_pct":            3.00,
            "session_asian":          False,
            "session_london":         False,
            "session_newyork":         True,
            "max_daily_trades":           2,
            "sl_mode":              "fixed",
            "sl_points":                300,
            "sl_atr_multiplier":        1.5,
            "sl_min_points":            100,
            "sl_max_points":           1000,
            "tp_ratio":                 1.5,
            "tp_range_ratio":           0.8,
            "e8_enabled":              True,
            "e8_tp1_ratio_pct":         100,
            "e8_close_pct":              50,
            "e8_move_sl_to_be":        True,
            "trail_enabled":           True,
            "trail_activate_pct":        40,
            "trail_lock_pct":            50,
            "trail_step_points":         20,
            "risk_pct":                 2.0,
            "max_lots":                0.50,
            "seasonal_bias":       "neutral",
            "weight_pct":               100,
            "notes": "100% win rate May 2026. Lower threshold — keep trading."
        },
        "US_500": {
            "active": True,
            "confidence_threshold_pct": 55,
            "bb_zone_pct":              30,
            "min_atr_pct":            0.05,
            "max_atr_pct":            2.00,
            "session_asian":          False,
            "session_london":         False,
            "session_newyork":         True,
            "max_daily_trades":           2,
            "sl_mode":              "fixed",
            "sl_points":                150,
            "sl_atr_multiplier":        1.5,
            "sl_min_points":             50,
            "sl_max_points":            500,
            "tp_ratio":                 1.5,
            "tp_range_ratio":           0.8,
            "e8_enabled":              True,
            "e8_tp1_ratio_pct":         100,
            "e8_close_pct":              50,
            "e8_move_sl_to_be":        True,
            "trail_enabled":           True,
            "trail_activate_pct":        40,
            "trail_lock_pct":            50,
            "trail_step_points":         10,
            "risk_pct":                 2.0,
            "max_lots":                0.50,
            "seasonal_bias":       "neutral",
            "weight_pct":               100,
            "notes": "Correlated with TECH100. Both NY session only."
        }
    }
}

# In-memory config (loaded from DB on start, saved back on change)
_brain_config = dict(DEFAULT_CONFIG)

def _sync_brain_to_personality():
    """
    Bridge: Push brain config values into the PERSONALITY dict.
    This means all existing scanner code that reads PERSONALITY
    automatically uses the Brain settings without any other changes.
    """
    sym_cfg = _brain_config.get("symbols", {})
    for sym, brain in sym_cfg.items():
        if sym not in PERSONALITY:
            continue
        p = PERSONALITY[sym]

        # Active / blacklist
        p["blacklist"] = not brain.get("active", True)

        # Confidence threshold % → min_score (1-5)
        conf = brain.get("confidence_threshold_pct", 60)
        p["min_score"] = 5 if conf >= 90 else 4 if conf >= 75 else 3 if conf >= 55 else 2

        # BB zone % → location_zone (0.0-1.0)
        p["location_zone"] = brain.get("bb_zone_pct", 30) / 100.0

        # Sessions list from booleans
        sessions = []
        if brain.get("session_asian"):   sessions.append("Asian")
        if brain.get("session_london"):  sessions.append("London")
        if brain.get("session_newyork"): sessions.append("New York")
        if sessions: p["sessions"] = sessions

        # Max daily trades
        p["max_daily"] = brain.get("max_daily_trades", p.get("max_daily", 2))

        # SL and TP
        p["sl_points"] = brain.get("sl_points", p.get("sl_points", 200))
        p["tp_ratio"]  = brain.get("tp_ratio",   p.get("tp_ratio",  1.5))

        # Weight → priority (100%=3, 67%=2, 33%=1)
        w = brain.get("weight_pct", 100)
        p["priority"] = 3 if w >= 80 else 2 if w >= 50 else 1

    print("[BRAIN] ✅ Config synced to personality engine")


def _load_brain_config():
    """Load config from DB on startup."""
    global _brain_config
    try:
        conn = _db_connect()
        if not conn: return
        cur = conn.cursor()
        cur.execute("SELECT value FROM brain_config WHERE key='main' LIMIT 1")
        row = cur.fetchone()
        if row:
            _brain_config = json.loads(row[0])
            print("[BRAIN] ✅ Config loaded from database")
        else:
            print("[BRAIN] No saved config — using defaults")
        _sync_brain_to_personality()
        cur.close(); conn.close()
    except Exception as e:
        print(f"[BRAIN] Config load error: {e}")

def _save_brain_config():
    """Save current config to DB."""
    try:
        conn = _db_connect()
        if not conn: return False
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brain_config (
                key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT NOW()
            )""")
        cur.execute("""
            INSERT INTO brain_config (key, value, updated_at)
            VALUES ('main', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        """, (json.dumps(_brain_config),))
        conn.commit(); cur.close(); conn.close()
        _sync_brain_to_personality()
        return True
    except Exception as e:
        print(f"[BRAIN] Config save error: {e}")
        return False

@app.route("/config", methods=["GET"])
def get_config():
    """Brain Settings tool reads full config."""
    return jsonify({"ok": True, "config": _brain_config,
                    "defaults": DEFAULT_CONFIG})

@app.route("/config", methods=["POST"])
def post_config():
    """Brain Settings tool writes full or partial config."""
    global _brain_config
    try:
        data = request.get_json(force=True)
        # Merge changes — supports full replace or partial update
        if "system" in data:
            _brain_config["system"].update(data["system"])
        if "symbols" in data:
            for sym, settings in data["symbols"].items():
                if sym in _brain_config["symbols"]:
                    _brain_config["symbols"][sym].update(settings)
        saved = _save_brain_config()
        print(f"[BRAIN] Config updated and {'saved' if saved else 'save failed'}")
        return jsonify({"ok": True, "saved": saved, "config": _brain_config})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/config/reset", methods=["POST"])
def reset_config():
    """Reset to defaults."""
    global _brain_config
    _brain_config = dict(DEFAULT_CONFIG)
    _save_brain_config()
    return jsonify({"ok": True, "config": _brain_config})
@app.route("/status", methods=["POST"])
def receive_status():
    global mt5_status, trade_history, symbol_regimes, bb_data
    try:
        data = request.get_json(force=True)
        mt5_status = data
        mt5_status["received_at"] = datetime.utcnow().isoformat()

        # Engine 1: Read regime from EA
        ea_sym    = str(data.get("symbol","")).strip().upper()
        ea_regime = str(data.get("regime","NEUTRAL")).upper().strip()
        if ea_sym and ea_regime in ["BULLISH","BEARISH","NEUTRAL"]:
            if symbol_regimes.get(ea_sym) != ea_regime:
                print(f"[REGIME] {ea_sym} = {ea_regime} (EA: EMA50/200+ADX14 H1)")
            symbol_regimes[ea_sym] = ea_regime
            scan_results["regimes"] = dict(symbol_regimes)

        # Engine 6: Store BB data from EA v8.9
        ea_bb = data.get("bb_data", {})
        for sym, bbd in ea_bb.items():
            if isinstance(bbd, dict) and bbd.get("range", 0) > 0:
                bb_data[sym.upper()] = bbd
                loc = bbd.get("location", 0.5)
                print(f"[ENGINE6] BB data: {sym} location={loc:.1%} "
                      f"upper={bbd.get('upper',0):.2f} "
                      f"lower={bbd.get('lower',0):.2f} "
                      f"price={bbd.get('price',0):.2f}")

        # Engine 5: Store ATR data (volatility) from EA v9.1
        ea_atr = data.get("atr_data", {})
        for sym, atr_val in ea_atr.items():
            if isinstance(atr_val, (int, float)) and atr_val > 0:
                price = bb_data.get(sym.upper(), {}).get("price", 0)
                atr_pct = (atr_val / price * 100) if price > 0 else 0
                volatility_data[sym.upper()] = {
                    "atr":       round(atr_val, 5),
                    "atr_pct":   round(atr_pct, 4),
                    "timestamp": datetime.utcnow().isoformat()
                }
                print(f"[ENGINE5] ATR data: {sym} ATR={atr_val:.5f} ({atr_pct:.3f}% of price)")

        # Engine 7: Track portfolio equity for drawdown calculation
        equity = data.get("equity")
        if equity:
            _e7_update_equity(float(equity))

        # Extract closed trades if EA sends them
        for t in data.get("closed_trades", []):
            tickets = [x["ticket"] for x in trade_history]
            if t.get("ticket") not in tickets:
                trade_history.insert(0, t)
        trade_history = trade_history[:200]

        return jsonify({"ok": True, "trading_enabled": trading_enabled})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/status", methods=["GET"])
def send_status():
    return jsonify(mt5_status if mt5_status else {"connected": False})

# ─── Trade History ─────────────────────────────────────────────────────────────
@app.route("/history", methods=["GET"])
def get_history():
    return jsonify({"trades": trade_history, "count": len(trade_history)})

@app.route("/history", methods=["POST"])
def post_history():
    """EA can POST a closed trade directly (v8.3: includes source field MANUAL/EA)"""
    global trade_history
    try:
        data = request.get_json(force=True)
        ticket = str(data.get("ticket", ""))
        if ticket in deleted_tickets:
            print(f"[HISTORY] Ticket {ticket} was deleted by user — rejecting re-post")
            return jsonify({"ok": True, "skipped": "deleted by user"})
        if ticket and ticket not in [str(x.get("ticket", "")) for x in trade_history]:
            data["received_at"] = datetime.utcnow().isoformat()
            trade_history.insert(0, data)
            trade_history = trade_history[:200]
            source = data.get("source", "EA")
            symbol = data.get("symbol", "?")
            profit = data.get("profit", 0)
            db_save_trade(data)
            _e7_record_close(symbol, float(profit))   # Engine 7 session tracking

            # Engine 9: Update memory with direction tracking
            trade_type = str(data.get("type", "BUY")).upper()
            stored_regime = str(data.get("regime", "")).upper()
            regime  = stored_regime if stored_regime in ["BULLISH","BEARISH","NEUTRAL"] else (
                symbol_regimes.get(symbol.upper(), "BULLISH" if trade_type == "BUY" else "BEARISH")
            )
            session = _infer_session(data.get("close_time") or data.get("received_at", ""))
            _memory_update(symbol, regime, session, float(profit) > 0, direction=trade_type)
            mem_adj = _get_memory_confidence(symbol, trade_type, regime, session)
            print(f"[ENGINE9] Memory updated: {symbol} {trade_type} {regime}/{session} "
                  f"{'WIN' if float(profit)>0 else 'LOSS'} → adj={mem_adj:+.0f}%")
            print(f"[HISTORY] Received closed trade: {symbol} ${profit:.2f} [{source}]")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Kill Switch ───────────────────────────────────────────────────────────────
@app.route("/history/delete", methods=["POST"])
def delete_history():
    """Command Centre calls this when user deletes a closed trade — removed permanently"""
    global trade_history, deleted_tickets
    try:
        data = request.get_json(force=True)
        ticket = str(data.get("ticket", ""))
        removed = 0
        if ticket:
            deleted_tickets.add(ticket)
            before = len(trade_history)
            trade_history = [t for t in trade_history if str(t.get("ticket", "")) != ticket]
            removed = before - len(trade_history)
            db_delete_trade(ticket)  # ← remove from database permanently
            print(f"[HISTORY] Deleted ticket {ticket} ({removed} record(s) removed, {len(deleted_tickets)} total blocked)")
        return jsonify({"ok": True, "removed": removed})
    except Exception as e:
        print(f"[ERROR] /history/delete: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/trading/stop", methods=["GET","POST"])
@app.route("/trading/pause", methods=["GET","POST"])
def stop_trading():
    global trading_enabled, pending_signal
    trading_enabled = False
    pending_signal  = None
    print("[KILL SWITCH] Trading STOPPED")
    return jsonify({"trading_enabled": False, "message": "All trading halted"})

@app.route("/trading/resume", methods=["GET","POST"])
def resume_trading():
    global trading_enabled
    trading_enabled = True
    print("[KILL SWITCH] Trading RESUMED")
    return jsonify({"trading_enabled": True, "message": "Trading resumed"})

@app.route("/trading/status", methods=["GET"])
def trading_status():
    # v6.4: recently_traded is a dict internally — return keys as list for Command Centre
    rt_list = list(recently_traded.keys())
    return jsonify({
        "trading_enabled": trading_enabled,
        "pending_signal": pending_signal is not None,
        "recently_traded": rt_list
    })

# ─── Scanner Intelligence Endpoints ───────────────────────────────────────────
@app.route("/scanner/results", methods=["GET"])
def scanner_results():
    result = dict(scan_results)
    result["recently_traded"] = list(recently_traded.keys())  # Always return as list
    return jsonify(result)

@app.route("/scanner/run", methods=["GET","POST"])
def manual_scan():
    threading.Thread(target=run_scanner, daemon=True).start()
    return jsonify({"status": "Scanner triggered manually"})

# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# ENGINE 9 HELPERS — Memory / Win Rate Bias
# ══════════════════════════════════════════════════════════════════════════════

def _infer_session(timestamp_str: str) -> str:
    """Determine trading session from a UTC timestamp string."""
    try:
        if "T" in timestamp_str:
            h = int(timestamp_str.split("T")[1][:2])
        elif " " in timestamp_str:
            h = int(timestamp_str.split(" ")[1][:2])
        else:
            return "London"
        if 8 <= h < 13:  return "London"
        if 13 <= h < 21: return "New York"
        return "Asian"
    except:
        return "London"


def _memory_update(symbol: str, regime: str, session: str, won: bool, direction: str = ""):
    """
    ENGINE 9: Add one trade result to memory_data.
    Now tracks DIRECTION (BUY/SELL) separately — the critical upgrade.
    GOLD SELL losing streak no longer hides behind GOLD BUY wins.
    """
    sym = symbol.upper()
    dr  = direction.upper() if direction else ""

    # Initialise symbol
    if sym not in memory_data:
        memory_data[sym] = {"overall": {"wins":0,"losses":0,"total":0}}

    # Direction bucket (BUY or SELL)
    if dr in ("BUY","SELL"):
        if dr not in memory_data[sym]:
            memory_data[sym][dr] = {"overall": {"wins":0,"losses":0,"total":0}}
        if regime not in memory_data[sym][dr]:
            memory_data[sym][dr][regime] = {}
        if session not in memory_data[sym][dr][regime]:
            memory_data[sym][dr][regime][session] = {"wins":0,"losses":0,"total":0}

        cond = memory_data[sym][dr][regime][session]
        dr_ov = memory_data[sym][dr]["overall"]
        if won:
            cond["wins"]  += 1; dr_ov["wins"]  += 1
        else:
            cond["losses"] += 1; dr_ov["losses"] += 1
        cond["total"]  += 1; dr_ov["total"] += 1

    # Regime+session bucket (direction-agnostic fallback)
    if regime not in memory_data[sym]:
        memory_data[sym][regime] = {}
    if session not in memory_data[sym][regime]:
        memory_data[sym][regime][session] = {"wins":0,"losses":0,"total":0}
    bucket  = memory_data[sym][regime][session]
    overall = memory_data[sym]["overall"]
    if won:
        bucket["wins"]  += 1; overall["wins"]  += 1
    else:
        bucket["losses"] += 1; overall["losses"] += 1
    bucket["total"] += 1; overall["total"] += 1


def _get_memory_confidence(symbol: str, direction: str, regime: str, session: str) -> float:
    """
    ENGINE 9: Returns a confidence ADJUSTMENT (-25 to +10) based on win rate memory.
    Replaces the old +1/0/-1 system with a continuous percentage scale.

    Adjustment is applied on top of Claude's signal confidence:
      win rate  0% → -25%  (this setup keeps losing — penalise hard)
      win rate 25% → -15%
      win rate 40% → -5%
      win rate 50% →  0%   (neutral)
      win rate 65% → +5%
      win rate 80% → +10%  (this setup keeps winning — reward)

    Priority: direction+regime+session → direction+overall → regime+session → overall
    Minimum sample size: 3 trades for direction, 5 for overall.
    """
    sym = symbol.upper()
    dr  = direction.upper()
    data = memory_data.get(sym, {})

    def wr_to_adj(wr: float) -> float:
        """Map win rate to confidence adjustment."""
        if wr <= 0.00: return -25.0
        if wr <= 0.25: return -15.0
        if wr <= 0.40: return  -5.0
        if wr <= 0.55: return   0.0
        if wr <= 0.65: return  +5.0
        return +10.0

    # Level 1: direction + regime + session (most specific)
    dr_data = data.get(dr, {})
    cond = dr_data.get(regime, {}).get(session, {})
    if cond.get("total", 0) >= 3:
        wr = cond["wins"] / cond["total"]
        adj = wr_to_adj(wr)
        print(f"[ENGINE9] {sym} {dr} {regime}/{session}: {cond['wins']}/{cond['total']} "
              f"= {wr*100:.0f}% WR → confidence adj {adj:+.0f}%")
        return adj

    # Level 2: direction overall
    dr_ov = dr_data.get("overall", {})
    if dr_ov.get("total", 0) >= 3:
        wr = dr_ov["wins"] / dr_ov["total"]
        adj = wr_to_adj(wr)
        print(f"[ENGINE9] {sym} {dr} overall: {dr_ov['wins']}/{dr_ov['total']} "
              f"= {wr*100:.0f}% WR → confidence adj {adj:+.0f}%")
        return adj

    # Level 3: regime + session (direction-agnostic)
    cond2 = data.get(regime, {}).get(session, {})
    if cond2.get("total", 0) >= 3:
        wr = cond2["wins"] / cond2["total"]
        return wr_to_adj(wr)

    # Level 4: symbol overall (5 trade minimum)
    ov = data.get("overall", {})
    if ov.get("total", 0) >= 5:
        wr = ov["wins"] / ov["total"]
        return wr_to_adj(wr)

    print(f"[ENGINE9] {sym} {dr}: insufficient data — no adjustment")
    return 0.0  # Not enough data — neutral


def _get_memory_score(symbol: str, regime: str, session: str) -> int:
    """Legacy +1/0/-1 score — kept for Claude hint string."""
    sym  = symbol.upper()
    data = memory_data.get(sym, {})
    cond = data.get(regime, {}).get(session, {})
    if cond.get("total", 0) >= 3:
        wr = cond["wins"] / cond["total"]
        if wr >= 0.75: return +1
        if wr <= 0.40: return -1
        return 0
    overall = data.get("overall", {})
    if overall.get("total", 0) >= 5:
        wr = overall["wins"] / overall["total"]
        if wr >= 0.75: return +1
        if wr <= 0.40: return -1
    return 0


def _rebuild_memory():
    """
    Rebuild memory_data from trade_history on startup.
    Infers regime from trade direction (BUY → BULLISH, SELL → BEARISH).
    Session inferred from open_time timestamp.
    """
    global memory_data
    memory_data = {}
    count = 0
    for trade in trade_history:
        sym    = str(trade.get("symbol", "")).upper()
        profit = float(trade.get("profit", 0))
        ttype  = str(trade.get("type", "BUY")).upper()
        ot     = str(trade.get("open_time") or trade.get("timestamp") or "")
        if not sym:
            continue
        stored_regime = str(trade.get("regime", "")).upper()
        regime = stored_regime if stored_regime in ["BULLISH","BEARISH","NEUTRAL"] else (
            "BULLISH" if ttype == "BUY" else "BEARISH"
        )
        session = _infer_session(ot)
        _memory_update(sym, regime, session, profit > 0, direction=ttype)
        count += 1
    print(f"[ENGINE9] Memory rebuilt from {count} trades — "
          f"{len(memory_data)} symbols tracked")
    for sym, data in memory_data.items():
        ov = data.get("overall", {})
        if ov.get("total", 0) > 0:
            wr = ov["wins"] / ov["total"] * 100
            print(f"[ENGINE9]   {sym}: {ov['wins']}/{ov['total']} = {wr:.0f}% win rate")


@app.route("/memory", methods=["GET"])
def get_memory():
    """Engine 9: Win rate memory per symbol — Config Tab reads this"""
    result = {}
    for sym, data in memory_data.items():
        ov = data.get("overall", {})
        total = ov.get("total", 0)
        win_rate = round(ov["wins"] / total * 100, 1) if total > 0 else 0
        conditions = {}
        for regime, sessions in data.items():
            if regime == "overall":
                continue
            conditions[regime] = {}
            for session, stats in sessions.items():
                t = stats.get("total", 0)
                wr = round(stats["wins"] / t * 100, 1) if t > 0 else 0
                score = _get_memory_score(sym, regime, session)
                conditions[regime][session] = {**stats, "win_rate_pct": wr, "memory_score": score}
        result[sym] = {
            "overall": {**ov, "win_rate_pct": win_rate},
            "conditions": conditions,
            "memory_score": _get_memory_score(sym,
                symbol_regimes.get(sym, "NEUTRAL"),
                "London")  # sample score for current regime
        }
    return jsonify({
        "engine":    "9 — Memory",
        "symbols":   result,
        "total_trades_tracked": sum(d.get("overall",{}).get("total",0) for d in memory_data.values()),
        "timestamp": datetime.utcnow().isoformat()
    })


# ENGINE 5 HELPERS — Volatility Gate
# ══════════════════════════════════════════════════════════════════════════════

# Per-symbol volatility thresholds (ATR as % of price)
# Too volatile = spike, news event — spread costs too high
# Too quiet   = dead market, no momentum, trade stalls
VOLATILITY_LIMITS = {
    "USDZAR":    {"min_pct": 0.05, "max_pct": 1.50},
    "EURUSD":    {"min_pct": 0.02, "max_pct": 0.80},
    "GOLD":      {"min_pct": 0.05, "max_pct": 1.50},
    "SILVER":    {"min_pct": 0.08, "max_pct": 2.00},
    "BTCUSD":    {"min_pct": 0.20, "max_pct": 4.00},
    "ETHUSD":    {"min_pct": 0.15, "max_pct": 3.50},
    "US_TECH100":{"min_pct": 0.05, "max_pct": 2.00},
    "US_500":    {"min_pct": 0.04, "max_pct": 1.50},
}

def _check_volatility(symbol: str) -> tuple:
    """
    Engine 5: Is the current volatility acceptable for this symbol?
    Returns (allowed, reason).
    Passes if no ATR data — never blocks on missing data.
    """
    sym  = symbol.upper()
    data = volatility_data.get(sym)
    if not data:
        return True, f"ENGINE5 ✅ No ATR data for {sym} — passing"

    atr_pct = data["atr_pct"]
    limits  = VOLATILITY_LIMITS.get(sym, {"min_pct": 0.02, "max_pct": 3.0})
    min_pct = limits["min_pct"]
    max_pct = limits["max_pct"]

    if atr_pct > max_pct:
        return False, (f"ENGINE5 ❌ {sym} TOO VOLATILE: ATR={atr_pct:.3f}% "
                       f"(limit {max_pct}%) — spike or news event. Waiting for calm.")
    if atr_pct < min_pct:
        return False, (f"ENGINE5 ❌ {sym} TOO QUIET: ATR={atr_pct:.3f}% "
                       f"(min {min_pct}%) — dead market, no momentum. Spread too costly.")

    return True, (f"ENGINE5 ✅ {sym} volatility OK: ATR={atr_pct:.3f}% "
                  f"(range {min_pct}%–{max_pct}%)")


@app.route("/volatility", methods=["GET"])
def get_volatility():
    """Engine 5: Live ATR volatility status per symbol"""
    result = {}
    for sym, data in volatility_data.items():
        limits  = VOLATILITY_LIMITS.get(sym, {"min_pct": 0.02, "max_pct": 3.0})
        atr_pct = data["atr_pct"]
        status  = ("TOO_HIGH" if atr_pct > limits["max_pct"] else
                   "TOO_LOW"  if atr_pct < limits["min_pct"] else "OK")
        result[sym] = {**data, "status": status, "limits": limits}
    return jsonify({
        "engine":    "5 — Volatility",
        "symbols":   result,
        "timestamp": datetime.utcnow().isoformat()
    })


# ENGINE 3 HELPERS — Calendar / News Filter
# ══════════════════════════════════════════════════════════════════════════════

def _parse_ff_datetime(date_str: str, time_str: str):
    """
    Parse ForexFactory date/time to UTC datetime.
    date_str: "05-17-2026"   time_str: "2:00pm" / "All Day" / "Tentative"
    FF times are US Eastern (EDT = UTC-4 in summer, EST = UTC-5 in winter).
    We use UTC-4 (EDT) as the standard approximation — accurate within 1 hour
    which is more than enough for a 30-minute blocking window.
    """
    if not time_str or time_str.strip() in [
        "All Day", "Tentative", "Day 1", "Day 2", "Day 3", "", "N/A"
    ]:
        return None
    try:
        m, d, y = date_str.strip().split("-")
        ts = time_str.strip().lower().replace(" ", "")
        is_pm = "pm" in ts
        is_am = "am" in ts
        ts = ts.replace("pm", "").replace("am", "")
        if ":" in ts:
            h, mn = ts.split(":")
        else:
            h, mn = ts, "00"
        h, mn = int(h), int(mn)
        if is_pm and h != 12:
            h += 12
        elif is_am and h == 12:
            h = 0
        dt_eastern = datetime(int(y), int(m), int(d), h, mn)
        return dt_eastern + timedelta(hours=4)   # EDT → UTC
    except Exception as e:
        return None


def _fetch_news_calendar():
    """Download and cache the ForexFactory weekly calendar."""
    global _news_calendar, _news_last_fetch
    try:
        req = urllib.request.Request(
            _NEWS_FETCH_URL,
            headers={"User-Agent": "Mozilla/5.0 MCAPI-Engine3/1.0"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        _news_calendar = data if isinstance(data, list) else []
        _news_last_fetch = datetime.utcnow()
        high_count = sum(1 for e in _news_calendar if e.get("impact") == "High")
        print(f"[ENGINE3] Calendar loaded: {len(_news_calendar)} events "
              f"({high_count} HIGH impact) this week")
    except Exception as e:
        print(f"[ENGINE3] Calendar fetch failed: {e} — passing all signals through")
        _news_last_fetch = datetime.utcnow()   # Prevent retry storm


def _check_news(symbol: str, action: str) -> tuple:
    """
    Engine 3: News gate. Returns (allowed, reason).
    HIGH impact: block 30 min before + 15 min after
    MEDIUM impact: block 15 min before only
    If calendar unavailable → PASS (never block on network error)
    """
    global _news_calendar, _news_last_fetch

    # Refresh calendar every 6 hours
    if (_news_last_fetch is None or
            (datetime.utcnow() - _news_last_fetch).total_seconds() > 21600):
        _fetch_news_calendar()

    if not _news_calendar:
        return True, "ENGINE3 ✅ Calendar unavailable — passing"

    currencies = NEWS_CURRENCIES.get(symbol.upper(), ["USD"])
    now = datetime.utcnow()
    upcoming = []

    for event in _news_calendar:
        impact  = event.get("impact", "")
        country = event.get("country", "")
        if impact not in ["High", "Medium"]:
            continue
        if country not in currencies:
            continue
        event_dt = _parse_ff_datetime(event.get("date",""), event.get("time",""))
        if not event_dt:
            continue

        minutes_to = (event_dt - now).total_seconds() / 60

        if impact == "High":
            # Block 30 min before and 15 min after
            if -15 <= minutes_to <= 30:
                return False, (
                    f"ENGINE3 ❌ NEWS BLOCK: {event.get('title','?')} "
                    f"({country} · HIGH) "
                    f"{'in ' + str(int(minutes_to)) + ' min' if minutes_to >= 0 else str(int(-minutes_to)) + ' min ago'}. "
                    f"Trading resumes when volatility settles."
                )
            if 0 <= minutes_to <= 120:
                upcoming.append(f"{event.get('title','?')} ({country}) in {int(minutes_to)}m")

        elif impact == "Medium":
            # Block 15 min before only
            if 0 <= minutes_to <= 15:
                return False, (
                    f"ENGINE3 ❌ NEWS BLOCK: {event.get('title','?')} "
                    f"({country} · MEDIUM) in {int(minutes_to)} min."
                )

    upcoming_str = " | Next: " + upcoming[0] if upcoming else ""
    return True, f"ENGINE3 ✅ News clear for {symbol}{upcoming_str}"


@app.route("/news", methods=["GET"])
def get_news():
    """Engine 3: Live news calendar status — Config Tab reads this"""
    if (_news_last_fetch is None or
            (datetime.utcnow() - _news_last_fetch).total_seconds() > 21600):
        _fetch_news_calendar()

    now = datetime.utcnow()
    upcoming_high = []
    for event in _news_calendar:
        if event.get("impact") != "High":
            continue
        event_dt = _parse_ff_datetime(event.get("date",""), event.get("time",""))
        if not event_dt:
            continue
        minutes_to = (event_dt - now).total_seconds() / 60
        if -60 <= minutes_to <= 240:
            upcoming_high.append({
                "title":      event.get("title"),
                "country":    event.get("country"),
                "time_utc":   event_dt.strftime("%H:%M UTC") if event_dt else "?",
                "minutes_to": round(minutes_to),
                "impact":     event.get("impact"),
            })

    upcoming_high.sort(key=lambda x: x["minutes_to"])
    return jsonify({
        "engine":          "3 — Calendar / News Filter",
        "status":          "LIVE",
        "last_fetch":      _news_last_fetch.isoformat() if _news_last_fetch else None,
        "total_events":    len(_news_calendar),
        "upcoming_high":   upcoming_high[:10],
        "timestamp":       now.isoformat(),
    })


# ENGINE 7 HELPERS — Session Risk Management
# ══════════════════════════════════════════════════════════════════════════════
def _e7_update_equity(equity: float):
    """Called every /status POST. Tracks drawdown from session start."""
    e7_risk["current_equity"] = equity
    if e7_risk["session_start_equity"] is None:
        e7_risk["session_start_equity"] = equity
        e7_risk["session_start_time"]   = datetime.utcnow().isoformat()
        print(f"[E7] Session equity initialised: ${equity:.2f}")
        return
    start  = e7_risk["session_start_equity"]
    dd_pct = round(((start - equity) / start * 100) if start > 0 else 0.0, 2)
    e7_risk["portfolio_drawdown_pct"] = dd_pct
    limit  = e7_risk["limits"]["max_drawdown_pct"]
    if dd_pct > limit and not e7_risk["portfolio_paused"]:
        e7_risk["portfolio_paused"] = True
        print(f"[E7] ⛔ PORTFOLIO PAUSED — drawdown {dd_pct:.1f}% > {limit}% limit")
    elif dd_pct <= limit and e7_risk["portfolio_paused"]:
        e7_risk["portfolio_paused"] = False
        print(f"[E7] ✅ Portfolio recovered to {dd_pct:.1f}% — trading resumed")

def _e7_record_close(symbol: str, profit: float):
    """Called when a trade closes. Updates session and consecutive loss counts."""
    sym = symbol.upper()
    if profit < 0:
        e7_risk["symbol_session_losses"][sym]  = e7_risk["symbol_session_losses"].get(sym, 0) + 1
        e7_risk["symbol_consecutive"][sym]     = e7_risk["symbol_consecutive"].get(sym, 0) + 1
        sl = e7_risk["symbol_session_losses"][sym]
        cl = e7_risk["symbol_consecutive"][sym]
        lim_s = e7_risk["limits"]["max_session_losses"]
        lim_c = e7_risk["limits"]["max_consecutive"]
        print(f"[E7] {sym} LOSS — session:{sl}/{lim_s} | consecutive:{cl}/{lim_c}")
        if sl >= lim_s or cl >= lim_c:
            e7_risk["symbol_paused"][sym] = True
            reason = "session losses" if sl >= lim_s else "consecutive losses"
            print(f"[E7] ⛔ {sym} PAUSED — {reason} limit reached")
    else:
        prev = e7_risk["symbol_consecutive"].get(sym, 0)
        if prev > 0:
            print(f"[E7] {sym} WIN — consecutive streak reset (was {prev})")
        e7_risk["symbol_consecutive"][sym] = 0
        if e7_risk["symbol_session_losses"].get(sym, 0) < e7_risk["limits"]["max_session_losses"]:
            e7_risk["symbol_paused"][sym] = False

def _e7_session_reset():
    """Reset session equity and per-symbol session counters."""
    equity = e7_risk.get("current_equity") or e7_risk.get("session_start_equity", 0)
    e7_risk["session_start_equity"]   = equity
    e7_risk["session_start_time"]     = datetime.utcnow().isoformat()
    e7_risk["portfolio_drawdown_pct"] = 0.0
    e7_risk["portfolio_paused"]       = False
    e7_risk["symbol_session_losses"]  = {}
    e7_risk["symbol_paused"]          = {}
    e7_risk["last_session_reset"]     = datetime.utcnow().isoformat()
    print(f"[E7] ═══ SESSION RESET ═══ New start equity: ${equity:.2f}")

def _e7_check_session_reset():
    """Check and execute session reset if due (04:00 UTC = 06:00 SAST)."""
    now = datetime.utcnow()
    if now.hour == e7_risk["limits"]["reset_hour_utc"] and now.minute < 31:
        last = e7_risk.get("last_session_reset")
        if last:
            if (now - datetime.fromisoformat(last)).total_seconds() > 3600:
                _e7_session_reset()
        else:
            _e7_session_reset()

def _e7_check_scanner(symbol: str) -> tuple:
    """Engine 7 scanner gate. Returns (allowed, reason)."""
    if e7_risk["portfolio_paused"]:
        dd  = e7_risk["portfolio_drawdown_pct"]
        lim = e7_risk["limits"]["max_drawdown_pct"]
        return False, (f"E7 ⛔ PORTFOLIO PAUSED — drawdown {dd:.1f}% > {lim}% limit. "
                       f"Resets at {e7_risk['limits']['reset_hour_utc']:02d}:00 UTC (06:00 SAST)")
    sym = symbol.upper()
    if e7_risk["symbol_paused"].get(sym):
        sl = e7_risk["symbol_session_losses"].get(sym, 0)
        cl = e7_risk["symbol_consecutive"].get(sym, 0)
        return False, (f"E7 ⛔ {sym} PAUSED — session:{sl} | consecutive:{cl}")
    dd = e7_risk["portfolio_drawdown_pct"]
    return True, f"E7 ✅ Risk OK — drawdown:{dd:.1f}% | {sym} session losses:{e7_risk['symbol_session_losses'].get(sym,0)}"

@app.route("/risk", methods=["GET"])
def get_risk():
    """Engine 7: Live session risk status — Config Tab reads this"""
    _e7_check_session_reset()
    return jsonify({
        "engine":                "7 — Session Risk Management",
        "session_start_equity":  e7_risk["session_start_equity"],
        "current_equity":        e7_risk["current_equity"],
        "portfolio_drawdown_pct":e7_risk["portfolio_drawdown_pct"],
        "portfolio_paused":      e7_risk["portfolio_paused"],
        "symbol_session_losses": e7_risk["symbol_session_losses"],
        "symbol_consecutive":    e7_risk["symbol_consecutive"],
        "symbol_paused":         e7_risk["symbol_paused"],
        "limits":                e7_risk["limits"],
        "last_session_reset":    e7_risk["last_session_reset"],
        "session_start_time":    e7_risk["session_start_time"],
        "timestamp":             datetime.utcnow().isoformat(),
    })

# ══════════════════════════════════════════════════════════════════════════════
# /live — MISSION CONTROL FEED
# Single endpoint that returns everything the Mission Control screen needs.
# Polled every 3 seconds by the Mission Control dashboard.
# Multi-account: each account's EA sends its account number in status POST.
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/live", methods=["GET"])
def get_live():
    """Mission Control: full system state in one call"""
    _e7_check_session_reset()

    # Build engine status summary
    engines = {
        "E1": {"name": "Regime",      "status": "PARTIAL" if not symbol_regimes else "LIVE",
               "detail": f"{len(symbol_regimes)} symbols classified"},
        "E2": {"name": "Session",     "status": "LIVE",    "detail": "market hours active"},
        "E4": {"name": "Personality", "status": "LIVE",    "detail": "6 symbols | AUDUSD blocked"},
        "E6": {"name": "Structure",   "status": "LIVE"    if bb_data else "WAITING",
               "detail": f"BB data: {len(bb_data)} symbols"},
        "E7": {"name": "Risk",        "status": "LIVE",
               "detail": f"drawdown {e7_risk['portfolio_drawdown_pct']:.1f}%"},
        "E3": {"name": "Calendar",    "status": "PLANNED", "detail": "news filter — next sprint"},
        "E9": {"name": "Memory",      "status": "PLANNED", "detail": "win rate scoring"},
        "E10":{"name": "Forensics",   "status": "PLANNED", "detail": "trade autopsy"},
    }

    # Last signal pipeline steps (from scan_results)
    pipeline_steps = []
    if scan_results.get("global_winner"):
        w = scan_results["global_winner"]
        pipeline_steps = [
            {"step": "Scanner",   "result": "PASS", "detail": f"picked {w.get('symbol','?')} {w.get('action','?')}"},
            {"step": "E1 Regime", "result": "PASS", "detail": symbol_regimes.get(w.get('symbol',''), 'NEUTRAL')},
            {"step": "E4 Engine", "result": "PASS", "detail": f"score {w.get('score','?')}/5"},
            {"step": "E6 Structure","result": scan_results.get("structure",{}).get("passed",True) and "PASS" or "FAIL",
             "detail": scan_results.get("structure",{}).get("reason","")[:60]},
            {"step": "E7 Risk",   "result": "PASS" if not e7_risk["portfolio_paused"] else "FAIL",
             "detail": f"drawdown {e7_risk['portfolio_drawdown_pct']:.1f}%"},
            {"step": "Signal",    "result": "FIRED", "detail": f"{w.get('symbol','?')} {w.get('action','?')} → EA"},
        ]
    elif scan_results.get("last_run"):
        pipeline_steps = [{"step": "Scanner", "result": "COMPLETE", "detail": "no signal this scan"}]

    # Account data from latest EA status
    acct = {
        "balance":    mt5_status.get("balance"),
        "equity":     mt5_status.get("equity"),
        "margin":     mt5_status.get("margin"),
        "free_margin":mt5_status.get("free_margin"),
        "daily_pnl":  mt5_status.get("daily_pnl"),
        "account_no": mt5_status.get("account"),
        "connected":  bool(mt5_status.get("timestamp")),
        "positions":  mt5_status.get("positions", []),
    }

    return jsonify({
        "version":        "7.6",
        "timestamp":      datetime.utcnow().isoformat(),
        "trading_enabled":trading_enabled,
        "pending_signal": pending_signal is not None,
        "last_scan":      scan_results.get("last_run"),
        "next_scan":      scan_results.get("next_run"),
        "last_winner":    scan_results.get("global_winner"),
        "pipeline_steps": pipeline_steps,
        "engines":        engines,
        "bb_live":        bb_data,
        "regimes":        symbol_regimes,
        "risk":           {
            "drawdown_pct":       e7_risk["portfolio_drawdown_pct"],
            "portfolio_paused":   e7_risk["portfolio_paused"],
            "session_start":      e7_risk["session_start_equity"],
            "current_equity":     e7_risk["current_equity"],
            "symbol_session_losses": e7_risk["symbol_session_losses"],
            "symbol_paused":      e7_risk["symbol_paused"],
            "limits":             e7_risk["limits"],
        },
        "account":        acct,
        "trade_count":    len(trade_history),
        "recent_trades":  trade_history[:5],
    })

# ─── Keep-alive ping endpoint ─────────────────────────────────────────────────
@app.route("/regime", methods=["GET"])
def get_regimes():
    """Engine 1: Current market regime per symbol"""
    return jsonify({
        "regimes": symbol_regimes,
        "timestamp": datetime.utcnow().isoformat(),
        "bullish": [s for s,r in symbol_regimes.items() if r=="BULLISH"],
        "bearish": [s for s,r in symbol_regimes.items() if r=="BEARISH"],
        "neutral": [s for s,r in symbol_regimes.items() if r=="NEUTRAL"]
    })

@app.route("/session", methods=["GET"])
def get_session():
    """Engine 2: Live market open/closed status per symbol"""
    status = get_session_status()
    open_syms   = [s for s,v in status.items() if v=="OPEN"]
    closed_syms = [s for s,v in status.items() if v=="CLOSED"]
    return jsonify({
        "timestamp": datetime.utcnow().isoformat(),
        "utc_hour":  datetime.utcnow().hour,
        "open":      open_syms,
        "closed":    closed_syms,
        "count_open": len(open_syms),
        "symbols":   status
    })

@app.route("/rotation", methods=["GET"])
def get_rotation():
    """Show current rotation guard — which symbols are blocked and when they expire"""
    now = datetime.utcnow()
    status = {}
    for sym, t in recently_traded.items():
        age_hours = (now - t).total_seconds() / 3600
        status[sym] = {"expires_in_hours": round(max(0, 6 - age_hours), 1)}
    return jsonify({"blocked": list(recently_traded.keys()),
                    "count": len(recently_traded), "detail": status})

@app.route("/rotation/clear", methods=["GET","POST"])
def clear_rotation():
    """Manually clear rotation guard — use when all symbols are stuck"""
    global recently_traded
    cleared = list(recently_traded.keys())
    recently_traded = {}
    print(f"[ROTATION] Manual clear — removed: {cleared}")
    return jsonify({"ok": True, "cleared": cleared,
                    "message": "All symbols available again ✅"})

@app.route("/personality", methods=["GET"])
def get_personality():
    """ENGINE 4: Return all symbol personalities + today's trade counts"""
    utc_hour = datetime.utcnow().hour
    session  = "London" if 8<=utc_hour<16 else "New York" if 13<=utc_hour<21 else "Asian"
    result   = {}
    for sym, cfg in PERSONALITY.items():
        result[sym] = {
            **cfg,
            "daily_count":  _daily_trades.get(sym, 0),
            "daily_remaining": max(0, cfg.get("max_daily", 3) - _daily_trades.get(sym, 0)),
            "session_open": session in cfg.get("sessions", [session]),
            "current_session": session
        }
    # Add defaults for symbols not in PERSONALITY
    return jsonify({
        "personalities": result,
        "session":       session,
        "daily_date":    _daily_date,
        "blacklisted":   [s for s, c in PERSONALITY.items() if c.get("blacklist")],
        "top_priority":  [s for s, c in PERSONALITY.items()
                          if c.get("priority", 1) >= 3 and not c.get("blacklist")],
        "timestamp":     datetime.utcnow().isoformat()
    })

@app.route("/personality/update", methods=["POST"])
def update_personality():
    """ENGINE 4: Update a symbol's personality settings from Command Centre"""
    try:
        data   = request.get_json(force=True)
        secret = data.get("secret","")
        if secret != "claude-market-2026":
            return jsonify({"error": "Invalid secret"}), 401
        symbol  = str(data.get("symbol","")).upper().strip()
        updates = data.get("settings", {})
        if not symbol or symbol not in PERSONALITY:
            return jsonify({"error": f"Unknown symbol: {symbol}"}), 400
        PERSONALITY[symbol].update(updates)
        print(f"[ENGINE4] {symbol} personality updated: {updates}")
        return jsonify({"ok": True, "symbol": symbol,
                        "personality": PERSONALITY[symbol]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/structure", methods=["GET"])
def get_structure():
    """ENGINE 6: Return current BB data and last structure check"""
    return jsonify({
        "bb_data":        bb_data,
        "last_check":     scan_results.get("structure", {}),
        "symbols_with_bb": list(bb_data.keys()),
        "timestamp":      datetime.utcnow().isoformat()
    })


    """Self-ping endpoint — keeps Render awake 24/7"""
    return jsonify({
        "alive": True,
        "time": datetime.utcnow().isoformat(),
        "scanner_last_run": scan_results.get("last_run"),
        "scanner_next_run": scan_results.get("next_run"),
        "trading": trading_enabled
    })

# ─── Close Signal (future auto-close support) ─────────────────────────────────
@app.route("/signal/close", methods=["POST"])
def post_close_signal():
    """Command Centre or server can send a close signal for a specific symbol"""
    global close_signal
    try:
        data = request.get_json(force=True)
        symbol = data.get("symbol","").upper()
        reason = data.get("reason","Manual close request")
        if not symbol:
            return jsonify({"error":"Missing symbol"}), 400
        close_signal = {"symbol": symbol, "reason": reason,
                        "timestamp": datetime.utcnow().isoformat()}
        print(f"[CLOSE] Signal set for {symbol} — {reason}")
        return jsonify({"ok": True, "symbol": symbol})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/signal/close", methods=["GET"])
def get_close_signal():
    """EA polls this for close requests"""
    global close_signal
    if not close_signal:
        return jsonify({"close": False})
    sig = close_signal
    close_signal = None
    print(f"[CLOSE] Signal delivered to EA: {sig['symbol']}")
    return jsonify({"close": True, **sig})

# ─── 3-Stage Global Scanner ───────────────────────────────────────────────────
def _claude_validate(symbol, action, price, sig_type):
    """Ask Claude AI to score a signal 1-5"""
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=50,
            messages=[{
                "role": "user",
                "content": (
                    f"Claude-Market signal validator. Score this {sig_type} signal 1-5.\n"
                    f"Symbol: {symbol} | Action: {action} | Price: {price}\n"
                    f"Consider: trend strength, volatility, time of day, market session.\n"
                    f"Reply with ONLY a single integer 1-5. Nothing else."
                )
            }]
        )
        text = resp.content[0].text.strip()
        score = int("".join(c for c in text if c.isdigit())[:1])
        return max(1, min(5, score))  # Cap at 1-5
    except Exception as e:
        print(f"[VALIDATE ERROR] {e}")
        return 3  # Default mid-score on failure

# ═══════════════════════════════════════════════════════════════════════════════
# MCAPI ENGINE 1 — MARKET REGIME ENGINE
# Classifies each asset as BULLISH / NEUTRAL / BEARISH before scanning
# This determines signal direction — fixes the "always BUY" problem
# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# MCAPI ENGINE 2 (mini) — SESSION INTELLIGENCE
# Filters closed markets from the scanner BEFORE Stage 1 even starts
# No more wasting signals on assets the broker won't accept
# ═══════════════════════════════════════════════════════════════════════════════
def is_asset_open(sym):
    """Return True if this asset is currently tradeable by market hours (UTC)"""
    now     = datetime.utcnow()
    weekday = now.weekday()          # 0=Mon … 6=Sun
    h       = now.hour + now.minute / 60.0   # decimal UTC hour

    # ── Crypto: always open 24/7 ──────────────────────────────────────────────
    if sym in ["BTCUSD","ETHUSD"]:
        return True

    # ── Weekend: only crypto is open ─────────────────────────────────────────
    if weekday == 6:                 # Sunday — forex opens 22:00 UTC
        return h >= 22.0
    if weekday == 5:                 # Saturday — all closed
        return False

    # ── Friday close — most markets close 22:00 UTC Friday ────────────────────
    if weekday == 4 and h >= 22.0:
        return False

    # ── US Indices: 14:30–21:00 UTC (16:30–23:00 SAST) ───────────────────────
    if sym in ["US_TECH100","US500","SPX500","SP500","NAS100","US_500","US_30"]:
        return 14.5 <= h <= 21.0

    # ── European Indices: 07:00–15:30 UTC ────────────────────────────────────
    if sym in ["GERMANY_40","UK_100","FRANCE_40"]:
        return 7.0 <= h <= 15.5

    # ── Asian Indices: 00:00–06:00 UTC ───────────────────────────────────────
    if sym in ["JAPAN_225"]:
        return h <= 6.0 or h >= 23.5

    # ── Forex, Metals, Oil, ZAR pairs: Mon–Fri broadly open ──────────────────
    # GOLD, SILVER, EURUSD, GBPUSD, USDZAR etc.
    return True


def get_session_status():
    """Return open/closed status for all assets — used by /session endpoint"""
    all_syms = []
    for assets in ASSET_GROUPS.values():
        all_syms.extend(assets)
    return {
        sym: "OPEN" if is_asset_open(sym) else "CLOSED"
        for sym in all_syms
    }



    """Engine 1: Classify market regime for each asset in a group"""
    try:
        utc_hour = datetime.utcnow().hour
        session  = "London/Frankfurt" if 8<=utc_hour<16 else "New York" if 13<=utc_hour<21 else "Asian/Off-hours"
        asset_list = ", ".join(assets)

        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"MCAPI Engine 1 — Market Regime Classification.\n"
                    f"Session: {session} ({utc_hour}:00 UTC). Group: {group_name}.\n"
                    f"Assets: {asset_list}\n\n"
                    f"For each asset, classify the current market regime:\n"
                    f"BULLISH = uptrend, buy pressure dominant\n"
                    f"BEARISH = downtrend, sell pressure dominant\n"
                    f"NEUTRAL = no clear direction, ranging\n\n"
                    f"Base on: typical current market conditions, session, asset behaviour.\n"
                    f"Reply ONLY with JSON using exact asset names from the list:\n"
                    f"{{\"EURUSD\":\"NEUTRAL\",\"GBPUSD\":\"BEARISH\",\"USDJPY\":\"BULLISH\"}}\n"
                    f"JSON only. All assets must be included."
                )
            }]
        )
        text  = resp.content[0].text.strip()
        print(f"[REGIME] {group_name} raw: {text[:120]}")
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])
            regimes = {}
            for sym in assets:
                raw = str(result.get(sym, "NEUTRAL")).upper().strip()
                regimes[sym] = raw if raw in ["BULLISH","BEARISH","NEUTRAL"] else "NEUTRAL"
            print(f"[REGIME] {group_name}: {regimes}")
            return regimes
        print(f"[REGIME] {group_name} — parse failed, defaulting NEUTRAL")
    except Exception as e:
        print(f"[REGIME ERROR] {group_name}: {e}")
    return {sym: "NEUTRAL" for sym in assets}


def _regime_action(regime):
    """Convert regime to allowed trade action"""
    if regime == "BULLISH": return "BUY"
    if regime == "BEARISH": return "SELL"

# ══════════════════════════════════════════════════════════════════════════════
# MCAPI ENGINE 1 ENHANCED — REGIME DIRECTION GATE
# Layer 1 Enhanced: No symbol BUYs in a BEARISH regime
#                   No symbol SELLs in a BULLISH regime
#                   NEUTRAL → both directions allowed (mean reversion)
#
# Data source: EA status POST → symbol_regimes dict (EMA50/200 + ADX H1)
# Per-symbol: GOLD can be BULLISH while BTCUSD is BEARISH simultaneously
# ══════════════════════════════════════════════════════════════════════════════
def _get_regime_action(symbol: str) -> str:
    """
    ENGINE 1 FULL: Determine correct trade direction from regime + BB location.

    BULLISH  → BUY  (trade with the uptrend — always)
    BEARISH  → SELL (trade with the downtrend — always)
    NEUTRAL  → direction from BB location:
               lower 50% of band → BUY  (bouncing from lower band)
               upper 50% of band → SELL (fading upper band)

    This replaces the hardcoded action="BUY" in the scanner.
    The result feeds into Claude's prompt so it knows the allowed direction.
    """
    regime = symbol_regimes.get(symbol.upper(), "NEUTRAL")
    if regime == "BULLISH":
        return "BUY"
    if regime == "BEARISH":
        return "SELL"
    # NEUTRAL / RANGING — use BB location to determine mean-reversion direction
    bb  = bb_data.get(symbol.upper(), {})
    loc = float(bb.get("location", 0.5)) if bb else 0.5
    return "BUY" if loc <= 0.50 else "SELL"


def _check_regime_direction(symbol, action, regime):
    """
    Engine 1 Enhanced: Does the proposed action match the current regime?
    Returns (allowed: bool, reason: str)

    BULLISH  → BUY only    (SELL blocked — trade WITH the trend)
    BEARISH  → SELL only   (BUY blocked — the key fix for BTC losses)
    NEUTRAL  → both OK     (mean reversion — lower BB BUY + upper BB SELL)
    No data  → BUY allowed (fail open — don't block due to missing regime data)
    """
    sym = symbol.upper()

    if not regime or regime == "NEUTRAL":
        return True, (f"ENGINE1 ✅ {sym} NEUTRAL — both directions allowed "
                      f"(mean reversion mode)")

    if regime == "BULLISH":
        if action == "BUY":
            return True, (f"ENGINE1 ✅ {sym} BULLISH + BUY → ALIGNED "
                          f"(trading with the trend)")
        else:
            return False, (f"ENGINE1 ❌ {sym} BULLISH regime — SELL blocked. "
                           f"Only BUY signals in an uptrend.")

    if regime == "BEARISH":
        if action == "SELL":
            return True, (f"ENGINE1 ✅ {sym} BEARISH + SELL → ALIGNED "
                          f"(trading with the trend)")
        else:
            return False, (f"ENGINE1 ❌ {sym} BEARISH regime — BUY blocked. "
                           f"Layer 1 Enhanced: never buy into a downtrend. "
                           f"Wait for regime to flip to NEUTRAL or BULLISH.")

    # Unknown regime — fail open, allow signal
    return True, f"ENGINE1 ✅ {sym} regime={regime} — unknown state, signal allowed"
    return None  # NEUTRAL = Claude decides


def _scan_group(group_name, assets):
    """Stage 1: Ask Claude to rank top 3 trading assets in a group"""
    try:
        asset_list = ", ".join(assets)
        utc_hour   = datetime.utcnow().hour
        session    = "London/Frankfurt" if 8<=utc_hour<16 else "New York" if 13<=utc_hour<21 else "Asian/Off-hours"
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"Claude-Market Scanner Stage 1. Session: {session}.\n"
                    f"Group: {group_name}. Assets: {asset_list}\n\n"
                    f"Pick the top 3 assets most suitable for trading right now.\n"
                    f"Reply ONLY with a JSON array using EXACT symbol names from the list.\n"
                    f"Example: [\"EURUSD\",\"GBPUSD\",\"USDJPY\"]\n"
                    f"JSON array only. No explanation."
                )
            }]
        )
        text = resp.content[0].text.strip()
        print(f"[SCAN1] {group_name} raw: {text[:80]}")
        start = text.find("[")
        end   = text.rfind("]") + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])
            # Only keep symbols that actually exist in our asset list
            valid = [s for s in result if s in assets]
            if valid:
                print(f"[SCAN1] {group_name} → {valid}")
                return valid[:3]
        print(f"[SCAN1] {group_name} — JSON parse failed, using fallback")
    except Exception as e:
        print(f"[SCAN1 ERROR] {group_name}: {e}")
    return assets[:3]  # Fallback

def _pick_group_winner(group_name, top5, recently):
    """Stage 2: Pick the single best asset from a group's top candidates"""
    try:
        avoid      = ", ".join(recently.keys()) if recently else "none"
        candidates = ", ".join(top5)
        utc_hour   = datetime.utcnow().hour
        session    = "London/Frankfurt" if 8<=utc_hour<16 else "New York" if 13<=utc_hour<21 else "Asian/Off-hours"
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": (
                    f"You are a trading signal selector. Session: {session}.\n"
                    f"Group: {group_name}\n"
                    f"Choose ONE symbol from this list: {candidates}\n"
                    f"Do not choose from recently traded: {avoid}\n\n"
                    f"Respond with ONLY a JSON object. No explanation before or after.\n"
                    f"Required fields:\n"
                    f"- symbol: must be one of [{candidates}]\n"
                    f"- action: BUY or SELL\n"
                    f"- confidence: LOW, MEDIUM, or HIGH\n"
                    f"- signal_type: RANGE or BB_BREAKOUT\n"
                    f"- reason: brief explanation\n\n"
                    f"JSON response:"
                )
            }]
        )
        text = resp.content[0].text.strip()
        print(f"[SCAN2] {group_name} raw: {text[:100]}")
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            r      = json.loads(text[start:end])
            raw_sym = str(r.get("symbol","")).strip()
            # v6.4: robust symbol matching — normalise both sides
            def norm(s): return s.upper().replace("/","").replace(" ","").replace("-","")
            norm_map = {norm(s): s for s in top5}
            sym = norm_map.get(norm(raw_sym), top5[0])
            if norm(raw_sym) not in norm_map:
                print(f"[SCAN2] '{raw_sym}' not matched — using {top5[0]}")
            action = str(r.get("action","BUY")).upper().strip()
            conf   = str(r.get("confidence","MEDIUM")).upper().strip()
            stype  = str(r.get("signal_type","RANGE")).upper().strip()
            reason = str(r.get("reason","Scanner pick"))
            if action not in ["BUY","SELL"]:           action = "BUY"
            if conf   not in ["LOW","MEDIUM","HIGH"]:  conf   = "MEDIUM"
            if stype  not in ["RANGE","BB_BREAKOUT"]:  stype  = "RANGE"
            result = {"symbol":sym,"action":action,"confidence":conf,
                      "signal_type":stype,"reason":reason,"support":0,"resistance":0}
            print(f"[SCAN2] {group_name} → {sym} {action} [{stype}] ({conf})")
            return result
        print(f"[SCAN2] {group_name} — JSON parse failed, using fallback")
    except Exception as e:
        print(f"[SCAN2 ERROR] {group_name}: {e}")
    # Always return a fallback winner — never leave Stage 2 empty
    fallback = {"symbol":top5[0],"action":"BUY","confidence":"LOW",
                "signal_type":"RANGE","reason":"Fallback pick","support":0,"resistance":0}
    print(f"[SCAN2] {group_name} fallback → {top5[0]}")
    return fallback

def _pick_global_winner(group_winners, recently):
    """Stage 3: Pick the best opportunity across all group winners"""
    try:
        candidates_json = json.dumps(group_winners, indent=2)
        avoid    = ", ".join(recently.keys()) if recently else "none"
        utc_hour = datetime.utcnow().hour
        session  = "London/Frankfurt" if 8<=utc_hour<16 else "New York" if 13<=utc_hour<21 else "Asian/Off-hours"
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"Claude-Market Scanner Stage 3 — Global Final.\n"
                    f"Session: {session} ({utc_hour}:00 UTC)\n"
                    f"Group winners:\n{candidates_json}\n"
                    f"Avoid: {avoid}\n\n"
                    f"Pick the SINGLE BEST global opportunity.\n"
                    f"Reply ONLY with this JSON:\n"
                    f"{{\"symbol\":\"GOLD\",\"action\":\"BUY\",\"score\":4,"
                    f"\"signal_type\":\"RANGE\",\"confidence\":\"HIGH\","
                    f"\"group\":\"Commodities\",\"reason\":\"one line reason\"}}\n"
                    f"score: 1-5. signal_type: RANGE or BB_BREAKOUT. JSON only."
                )
            }]
        )
        text = resp.content[0].text.strip()
        print(f"[SCAN3] Global raw: {text[:120]}")
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            r = json.loads(text[start:end])
            sym    = str(r.get("symbol","")).strip()
            action = str(r.get("action","BUY")).upper()
            score  = max(1, min(5, int(r.get("score", 3) or 3)))
            stype  = str(r.get("signal_type","RANGE")).upper()
            conf   = str(r.get("confidence","MEDIUM")).upper()
            reason = str(r.get("reason","Global scanner pick"))
            group  = str(r.get("group",""))
            if action not in ["BUY","SELL"]: action = "BUY"
            if stype not in ["RANGE","BB_BREAKOUT"]: stype = "RANGE"
            if conf not in ["LOW","MEDIUM","HIGH"]: conf = "MEDIUM"
            result = {"symbol":sym,"action":action,"score":score,"signal_type":stype,
                      "confidence":conf,"group":group,"reason":reason,
                      "support":0,"resistance":0,"broker_available":True}
            print(f"[SCAN3] Global winner: {sym} {action} Score:{score} [{stype}] ({conf})")
            return result
        print(f"[SCAN3] JSON parse failed")
    except Exception as e:
        print(f"[SCAN3 ERROR]: {e}")
    # Fallback — pick first group winner
    if group_winners:
        w = group_winners[0]
        w["score"] = 3
        print(f"[SCAN3] Fallback → {w.get('symbol')}")
        return w
    return None

def _add_to_history(symbol, action, score, sig_type):
    """Track recently traded assets for rotation"""
    global recently_traded
    entry = {"symbol": symbol, "action": action, "score": score,
             "type": sig_type, "time": datetime.utcnow().isoformat()}
    scan_results["history"].insert(0, entry)
    scan_results["history"] = scan_results["history"][:20]

    # v6.4: recently_traded with 6-hour expiry
    recently_traded[symbol] = datetime.utcnow()
    # Remove symbols older than 6 hours
    cutoff = datetime.utcnow() - timedelta(hours=6)
    expired = [s for s, t in recently_traded.items() if t < cutoff]
    for s in expired:
        del recently_traded[s]
        print(f"[ROTATION] {s} expired from rotation guard")

def run_scanner():
    """
    Simplified scanner v6.9 — uses same logic as Test Signal (known to work)
    1. Filter available symbols (market open, not blocked, not already open)
    2. Claude picks the best symbol and direction  
    3. Claude validates score (SAME as /webhook Test Signal)
    4. Fire signal in identical format to Test Signal
    """
    global pending_signal
    with scan_lock:
        print(f"\n[SCANNER] ═══ Starting — {datetime.utcnow().strftime('%H:%M UTC')} ═══")
        scan_results["last_run"] = datetime.utcnow().isoformat()
        next_mins = get_scan_interval() // 60
        scan_results["next_run"] = (datetime.utcnow() + timedelta(minutes=next_mins)).isoformat()

        if not trading_enabled:
            print("[SCANNER] Skipped — kill switch active")
            return

        # Engine 7: Check session reset + portfolio risk gate
        _e7_check_session_reset()
        e7_ok, e7_reason = _e7_check_scanner("PORTFOLIO")
        if not e7_ok:
            print(f"[ENGINE7] {e7_reason}")
            scan_results["e7_status"] = {"passed": False, "reason": e7_reason}
            return

        # ── Step 1: Clean rotation guard ─────────────────────────────────────
        cutoff = datetime.utcnow() - timedelta(hours=6)
        for s in [k for k, v in list(recently_traded.items()) if v < cutoff]:
            del recently_traded[s]
            print(f"[ROTATION] {s} expired — available again")
        print(f"[ROTATION] Blocked: {list(recently_traded.keys())}")

        # ── Step 2: Build available symbol list with ENGINE 4 filter ─────────────
        open_syms = {str(p.get("symbol","")).upper()
                     for p in mt5_status.get("positions",[])}

        utc_hour = datetime.utcnow().hour
        session  = "London" if 8<=utc_hour<16 else "New York" if 13<=utc_hour<21 else "Asian"

        # Base symbol list — ONLY symbols with open MT5 charts sending BB data
        # v7.7: Added EURUSD (2 forex) + US500 placeholder (confirm Ava symbol name)
        # 8 symbols: 2 metals + 2 crypto + 2 forex + 2 US indices
        # US500 will be blocked by Engine 6 until chart + EA is open in MT5
        all_syms = ["USDZAR","GOLD","SILVER","BTCUSD","ETHUSD","US_TECH100","EURUSD","US_500"]

        # Engine 4: Filter using personality
        available = []
        blocked_log = []
        for s in all_syms:
            if s in recently_traded:
                blocked_log.append(f"{s}(rotation)")
                continue
            if s in open_syms:
                blocked_log.append(f"{s}(open)")
                continue
            if not is_asset_open(s):
                blocked_log.append(f"{s}(closed)")
                continue
            allowed, reason = _personality_check(s, "BUY", session)
            if not allowed:
                blocked_log.append(f"{s}({reason.split(':')[0]})")
                continue
            available.append(s)

        # Engine 4: Sort by personality priority (highest first)
        available.sort(key=lambda s: _personality_priority(s), reverse=True)

        print(f"[ENGINE4] Session: {session}")
        print(f"[ENGINE4] Available (priority sorted): {available}")
        print(f"[ENGINE4] Blocked: {blocked_log}")

        if not available:
            print("[SCANNER] No available symbols after Engine 4 filter")
            scan_results["global_winner"] = None
            return

        # ── Step 3: ENGINE 1 FULL — Regime-aware action + Claude symbol pick ───
        # Determine the correct action for EACH symbol based on its regime.
        # BULLISH → BUY · BEARISH → SELL · NEUTRAL → BB location decides.
        # Claude then picks the BEST symbol — but action is regime-determined.
        regime_actions = {s: _get_regime_action(s) for s in available}

        sym    = available[0]   # Default = highest priority symbol
        action = regime_actions.get(sym, "BUY")
        try:
            # Build regime-aware priority hints for Claude — Engine 9 memory score included
            priority_hints = []
            for s in available[:6]:
                p = _get_personality(s)
                r = symbol_regimes.get(s.upper(), "NEUTRAL")
                a = regime_actions.get(s, "BUY")
                mem = _get_memory_score(s, r, session)
                hint = f"{s}(priority={p.get('priority',1)},regime={r},{a}"
                if p.get('priority',1) >= 3:
                    hint += ",TOP_PICK"
                if mem != 0:
                    hint += f",mem={mem:+d}"  # +1 = proven setup, -1 = struggling
                hint += ")"
                priority_hints.append(hint)

            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=80,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Session: {session} UTC. "
                        f"Pick the BEST symbol from: {', '.join(priority_hints)}.\n"
                        f"Each entry shows: name(priority,regime,action,mem=score). "
                        f"TOP_PICK = proven performer. mem=+1 = strong historical win rate in this setup. "
                        f"mem=-1 = this setup has been struggling — avoid if alternatives exist.\n"
                        f"Reply ONLY with JSON: {{\"symbol\":\"GOLD\",\"action\":\"BUY\"}}\n"
                        f"Use EXACTLY the action shown for your chosen symbol. JSON only."
                    )
                }]
            )
            text  = resp.content[0].text.strip()
            start = text.find("{")
            end   = text.rfind("}") + 1
            if start >= 0 and end > start:
                r   = json.loads(text[start:end])
                raw = str(r.get("symbol","")).upper().strip()
                sym = raw if raw in available else available[0]
            # Always use regime-determined action — not Claude's action field
            action = regime_actions.get(sym, "BUY")
            print(f"[ENGINE1] Regime actions: {regime_actions}")
            print(f"[ENGINE4] Claude picked: {sym} → action={action} (regime={symbol_regimes.get(sym.upper(),'NEUTRAL')})")
        except Exception as e:
            print(f"[ENGINE4] Claude pick error: {e} — using {sym} {action}")

        # Update scan_results so Command Centre shows the pick
        scan_results["stage1"] = {g: [s for s in a if is_asset_open(s)][:3]
                                   for g, a in ASSET_GROUPS.items()
                                   if any(is_asset_open(s) for s in a)}
        scan_results["stage2"] = {"Selected": {"symbol": sym, "action": action,
                                                "confidence": "MEDIUM", "signal_type": "BB_BREAKOUT",
                                                "reason": f"Scanner pick — {datetime.utcnow().strftime('%H:%M')} UTC"}}

        # ── Step 4: Validate — per-symbol min_score from Engine 4 ────────────
        final_sym    = None
        final_action = None
        final_score  = 0

        candidates_to_try = [sym] + [s for s in available if s != sym]

        for try_sym in candidates_to_try[:5]:
            try_action    = regime_actions.get(try_sym, "BUY")  # E1 Full: regime action
            sym_min_score = _personality_min_score(try_sym)
            score = _claude_validate(try_sym, try_action, "0", "BB")
            print(f"[ENGINE4] {try_sym} {try_action} → Score:{score}/5 (need≥{sym_min_score})")
            if score >= sym_min_score:
                final_sym    = try_sym
                final_action = try_action
                final_score  = score
                break
            elif score >= 2 and not final_sym:
                final_sym    = try_sym
                final_action = try_action
                final_score  = score

        if not final_sym:
            final_sym    = available[0]
            final_action = regime_actions.get(final_sym, "BUY")
            final_score  = _claude_validate(final_sym, final_action, "0", "BB")
            print(f"[ENGINE4] Using best available: {final_sym} {final_action} Score:{final_score}")

        print(f"[ENGINE4] Final pick: {final_sym} {final_action} Score:{final_score}/5")

        # ── Step 4.45: ENGINE 9 — Memory confidence gate ─────────────────────
        # Apply direction-aware win rate memory as a HARD GATE.
        # If this exact setup (symbol + direction + regime + session) has been
        # losing consistently — raise the bar or block entirely.
        mem_regime  = symbol_regimes.get(final_sym.upper(), "NEUTRAL")
        mem_adj     = _get_memory_confidence(final_sym, final_action, mem_regime, session)
        brain_threshold = _brain_config.get("symbols", {}).get(final_sym, {}).get(
                          "confidence_threshold_pct", 60)

        # Convert score to confidence %: 1→20, 2→40, 3→60, 4→80, 5→100
        base_confidence = final_score * 20
        adjusted_confidence = base_confidence + mem_adj

        print(f"[ENGINE9] {final_sym} {final_action}: base={base_confidence}% "
              f"mem_adj={mem_adj:+.0f}% adjusted={adjusted_confidence:.0f}% "
              f"threshold={brain_threshold}%")

        if adjusted_confidence < brain_threshold:
            # Memory shows this setup has poor track record — try alternative
            print(f"[ENGINE9] ❌ BLOCKED — adjusted confidence {adjusted_confidence:.0f}% "
                  f"< threshold {brain_threshold}%. Memory says: avoid {final_sym} {final_action}.")
            # Try next candidate
            e9_blocked = True
            for alt_sym in candidates_to_try[:5]:
                if alt_sym == final_sym:
                    continue
                alt_action = regime_actions.get(alt_sym, "BUY")
                alt_regime = symbol_regimes.get(alt_sym.upper(), "NEUTRAL")
                alt_score  = _claude_validate(alt_sym, alt_action, "0", "BB")
                alt_adj    = _get_memory_confidence(alt_sym, alt_action, alt_regime, session)
                alt_conf   = alt_score * 20 + alt_adj
                alt_thresh = _brain_config.get("symbols", {}).get(alt_sym, {}).get(
                             "confidence_threshold_pct", 60)
                if alt_conf >= alt_thresh:
                    print(f"[ENGINE9] ✅ Alternative: {alt_sym} {alt_action} conf={alt_conf:.0f}%")
                    final_sym    = alt_sym
                    final_action = alt_action
                    final_score  = alt_score
                    adjusted_confidence = alt_conf
                    e9_blocked = False
                    break
            if e9_blocked:
                print(f"[ENGINE9] All candidates blocked by memory — holding this scan")
                return
        else:
            print(f"[ENGINE9] ✅ PASS — confidence {adjusted_confidence:.0f}% ≥ {brain_threshold}%")
        e7_sym_ok, e7_sym_reason = _e7_check_scanner(final_sym)
        print(f"[ENGINE7] {e7_sym_reason}")
        if not e7_sym_ok:
            # Try alternatives that are not paused
            e7_found = False
            for alt_sym in candidates_to_try[:5]:
                if alt_sym == final_sym:
                    continue
                alt_ok, alt_reason = _e7_check_scanner(alt_sym)
                if alt_ok:
                    print(f"[ENGINE7] Alternative: {alt_sym} — {alt_reason}")
                    final_sym = alt_sym
                    e7_found  = True
                    break
            if not e7_found:
                print(f"[ENGINE7] All candidates paused by risk management — holding")
                return
        # BTC never BUYs in BEARISH. No symbol fights its regime.
        # Regime data comes from EA EMA50/200 + ADX H1 on each chart.
        regime     = symbol_regimes.get(final_sym.upper(), "NEUTRAL")
        reg_ok, reg_reason = _check_regime_direction(final_sym, final_action, regime)
        print(f"[ENGINE1] {reg_reason}")
        scan_results["regime_check"] = {
            "symbol": final_sym, "action": final_action,
            "regime": regime, "passed": reg_ok, "reason": reg_reason,
            "all_regimes": dict(symbol_regimes)
        }

        if not reg_ok:
            # Try alternatives whose regime aligns with the action
            regime_found = False
            for alt_sym in candidates_to_try[:5]:
                if alt_sym == final_sym:
                    continue
                alt_regime  = symbol_regimes.get(alt_sym.upper(), "NEUTRAL")
                alt_ok, alt_reason = _check_regime_direction(alt_sym, final_action, alt_regime)
                if alt_ok:
                    print(f"[ENGINE1] Alternative: {alt_sym} regime={alt_regime} — switching")
                    final_sym   = alt_sym
                    regime_found = True
                    break
            if not regime_found:
                print(f"[ENGINE1] All candidates blocked by regime — signal held. "
                      f"Waiting for regime alignment on next scan.")
                return

        # ── Step 4.6: ENGINE 5 — Volatility Gate ─────────────────────────────
        vol_ok, vol_reason = _check_volatility(final_sym)
        print(f"[ENGINE5] {vol_reason}")
        if not vol_ok:
            # Try alternative symbols with acceptable volatility
            vol_found = False
            for alt_sym in candidates_to_try[:5]:
                if alt_sym == final_sym:
                    continue
                alt_ok, alt_reason = _check_volatility(alt_sym)
                if alt_ok:
                    print(f"[ENGINE5] Alternative volatility-OK: {alt_sym}")
                    final_sym    = alt_sym
                    final_action = regime_actions.get(alt_sym, "BUY")
                    vol_found    = True
                    break
            if not vol_found:
                print(f"[ENGINE5] All candidates outside volatility range — holding")
                return

        # ── Step 4.6: ENGINE 3 — Calendar / News Filter ──────────────────────
        # Blocks signal if high-impact news event for this symbol's currencies
        # is within 30 minutes. Tries alternatives if available.
        news_ok, news_reason = _check_news(final_sym, final_action)
        print(f"[ENGINE3] {news_reason}")
        scan_results["news_check"] = {
            "symbol": final_sym, "passed": news_ok, "reason": news_reason
        }
        if not news_ok:
            # Try alternative symbols that are news-clear
            news_found = False
            for alt_sym in candidates_to_try[:5]:
                if alt_sym == final_sym:
                    continue
                alt_ok, alt_reason = _check_news(alt_sym, final_action)
                if alt_ok:
                    print(f"[ENGINE3] Alternative news-clear: {alt_sym}")
                    final_sym  = alt_sym
                    news_found = True
                    break
            if not news_found:
                print(f"[ENGINE3] All candidates news-blocked — holding until event passes")
                return

        # Is price at the RIGHT LOCATION on the Bollinger Bands?
        # Only applies to symbols with BB data (8 charted symbols)
        struct_ok, struct_reason = _check_structure(final_sym, final_action)
        print(f"[ENGINE6] {struct_reason}")
        scan_results["structure"] = {
            "symbol": final_sym, "action": final_action,
            "passed": struct_ok, "reason": struct_reason,
            "bb_data": bb_data.get(final_sym.upper(), {})
        }

        if not struct_ok:
            # Try next candidate with BB data before giving up
            print(f"[ENGINE6] {final_sym} blocked by structure — trying alternatives...")
            struct_override = False
            for alt_sym in candidates_to_try[:5]:
                if alt_sym == final_sym:
                    continue
                alt_ok, alt_reason = _check_structure(alt_sym, final_action)
                if alt_ok:
                    print(f"[ENGINE6] Alternative found: {alt_sym} — {alt_reason}")
                    final_sym = alt_sym
                    struct_override = True
                    break
            if not struct_override:
                # No good location found — check if BB data exists at all
                has_bb = any(bb_data.get(s.upper()) for s in candidates_to_try[:5])
                if has_bb:
                    print(f"[ENGINE6] All candidates at poor location — signal blocked")
                    return  # Wait for next scan when price may be better
                else:
                    print(f"[ENGINE6] No BB data available — structure check skipped")

        scan_results["global_winner"] = {
            "symbol": final_sym, "action": final_action, "score": final_score,
            "signal_type": "BB_BREAKOUT", "confidence": "MEDIUM",
            "reason": f"Auto-scanner — score {final_score}/5"
        }

        if pending_signal:
            print("[SCANNER] Signal already pending — skipping")
            return

        # ── Step 5: Fire signal — IDENTICAL format to Test Signal ─────────────
        pending_signal = {
            "symbol":      final_sym,
            "action":      final_action,
            "price":       "0",
            "score":       str(final_score),
            "signal_type": "BB_BREAKOUT",
            "source":      "scanner",
            "timestamp":   datetime.utcnow().isoformat()
        }
        _add_to_history(final_sym, final_action, final_score, "BB")
        _personality_record_trade(final_sym)
        print(f"[ENGINE4] ✅ Signal fired → {final_sym} {final_action} Score:{final_score}/5")

# ─── Daily Brain Analysis — runs at 04:00 UTC (06:00 SAST) ───────────────────

def _run_daily_brain_analysis():
    """
    ENGINE BRAIN: Runs daily at session reset (04:00 UTC).
    Reviews every symbol's last 7 days of trades.
    Claude generates per-symbol recommendations with suggested setting changes.
    Report stored in DB — Brain Settings loads it on open.
    """
    print("\n[BRAIN] ═══ Daily Analysis Starting ═══")
    cutoff = datetime.utcnow() - timedelta(days=7)
    symbols = ["GOLD","SILVER","USDZAR","EURUSD","BTCUSD","ETHUSD","US_TECH100","US_500"]
    report  = {
        "generated_at": datetime.utcnow().isoformat(),
        "generated_at_sast": (datetime.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M SAST"),
        "symbols": {}
    }

    for sym in symbols:
        # Pull last 7 days for this symbol
        sym_trades = [t for t in trade_history
                      if t.get("symbol","").upper() == sym
                      and t.get("received_at","") >= cutoff.isoformat()]

        wins   = [t for t in sym_trades if float(t.get("profit",0)) > 0]
        losses = [t for t in sym_trades if float(t.get("profit",0)) <= 0]
        total  = len(sym_trades)
        wr     = round(len(wins)/total*100) if total > 0 else 0
        pnl    = sum(float(t.get("profit",0)) for t in sym_trades)
        avg_w  = sum(float(t.get("profit",0)) for t in wins)   / len(wins)   if wins   else 0
        avg_l  = sum(float(t.get("profit",0)) for t in losses) / len(losses) if losses else 0

        # Direction breakdown
        buys  = [t for t in sym_trades if str(t.get("type","")).upper() == "BUY"]
        sells = [t for t in sym_trades if str(t.get("type","")).upper() == "SELL"]
        buy_wr  = round(len([t for t in buys  if float(t.get("profit",0))>0])/len(buys)*100)  if buys  else None
        sell_wr = round(len([t for t in sells if float(t.get("profit",0))>0])/len(sells)*100) if sells else None

        # Current brain settings for this symbol
        sym_cfg = _brain_config.get("symbols",{}).get(sym,{})

        # Ask Claude for recommendation
        recommendation = "No data — insufficient trades to analyse."
        action = "MAINTAIN"
        suggested = {}

        if total >= 2:
            try:
                prompt = f"""You are the MCAPI Brain analyst. Review this symbol and give a precise recommendation.

Symbol: {sym}
Last 7 days: {total} trades | Win rate: {wr}% | Net P&L: ${pnl:.2f}
Wins: {len(wins)} (avg +${avg_w:.2f}) | Losses: {len(losses)} (avg ${avg_l:.2f})
BUY win rate: {buy_wr}% ({len(buys)} trades) | SELL win rate: {sell_wr}% ({len(sells)} trades)
Current confidence threshold: {sym_cfg.get('confidence_threshold_pct',60)}%
Current BB zone: {sym_cfg.get('bb_zone_pct',30)}%
Current SL: {sym_cfg.get('sl_points',200)} points

Recent trades: {[f"{t.get('type')} ${t.get('profit')}" for t in sym_trades[-5:]]}

Respond in JSON only:
{{
  "action": "TIGHTEN" | "LOOSEN" | "PAUSE" | "MAINTAIN",
  "reason": "one sentence explanation",
  "recommendation": "2-3 sentences of specific advice",
  "suggested_changes": {{
    "confidence_threshold_pct": number or null,
    "bb_zone_pct": number or null,
    "sl_points": number or null
  }}
}}"""

                resp = client.messages.create(
                    model  = "claude-sonnet-4-6",
                    max_tokens = 300,
                    messages = [{"role":"user","content":prompt}]
                )
                text = resp.content[0].text.strip()
                s = text.find("{"); e = text.rfind("}") + 1
                if s >= 0 and e > s:
                    d = json.loads(text[s:e])
                    recommendation = d.get("recommendation","")
                    action         = d.get("action","MAINTAIN")
                    suggested      = {k:v for k,v in d.get("suggested_changes",{}).items() if v is not None}
                    print(f"[BRAIN] {sym}: {action} — {d.get('reason','')}")
            except Exception as e:
                print(f"[BRAIN] {sym} analysis error: {e}")
                recommendation = f"Win rate {wr}% over {total} trades. P&L: ${pnl:.2f}."
                action = "PAUSE" if (total >= 3 and wr == 0) else ("TIGHTEN" if wr < 45 else "MAINTAIN")

        report["symbols"][sym] = {
            "total": total, "wins": len(wins), "losses": len(losses),
            "win_rate": wr, "pnl": round(pnl,2),
            "avg_win": round(avg_w,2), "avg_loss": round(avg_l,2),
            "buy_wr": buy_wr, "sell_wr": sell_wr,
            "action": action, "recommendation": recommendation,
            "suggested_changes": suggested,
            "current_settings": {
                "confidence_threshold_pct": sym_cfg.get("confidence_threshold_pct",60),
                "bb_zone_pct": sym_cfg.get("bb_zone_pct",30),
                "sl_points": sym_cfg.get("sl_points",200),
                "active": sym_cfg.get("active",True)
            }
        }

    # Save to DB
    try:
        conn = _db_connect()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_daily_report (
                    id SERIAL PRIMARY KEY, report TEXT,
                    generated_at TIMESTAMP DEFAULT NOW()
                )""")
            cur.execute("INSERT INTO brain_daily_report (report) VALUES (%s)",
                        (json.dumps(report),))
            # Keep only last 30 reports
            cur.execute("""DELETE FROM brain_daily_report WHERE id NOT IN
                          (SELECT id FROM brain_daily_report ORDER BY generated_at DESC LIMIT 30)""")
            conn.commit(); cur.close(); conn.close()
            print("[BRAIN] ✅ Daily report saved to database")
    except Exception as e:
        print(f"[BRAIN] Report save error: {e}")

    print(f"[BRAIN] ═══ Daily Analysis Complete — {len(symbols)} symbols reviewed ═══\n")
    return report


# Latest report kept in memory for fast serving
_latest_brain_report = None

@app.route("/brain-report", methods=["GET"])
def get_brain_report():
    """Brain Settings loads this on open — shows latest daily analysis."""
    global _latest_brain_report
    # Try memory first
    if _latest_brain_report:
        return jsonify({"ok": True, "report": _latest_brain_report})
    # Try DB
    try:
        conn = _db_connect()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT report, generated_at FROM brain_daily_report ORDER BY generated_at DESC LIMIT 1")
            row = cur.fetchone()
            cur.close(); conn.close()
            if row:
                rpt = json.loads(row[0])
                _latest_brain_report = rpt
                return jsonify({"ok": True, "report": rpt})
    except Exception as e:
        print(f"[BRAIN] Report fetch error: {e}")
    return jsonify({"ok": False, "report": None, "message": "No report yet — runs daily at 04:00 UTC"})

@app.route("/brain-report/run", methods=["POST"])
def trigger_brain_report():
    """Manually trigger the daily analysis from Brain Settings."""
    global _latest_brain_report
    try:
        rpt = _run_daily_brain_analysis()
        _latest_brain_report = rpt
        return jsonify({"ok": True, "report": rpt})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/brain-ask", methods=["POST"])
def brain_ask():
    """Brain Settings AI chat — proxies browser questions through webhook."""
    try:
        data       = request.get_json(force=True)
        prompt     = data.get("prompt", "").strip()
        max_tokens = int(data.get("max_tokens", 600))
        if not prompt:
            return jsonify({"ok": False, "error": "No prompt provided"}), 400
        resp = client.messages.create(
            model      = "claude-sonnet-4-6",
            max_tokens = max_tokens,
            messages   = [{"role": "user", "content": prompt}]
        )
        answer = resp.content[0].text.strip()
        print(f"[BRAIN-ASK] tokens={max_tokens} Q: {prompt[:60]}...")
        return jsonify({"ok": True, "answer": answer})
    except Exception as e:
        print(f"[BRAIN-ASK] Error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


def daily_analysis_loop():
    """Thread: runs daily brain analysis at 04:00 UTC (06:00 SAST)."""
    global _latest_brain_report
    print("[BRAIN] Daily analysis thread started — fires at 04:00 UTC")
    while True:
        now = datetime.utcnow()
        # Calculate seconds until next 04:00 UTC
        target = now.replace(hour=4, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        print(f"[BRAIN] Next daily analysis in {int(wait//3600)}h {int((wait%3600)//60)}m (04:00 UTC)")
        time.sleep(wait)
        try:
            rpt = _run_daily_brain_analysis()
            _latest_brain_report = rpt
        except Exception as e:
            print(f"[BRAIN] Daily analysis error: {e}")


def get_scan_interval():
    """
    Dynamic scan interval based on active trading session.
    Reads from Brain config — adjustable without code change.
    """
    sys_cfg = _brain_config.get("system", {})
    h = datetime.utcnow().hour + datetime.utcnow().minute / 60.0

    if 13.5 <= h < 17.0:
        interval = sys_cfg.get("scan_overlap_min", 10) * 60
        session  = "London/NY overlap"
    elif 8.0 <= h < 13.5:
        interval = sys_cfg.get("scan_london_min", 20) * 60
        session  = "London"
    elif 17.0 <= h < 22.0:
        interval = sys_cfg.get("scan_newyork_min", 15) * 60
        session  = "New York"
    else:
        interval = sys_cfg.get("scan_asian_min", 45) * 60
        session  = "Asian/off-hours"

    print(f"[SCANNER] Next scan in {interval//60} min — {session} session ({h:.1f}h UTC)")
    return interval

def scanner_loop():
    time.sleep(60)  # Initial delay — let server start
    while True:
        try:
            run_scanner()
        except Exception as e:
            print(f"[SCANNER ERROR] {e}")
        time.sleep(get_scan_interval())

# ─── Self-Ping Thread — keeps Render free tier awake 24/7 ─────────────────────
def self_ping_loop():
    """Pings own /ping endpoint every 10 minutes — prevents Render sleep"""
    time.sleep(120)  # Wait for server to fully start
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not render_url:
        print("[PING] No RENDER_EXTERNAL_URL set — self-ping disabled (OK for local dev)")
        return
    ping_url = render_url.rstrip("/") + "/ping"
    print(f"[PING] Self-ping active → {ping_url} every 10 min")
    while True:
        try:
            urllib.request.urlopen(ping_url, timeout=15)
            print(f"[PING] ✅ Render kept awake — {datetime.utcnow().strftime('%H:%M UTC')}")
        except Exception as e:
            print(f"[PING] ⚠️  Self-ping failed: {e}")
        time.sleep(10 * 60)  # Every 10 minutes

threading.Thread(target=scanner_loop,        daemon=True).start()
threading.Thread(target=self_ping_loop,      daemon=True).start()
threading.Thread(target=daily_analysis_loop, daemon=True).start()

# ─── Database startup — load persisted trades ──────────────────────────────────
init_db()
load_trades_from_db()
_rebuild_memory()   # Engine 9: build win rate memory from trade history
_load_brain_config()  # Brain: load saved symbol/system settings

# ─── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"Claude-Market Webhook Server v6.9 — Engine 1 + Engine 2 SESSION FILTER — port {port}")
    print(f"Autonomous: scanner every 30min + self-ping every 10min")
    app.run(host="0.0.0.0", port=port)
