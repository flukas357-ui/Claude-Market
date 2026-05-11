""" 
Claude-Market Webhook Server v6.0
Lukas Ferreira - Pretoria ZA
Features: 3-Stage Global Scanner, Kill Switch, Live MT5 Status, Range Detection
v6.0: AUTONOMOUS — self-ping keeps Render awake 24/7, scanner never stops
      Fixed Ava broker symbol names (US_TECH100, US_500, US_30, GERMANY_40)
      Scanner fires both RANGE and BB_BREAKOUT signals
      Added /ping endpoint + /signal/close for future auto-close
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
    "history": []
}
recently_traded  = []
scan_lock        = threading.Lock()

# ─── Asset Universe — Ava broker symbol names ─────────────────────────────────
ASSET_GROUPS = {
    "Forex Majors":  ["EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","USDCAD","NZDUSD","EURGBP","EURJPY"],
    "Forex Minors":  ["GBPJPY","AUDJPY","CADJPY","CHFJPY","EURAUD","EURNZD","GBPAUD","AUDCAD","NZDCAD"],
    "Indices":       ["US_TECH100","US_500","US_30","GERMANY_40","UK_100","JAPAN_225","FRANCE_40"],
    "Commodities":   ["GOLD","SILVER","CrudeOIL","COPPER","USDZAR"],
    "Crypto":        ["BTCUSD","ETHUSD"],
    "SA & Emerging": ["USDZAR","EURZAR","GBPZAR"]
}

# ─── Health Check ─────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "Claude-Market Webhook Server",
        "version": "6.0",
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
    global mt5_status, trade_history
    try:
        data = request.get_json(force=True)
        mt5_status = data
        mt5_status["received_at"] = datetime.utcnow().isoformat()

        # Extract closed trades if EA sends them
        for t in data.get("closed_trades", []):
            tickets = [x["ticket"] for x in trade_history]
            if t.get("ticket") not in tickets:
                trade_history.insert(0, t)
        trade_history = trade_history[:200]  # Keep last 200

        return jsonify({"ok": True})
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
    return jsonify({
        "trading_enabled": trading_enabled,
        "pending_signal": pending_signal is not None,
        "recently_traded": recently_traded
    })

# ─── Scanner Intelligence Endpoints ───────────────────────────────────────────
@app.route("/scanner/results", methods=["GET"])
def scanner_results():
    return jsonify(scan_results)

@app.route("/scanner/run", methods=["GET","POST"])
def manual_scan():
    threading.Thread(target=run_scanner, daemon=True).start()
    return jsonify({"status": "Scanner triggered manually"})

# ─── Keep-alive ping endpoint ─────────────────────────────────────────────────
@app.route("/ping", methods=["GET"])
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

def _scan_group(group_name, assets):
    """Stage 1: Ask Claude to rank top 5 ranging assets in a group"""
    try:
        asset_list = ", ".join(assets)
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"Claude-Market Range Scanner — Stage 1.\n"
                    f"Group: {group_name}\n"
                    f"Assets: {asset_list}\n"
                    f"Current UTC time: {datetime.utcnow().strftime('%H:%M %A')}\n\n"
                    f"Analyse these assets for RANGING (sideways) market conditions.\n"
                    f"Consider: Bollinger Band squeeze, low ATR, consolidation, support/resistance.\n\n"
                    f"Reply ONLY with a JSON array of the top 5 symbols most likely ranging right now.\n"
                    f"Format: [\"SYM1\",\"SYM2\",\"SYM3\",\"SYM4\",\"SYM5\"]\n"
                    f"Use exact symbol names from the list. JSON only, no explanation."
                )
            }]
        )
        text = resp.content[0].text.strip()
        start = text.find("[")
        end   = text.rfind("]") + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])
            return [s for s in result if s in assets][:5]
    except Exception as e:
        print(f"[SCAN ERROR] Stage1 {group_name}: {e}")
    return assets[:3]  # Fallback

def _pick_group_winner(group_name, top5, recently):
    """Stage 2: Pick the single best ranging asset from a group's top 5"""
    try:
        avoid = ", ".join(recently) if recently else "none"
        candidates = ", ".join(top5)
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"Claude-Market Range Scanner — Stage 2.\n"
                    f"Group: {group_name}\n"
                    f"Candidates: {candidates}\n"
                    f"Recently traded (avoid): {avoid}\n\n"
                    f"Pick the SINGLE BEST ranging opportunity from the candidates.\n"
                    f"Do NOT pick from the recently traded list.\n\n"
                    f"Reply ONLY with this exact JSON format:\n"
                    f"{{\"symbol\":\"SYMBOL\",\"action\":\"BUY\",\"support\":0.0,\"resistance\":0.0,"
                    f"\"confidence\":\"HIGH\",\"reason\":\"brief reason\"}}\n"
                    f"action must be BUY (near support) or SELL (near resistance).\n"
                    f"JSON only, no explanation."
                )
            }]
        )
        text = resp.content[0].text.strip()
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])
            if result.get("symbol") and result.get("action"):
                return result
    except Exception as e:
        print(f"[SCAN ERROR] Stage2 {group_name}: {e}")
    return None

