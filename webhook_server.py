"""
CLAUDE-MARKET WEBHOOK SERVER v4.0
====================================
NEW: Automatic Range Scanner (every 30 minutes)
  → Asks Claude AI if each asset is ranging or trending
  → If ranging: auto-generates BUY at support / SELL at resistance
  → EA picks it up within 10 seconds — fully automatic

NEW: Global Kill Switch
  GET /trading/stop   → stops ALL trading instantly
  GET /trading/resume → resumes ALL trading
  GET /trading/status → current state

All previous features:
  POST /webhook    → TradingView BB signals
  GET  /signal     → EA polls every 10s
  POST /status     → EA reports live data
  GET  /status     → Command Centre reads live data

Account: 107072723 — Lukas Ferreira — Pretoria 🇿🇦
"""

import os, json, smtplib, threading, time, anthropic
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import HTTPServer, BaseHTTPRequestHandler

SAST = timezone(timedelta(hours=2))

# ── GLOBAL STATE ──────────────────────────────────────────────────
pending_signal   = None
signal_lock      = threading.Lock()
trading_enabled  = True          # KILL SWITCH
trading_lock     = threading.Lock()

account_status   = {
    "balance":0,"equity":0,"margin":0,"free_margin":0,
    "daily_pnl":0,"leverage":0,"positions":[],"pos_count":0,
    "ea_version":"unknown","account":"107072723",
    "server":"Ava-Demo 1-MT5","last_update":"Never","connected":False
}
status_lock = threading.Lock()

range_scanner = {
    "last_run":    "Never",
    "next_run":    "In 30 minutes",
    "last_signal": "None",
    "signals_today": 0,
    "status":      "Starting...",
    "results":     {}
}
scanner_lock = threading.Lock()

