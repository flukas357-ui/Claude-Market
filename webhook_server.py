"""   
Claude-Market Webhook Server v7.0
Lukas Ferreira - Pretoria ZA
MCAPI ENGINE 4 — ASSET PERSONALITY
v7.0: Per-symbol personality config
      AUDUSD blacklisted (Sprint 0: 0% win rate, -$526)
      USDZAR top priority (Sprint 0: 100% win rate, +$339)
      Per-symbol: min_score, sessions, max_daily, sl/tp/trail settings
      Scanner sorts by priority before picking
      /personality GET endpoint for Command Centre
      /personality/update POST endpoint for live config changes
Model: claude-sonnet-4-6
"""

from flask import Flask, request, jsonify
import anthropic
import threading
import time
import json
import urllib.request
from datetime import datetime, timedelta
import os

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

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
        "priority":       1,
        "min_score":      4,          # Sprint 0: 0/1. Higher bar.
        "sessions":       ["London","New York"],
        "max_daily":      2,
        "sl_points":      30,
        "tp_ratio":       1.5,
        "trail_pct":      20,
        "location_zone":  0.33,
        "notes":          "Tight range. Needs strong session alignment."
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
        "version": "6.9",
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

# ─── EA Status Receiver ────────────────────────────────────────────────────────
@app.route("/status", methods=["POST"])
def receive_status():
    global mt5_status, trade_history, symbol_regimes
    try:
        data = request.get_json(force=True)
        mt5_status = data
        mt5_status["received_at"] = datetime.utcnow().isoformat()

        # v6.6 Engine 1: Read REAL regime from EA (EMA50/200 + ADX14 calculated in MQL5)
        ea_sym    = str(data.get("symbol","")).strip().upper()
        ea_regime = str(data.get("regime","NEUTRAL")).upper().strip()
        if ea_sym and ea_regime in ["BULLISH","BEARISH","NEUTRAL"]:
            if symbol_regimes.get(ea_sym) != ea_regime:  # Only log on change
                print(f"[REGIME] {ea_sym} = {ea_regime} (EA: EMA50/200+ADX14 H1)")
            symbol_regimes[ea_sym] = ea_regime
            scan_results["regimes"] = dict(symbol_regimes)

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
            deleted_tickets.add(ticket)           # v5.5: block EA from re-posting
            before = len(trade_history)
            trade_history = [t for t in trade_history if str(t.get("ticket", "")) != ticket]
            removed = before - len(trade_history)
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

def ping():
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
    if sym in ["US_TECH100","NAS100","US_500","US_30"]:
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
        scan_results["next_run"] = (datetime.utcnow() + timedelta(minutes=30)).isoformat()

        if not trading_enabled:
            print("[SCANNER] Skipped — kill switch active")
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

        # Base symbol list — all non-blacklisted
        all_syms = ["GOLD","SILVER","BTCUSD","ETHUSD","EURUSD",
                    "GBPUSD","USDJPY","USDZAR","GBPJPY","AUDUSD","USDCAD","AUDJPY"]

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

        # ── Step 3: Claude picks best symbol (personality-aware) ─────────────
        sym    = available[0]   # Default = highest priority symbol
        action = "BUY"
        try:
            # Build priority hints for Claude
            priority_hints = []
            for s in available[:6]:
                p = _get_personality(s)
                hint = f"{s}(priority={p.get('priority',1)}"
                if p.get('priority',1) >= 3:
                    hint += ",TOP_PICK"
                hint += ")"
                priority_hints.append(hint)

            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=80,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Session: {session} UTC. "
                        f"Pick the BEST trade from: {', '.join(priority_hints)}.\n"
                        f"Higher priority = stronger historical performance. "
                        f"TOP_PICK symbols are proven performers.\n"
                        f"Reply ONLY with JSON: {{\"symbol\":\"GOLD\",\"action\":\"BUY\"}}\n"
                        f"action must be BUY or SELL. symbol must be from the list. JSON only."
                    )
                }]
            )
            text  = resp.content[0].text.strip()
            start = text.find("{")
            end   = text.rfind("}") + 1
            if start >= 0 and end > start:
                r      = json.loads(text[start:end])
                raw    = str(r.get("symbol","")).upper().strip()
                act    = str(r.get("action","BUY")).upper().strip()
                sym    = raw    if raw in available else available[0]
                action = act    if act in ["BUY","SELL"] else "BUY"
            print(f"[ENGINE4] Claude picked: {sym} {action}")
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
            sym_min_score = _personality_min_score(try_sym)
            score = _claude_validate(try_sym, action, "0", "BB")
            print(f"[ENGINE4] {try_sym} {action} → Score:{score}/5 (need≥{sym_min_score})")
            if score >= sym_min_score:
                final_sym    = try_sym
                final_action = action
                final_score  = score
                break
            elif score >= 2 and not final_sym:
                final_sym    = try_sym
                final_action = action
                final_score  = score

        if not final_sym:
            final_sym    = available[0]
            final_action = action
            final_score  = _claude_validate(final_sym, final_action, "0", "BB")
            print(f"[ENGINE4] Using best available: {final_sym} Score:{final_score}")

        print(f"[ENGINE4] Final pick: {final_sym} {final_action} Score:{final_score}/5")

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

# ─── Background Scanner Thread ─────────────────────────────────────────────────
def scanner_loop():
    time.sleep(60)  # Initial delay — let server start
    while True:
        try:
            run_scanner()
        except Exception as e:
            print(f"[SCANNER ERROR] {e}")
        time.sleep(30 * 60)  # 30-minute cycle

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

threading.Thread(target=scanner_loop,   daemon=True).start()
threading.Thread(target=self_ping_loop, daemon=True).start()

# ─── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"Claude-Market Webhook Server v6.9 — Engine 1 + Engine 2 SESSION FILTER — port {port}")
    print(f"Autonomous: scanner every 30min + self-ping every 10min")
    app.run(host="0.0.0.0", port=port)