def _pick_global_winner(group_winners, recently):
    """Stage 3: Pick the best opportunity across all group winners"""
    try:
        candidates_json = json.dumps(group_winners, indent=2)
        avoid = ", ".join(recently) if recently else "none"
        utc_hour = datetime.utcnow().hour
        session = "London/Frankfurt" if 8<=utc_hour<16 else "New York" if 13<=utc_hour<21 else "Asian/Off-hours"
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": (
                    f"Claude-Market Scanner — Stage 3: Global Final.\n"
                    f"Current session: {session} ({utc_hour}:00 UTC)\n"
                    f"Group winners to compare:\n{candidates_json}\n"
                    f"Recently traded (avoid): {avoid}\n\n"
                    f"Select the SINGLE BEST global trading opportunity.\n"
                    f"Consider: session suitability, confidence, typical range width, volatility.\n"
                    f"Set signal_type to RANGE if market is ranging/sideways, BB_BREAKOUT if trending/breaking.\n\n"
                    f"Reply ONLY with this JSON:\n"
                    f"{{\"symbol\":\"SYM\",\"action\":\"BUY\",\"score\":4,"
                    f"\"signal_type\":\"RANGE\",\"group\":\"GroupName\","
                    f"\"support\":0.0,\"resistance\":0.0,"
                    f"\"confidence\":\"HIGH\",\"reason\":\"explanation\","
                    f"\"broker_available\":true}}\n"
                    f"action: BUY or SELL. score: 1-5. JSON only."
                )
            }]
        )
        text = resp.content[0].text.strip()
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except Exception as e:
        print(f"[SCAN ERROR] Stage3 global: {e}")
    return None

def _add_to_history(symbol, action, score, sig_type):
    """Track recently traded assets for rotation"""
    global recently_traded
    entry = {"symbol": symbol, "action": action, "score": score,
             "type": sig_type, "time": datetime.utcnow().isoformat()}
    scan_results["history"].insert(0, entry)
    scan_results["history"] = scan_results["history"][:20]

    # Keep rotation list for 24h
    if symbol not in recently_traded:
        recently_traded.append(symbol)
    # Clear entries older than 24h (simple: keep last 12)
    recently_traded = recently_traded[-12:]

def run_scanner():
    """Full 3-stage scan — runs every 30 minutes"""
    global pending_signal
    with scan_lock:
        print(f"\n[SCANNER] Starting 3-stage scan — {datetime.utcnow().strftime('%H:%M UTC')}")
        scan_results["last_run"] = datetime.utcnow().isoformat()
        scan_results["next_run"] = (datetime.utcnow() + timedelta(minutes=30)).isoformat()

        if not trading_enabled:
            print("[SCANNER] Skipped — kill switch active")
            return

        # ── STAGE 1: Scan all 6 groups ────────────────────────────────────────
        print("[SCANNER] Stage 1: Scanning 6 groups × 9 assets...")
        stage1 = {}
        for group, assets in ASSET_GROUPS.items():
            top5 = _scan_group(group, assets)
            stage1[group] = top5
            print(f"  {group}: {top5}")
            time.sleep(1)  # Rate limit spacing
        scan_results["stage1"] = stage1

        # ── STAGE 2: Pick winner per group ────────────────────────────────────
        print("[SCANNER] Stage 2: Picking group winners...")
        stage2 = {}
        for group, top5 in stage1.items():
            if not top5: continue
            winner = _pick_group_winner(group, top5, recently_traded)
            if winner:
                stage2[group] = winner
                print(f"  {group} winner: {winner.get('symbol')} {winner.get('action')} ({winner.get('confidence')})")
            time.sleep(1)
        scan_results["stage2"] = stage2

        # ── STAGE 3: Global champion ───────────────────────────────────────────
        if not stage2:
            print("[SCANNER] No group winners found")
            return

        print("[SCANNER] Stage 3: Selecting global champion...")
        global_winner = _pick_global_winner(list(stage2.values()), recently_traded)

        if not global_winner:
            print("[SCANNER] No global winner selected")
            return

        sym      = global_winner.get("symbol", "")
        action   = str(global_winner.get("action", "BUY")).upper()
        score    = int(global_winner.get("score", 3))
        score    = max(1, min(5, score))
        conf     = global_winner.get("confidence", "MEDIUM")
        sig_type = global_winner.get("signal_type", "RANGE").upper()
        if sig_type not in ["RANGE", "BB_BREAKOUT"]: sig_type = "RANGE"

        print(f"[SCANNER] ★ GLOBAL WINNER: {sym} {action} Score:{score} Type:{sig_type} Conf:{conf}")
        scan_results["global_winner"] = global_winner

        if conf not in ["MEDIUM", "HIGH"] or score < 3:
            print(f"[SCANNER] Confidence too low ({conf}) — no trade fired")
            return

        if sym in recently_traded:
            print(f"[SCANNER] {sym} recently traded — rotation guard active")
            return

        if pending_signal:
            print("[SCANNER] Signal already pending — skipping")
            return

        pending_signal = {
            "symbol":      sym,
            "action":      action,
            "price":       "0",
            "score":       str(score),
            "signal_type": sig_type,
            "confidence":  conf,
            "reason":      global_winner.get("reason", ""),
            "source":      "scanner",
            "timestamp":   datetime.utcnow().isoformat()
        }
        _add_to_history(sym, action, score, sig_type)
        print(f"[SCANNER] ✅ Signal fired → {sym} {action} [{sig_type}] Score:{score}")

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
    print(f"Claude-Market Webhook Server v6.0 — port {port}")
    print(f"Autonomous: scanner every 30min + self-ping every 10min")
    app.run(host="0.0.0.0", port=port)