# ── ENV VARS ──────────────────────────────────────────────────────
GMAIL_USER    = os.environ.get("GMAIL_USER",        "")
GMAIL_PASS    = os.environ.get("GMAIL_PASS",        "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SECRET        = os.environ.get("WEBHOOK_SECRET",    "claude-market-2026")

ASSETS = ["GOLD","SILVER","BTCUSD","ETHUSD","NAS100"]

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None


# ── REQUEST HANDLER ───────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[{datetime.now(SAST).strftime('%H:%M:%S')}] {format % args}")

    def send_json(self, code, data):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length",  len(body))
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global pending_signal, trading_enabled
        now = datetime.now(SAST).strftime("%H:%M SAST — %d %b %Y")

        # ── HEALTH ────────────────────────────────────────────────
        if self.path in ("/", "/health"):
            with trading_lock:
                te = trading_enabled
            self.send_json(200, {
                "status":          "Claude-Market Webhook Server v4.0",
                "time_sast":       now,
                "version":         "4.0",
                "account":         "107072723 — Lukas Ferreira",
                "trading_enabled": te,
                "pending_signal":  pending_signal is not None,
                "ea_connected":    account_status["connected"],
                "range_scanner":   range_scanner["status"]
            })

        # ── EA POLLS SIGNAL ───────────────────────────────────────
        elif self.path == "/signal":
            with trading_lock:
                te = trading_enabled
            if not te:
                self.send_json(200, {"signal": False, "reason": "Trading stopped by kill switch"})
                return
            with signal_lock:
                if pending_signal:
                    self.send_json(200, {
                        "signal": True,
                        "symbol": pending_signal["symbol"],
                        "action": pending_signal["action"],
                        "reason": pending_signal["reason"],
                        "score":  pending_signal["score"],
                        "type":   pending_signal.get("type", "BB"),
                        "time":   pending_signal["time"]
                    })
                else:
                    self.send_json(200, {"signal": False})

        # ── SIGNAL CLEAR ──────────────────────────────────────────
        elif self.path == "/signal/clear":
            with signal_lock:
                cleared        = pending_signal is not None
                pending_signal = None
            self.send_json(200, {"cleared": cleared, "message": "Ready for next signal"})

        # ── LIVE ACCOUNT DATA ─────────────────────────────────────
        elif self.path == "/status":
            with status_lock:
                data = dict(account_status)
            with trading_lock:
                data["trading_enabled"] = trading_enabled
            with scanner_lock:
                data["range_scanner"] = dict(range_scanner)
            self.send_json(200, data)

        # ── KILL SWITCH — STOP ────────────────────────────────────
        elif self.path == "/trading/stop":
            with trading_lock:
                trading_enabled = False
            msg = f"🔴 ALL TRADING STOPPED — {now}"
            print(msg)
            self.send_json(200, {"trading_enabled": False, "message": msg})

        # ── KILL SWITCH — RESUME ──────────────────────────────────
        elif self.path == "/trading/resume":
            with trading_lock:
                trading_enabled = True
            msg = f"🟢 TRADING RESUMED — {now}"
            print(msg)
            self.send_json(200, {"trading_enabled": True, "message": msg})

        # ── TRADING STATUS ────────────────────────────────────────
        elif self.path == "/trading/status":
            with trading_lock:
                te = trading_enabled
            with scanner_lock:
                rs = dict(range_scanner)
            self.send_json(200, {
                "trading_enabled": te,
                "status":          "RUNNING" if te else "STOPPED",
                "range_scanner":   rs
            })

        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        global pending_signal

        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except Exception as e:
            self.send_json(400, {"error": f"Invalid JSON: {e}"}); return

        # ── EA STATUS REPORT ──────────────────────────────────────
        if self.path == "/status":
            if data.get("secret") != SECRET:
                self.send_json(403, {"error": "Invalid secret"}); return
            now_str = datetime.now(SAST).strftime("%H:%M:%S SAST")
            with status_lock:
                account_status.update({
                    "balance":    data.get("balance",    0),
                    "equity":     data.get("equity",     0),
                    "margin":     data.get("margin",     0),
                    "free_margin":data.get("free_margin",0),
                    "daily_pnl":  data.get("daily_pnl",  0),
                    "leverage":   data.get("leverage",   0),
                    "positions":  data.get("positions",  []),
                    "pos_count":  data.get("pos_count",  0),
                    "ea_version": data.get("ea_version", "unknown"),
                    "last_update":now_str,
                    "connected":  True
                })
            print(f"Status: Bal=${data.get('balance',0):.2f} Pos:{data.get('pos_count',0)}")
            self.send_json(200, {"status": "updated", "time": now_str})
            return

        # ── TRADINGVIEW WEBHOOK ───────────────────────────────────
        if self.path == "/webhook":
            with trading_lock:
                te = trading_enabled
            if not te:
                self.send_json(200, {
                    "status": "rejected",
                    "reason": "Trading stopped by kill switch — use /trading/resume to restart"
                }); return

            if data.get("secret") != SECRET:
                self.send_json(403, {"error": "Invalid secret"}); return

            ticker = str(data.get("ticker", "")).upper().strip()
            action = str(data.get("action", "BUY")).upper().strip()
            price  = str(data.get("price",  "0"))
            sig_type = str(data.get("type", "BB"))

            if not ticker:
                self.send_json(400, {"error": "ticker required"}); return
            if action not in ("BUY","SELL"):
                self.send_json(400, {"error": "action must be BUY or SELL"}); return

            sym   = resolve_symbol(ticker)
            score = 4
            reason = f"{sig_type} signal"

            if client:
                try:
                    score, reason = validate_with_claude(sym, action, price, sig_type)
                except Exception as e:
                    print(f"Claude validation error: {e}")

            if score < 4:
                self.send_json(200, {"status":"rejected","score":score,"reason":reason}); return

            now_str = datetime.now(SAST).strftime("%Y.%m.%d %H:%M:%S")
            with signal_lock:
                pending_signal = {
                    "symbol": sym, "action": action, "reason": reason,
                    "score": score, "type": sig_type, "time": now_str
                }
            print(f"Signal stored: {sym} {action} {score}/5 [{sig_type}]")

            if GMAIL_USER and GMAIL_PASS:
                threading.Thread(target=send_email,
                    args=(sym,action,price,score,reason,sig_type),daemon=True).start()

            self.send_json(200, {
                "status": "signal_stored", "symbol": sym,
                "action": action, "score": f"{score}/5",
                "reason": reason, "type": sig_type,
                "message": "EA picks up within 10 seconds"
            })
            return

        self.send_json(404, {"error": "Not found"})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ── AUTOMATIC RANGE SCANNER ───────────────────────────────────────
# Runs every 30 minutes in background
# Asks Claude AI if each asset is ranging or trending
# If ranging → auto-generates signal → EA trades it
def range_scanner_loop():
    print("🔭 Range Scanner started — runs every 30 minutes")
    time.sleep(60)  # Wait 1 minute after startup before first scan

    while True:
        try:
            run_range_scan()
        except Exception as e:
            print(f"Range scanner error: {e}")
        time.sleep(1800)  # 30 minutes


def run_range_scan():
    global pending_signal

    with trading_lock:
        te = trading_enabled
    if not te:
        print("Range scanner skipped — trading stopped")
        return

    now_str = datetime.now(SAST).strftime("%H:%M SAST %d %b %Y")
    print(f"🔭 Range scan starting — {now_str}")

    with scanner_lock:
        range_scanner["status"]   = f"Scanning... {now_str}"
        range_scanner["last_run"] = now_str

    if not client:
        print("No Claude client — skipping range scan")
        return

    # Check if signal already pending — don't overwrite
    with signal_lock:
        if pending_signal:
            print("Range scan: signal already pending — skipping")
            return

    prompt = f"""You are the Claude-Market Range Scanner — an automatic trading agent.
Current time: {now_str}

Analyse each asset for RANGING (sideways) vs TRENDING market conditions.

Assets: GOLD, SILVER, BTCUSD, ETHUSD, NAS100

For RANGING markets (price bouncing between support and resistance):
- Bollinger Bands are NARROW (bands squeezing together)
- Price oscillates up and down without clear direction
- ATR (Average True Range) is LOW compared to recent history
- Common on weekends, holidays, low-volume periods
- Strategy: BUY at support, SELL at resistance for quick profits

For TRENDING markets (price moving strongly one direction):
- BB Breakout already handles this — DO NOT generate range signals

Your job: Find ONE asset that is currently RANGING and give a clear trade signal.

Respond in EXACTLY this format:
MARKET_MODE: RANGING or TRENDING
BEST_ASSET: [symbol or NONE]
ACTION: BUY or SELL or NONE
SUPPORT: [price level or N/A]
RESISTANCE: [price level or N/A]
SCORE: [4 or 5]
REASON: [one clear sentence explaining the range and entry]
CONFIDENCE: [LOW / MEDIUM / HIGH]

Rules:
- Only generate a signal if CONFIDENCE is MEDIUM or HIGH
- If all assets are clearly trending, output BEST_ASSET: NONE
- BUY when price is near SUPPORT (bottom of range)
- SELL when price is near RESISTANCE (top of range)
- Be conservative — only fire when you are sure"""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=200,
            messages=[{"role":"user","content":prompt}]
        )
        text = resp.content[0].text.strip()
        print(f"Range scan result:\n{text}")

        # Parse response
        mode       = "TRENDING"
        asset      = "NONE"
        action     = "NONE"
        score      = 4
        reason     = ""
        confidence = "LOW"

        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("MARKET_MODE:"):
                mode = line.split(":",1)[1].strip()
            elif line.startswith("BEST_ASSET:"):
                asset = line.split(":",1)[1].strip()
            elif line.startswith("ACTION:"):
                action = line.split(":",1)[1].strip()
            elif line.startswith("SCORE:"):
                try: score = int(line.split(":",1)[1].strip())
                except: pass
            elif line.startswith("REASON:"):
                reason = line.split(":",1)[1].strip()
            elif line.startswith("CONFIDENCE:"):
                confidence = line.split(":",1)[1].strip()

        with scanner_lock:
            range_scanner["results"][datetime.now(SAST).strftime("%H:%M")] = {
                "mode": mode, "asset": asset, "action": action,
                "confidence": confidence, "reason": reason
            }
            range_scanner["status"] = f"Last scan: {now_str} | Mode:{mode} | {asset} {action}"
            nxt = datetime.now(SAST) + timedelta(minutes=30)
            range_scanner["next_run"] = nxt.strftime("%H:%M SAST")

        # Generate signal if ranging conditions met
        if (mode == "RANGING" and
            asset != "NONE" and
            action in ("BUY","SELL") and
            confidence in ("MEDIUM","HIGH") and
            score >= 4):

            sym = resolve_symbol(asset)
            if sym:
                now_sig = datetime.now(SAST).strftime("%Y.%m.%d %H:%M:%S")
                full_reason = f"RANGE: {reason}"
                with signal_lock:
                    pending_signal = {
                        "symbol": sym, "action": action,
                        "reason": full_reason, "score": score,
                        "type":   "RANGE", "time": now_sig
                    }
                with scanner_lock:
                    range_scanner["last_signal"] = f"{sym} {action} {now_sig}"
                    range_scanner["signals_today"] += 1

                print(f"🎯 Range signal generated: {sym} {action} Score:{score} [{confidence}]")

                if GMAIL_USER and GMAIL_PASS:
                    threading.Thread(
                        target=send_email,
                        args=(sym,action,"auto",score,full_reason,"RANGE"),
                        daemon=True
                    ).start()
        else:
            print(f"Range scan: No signal generated — Mode:{mode} Asset:{asset} Confidence:{confidence}")

    except Exception as e:
        print(f"Range scan Claude error: {e}")
        with scanner_lock:
            range_scanner["status"] = f"Error: {e}"


# ── HELPERS ───────────────────────────────────────────────────────
def resolve_symbol(ticker):
    mapping = {
        "XAUUSD":"GOLD","GOLD":"GOLD","XAGUSD":"SILVER","SILVER":"SILVER",
        "BTCUSD":"BTCUSD","BTC":"BTCUSD","ETHUSD":"ETHUSD","ETH":"ETHUSD",
        "NAS100":"NAS100","US100":"NAS100","NASDAQ":"NAS100","US_TECH100":"NAS100"
    }
    return mapping.get(ticker, ticker)


def validate_with_claude(symbol, action, price, sig_type="BB"):
    prompt = f"""Trading signal:
Symbol: {symbol} | Action: {action} | Price: {price} | Type: {sig_type}

Score 1-5:
Box 1 — Major tradeable asset?
Box 2 — {action} correct direction for {sig_type} signal?
Box 3 — Price level reasonable?
Box 4 — 2% risk rule OK?
Box 5 — Signal confirmed?

SCORE: X
REASON: one sentence"""

    resp = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=100,
        messages=[{"role":"user","content":prompt}]
    )
    text = resp.content[0].text.strip()
    score=4; reason=f"{sig_type} validated"
    for line in text.split("\n"):
        line=line.strip()
        if line.startswith("SCORE:"):
            try: score=int(line.replace("SCORE:","").strip().split("/")[0])
            except: pass
        elif line.startswith("REASON:"):
            reason=line.replace("REASON:","").strip()
    return score, reason


def send_email(symbol, action, price, score, reason, sig_type="BB"):
    try:
        now_str = datetime.now(SAST).strftime("%H:%M SAST — %d %b %Y")
        emoji   = "🔄" if sig_type=="RANGE" else "📈"
        subject = f"{emoji} CM {sig_type}: {action} {symbol} — Score {score}/5"
        body = (f"Claude-Market {sig_type} Signal\n\n"
                f"Time: {now_str}\nSymbol: {symbol}\nAction: {action}\n"
                f"Type: {sig_type} ({'Range Trading' if sig_type=='RANGE' else 'BB Breakout'})\n"
                f"Score: {score}/5\nReason: {reason}\n\n"
                f"EA placing trade automatically.\nAccount: 107072723")
        msg = MIMEMultipart()
        msg["From"]=GMAIL_USER; msg["To"]=GMAIL_USER; msg["Subject"]=subject
        msg.attach(MIMEText(body,"plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
            s.login(GMAIL_USER,GMAIL_PASS); s.send_message(msg)
        print(f"Email: {subject}")
    except Exception as e:
        print(f"Email failed: {e}")


# ── MAIN ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    # Start range scanner background thread
    scanner_thread = threading.Thread(target=range_scanner_loop, daemon=True)
    scanner_thread.start()

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Claude-Market Webhook Server v4.0")
    print(f"Port: {port}")
    print(f"Features: BB Signals + Auto Range Scanner + Kill Switch")
    print(f"Endpoints:")
    print(f"  GET  /              — health")
    print(f"  GET  /signal        — EA polls")
    print(f"  GET  /signal/clear  — EA clears")
    print(f"  POST /webhook       — TradingView + Range signals")
    print(f"  POST /status        — EA reports")
    print(f"  GET  /status        — Command Centre reads")
    print(f"  GET  /trading/stop  — KILL SWITCH STOP")
    print(f"  GET  /trading/resume — KILL SWITCH RESUME")
    print(f"  GET  /trading/status — current state")
    server.serve_forever()
