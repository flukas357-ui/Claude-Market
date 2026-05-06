"""
CLAUDE-MARKET WEBHOOK SERVER v3.2
====================================
NEW: /status POST — EA reports live account data every 60s
NEW: /status GET  — Command Centre reads live data
All previous endpoints retained

ENDPOINTS:
  GET  /            — health check
  GET  /signal      — EA polls every 10s
  GET  /signal/clear — EA calls after trade
  POST /webhook     — TradingView sends signals
  POST /status      — EA posts live account data every 60s
  GET  /status      — Command Centre reads live data

Account: 107072723 — Lukas Ferreira
Server:  Ava-Demo 1-MT5
"""

import os
import json
import smtplib
import threading
import anthropic

from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import HTTPServer, BaseHTTPRequestHandler

SAST = timezone(timedelta(hours=2))

# ── IN-MEMORY STORES ──────────────────────────────────────────────
pending_signal   = None
signal_lock      = threading.Lock()
last_signal_time = None

# Live account data from EA
account_status   = {
    "balance":    0,
    "equity":     0,
    "margin":     0,
    "free_margin":0,
    "daily_pnl":  0,
    "leverage":   0,
    "positions":  [],
    "pos_count":  0,
    "ea_version": "unknown",
    "account":    "107072723",
    "server":     "Ava-Demo 1-MT5",
    "last_update":"Never",
    "connected":  False
}
status_lock = threading.Lock()

# ── ENV VARS ──────────────────────────────────────────────────────
GMAIL_USER    = os.environ.get("GMAIL_USER",        "")
GMAIL_PASS    = os.environ.get("GMAIL_PASS",        "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SECRET        = os.environ.get("WEBHOOK_SECRET",    "claude-market-2026")

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None


# ── REQUEST HANDLER ───────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        now = datetime.now(SAST).strftime("%H:%M:%S")
        print(f"[{now} SAST] {format % args}")

    def send_json(self, code, data):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type",  "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    # ── GET ──────────────────────────────────────────────────────
    def do_GET(self):
        global pending_signal

        now_sast = datetime.now(SAST).strftime("%H:%M SAST — %d %b %Y")

        # Health check
        if self.path in ("/", "/health"):
            self.send_json(200, {
                "status":         "Claude-Market Webhook Server v3.2 Running",
                "time_sast":      now_sast,
                "version":        "3.2",
                "account":        "107072723 — Lukas Ferreira",
                "server":         "Ava-Demo 1-MT5",
                "assets":         ["GOLD","SILVER","BTCUSD","ETHUSD","NAS100"],
                "pending_signal": pending_signal is not None,
                "ea_connected":   account_status["connected"],
                "ea_last_seen":   account_status["last_update"]
            })

        # EA polls for signal
        elif self.path == "/signal":
            with signal_lock:
                if pending_signal:
                    self.send_json(200, {
                        "signal": True,
                        "symbol": pending_signal["symbol"],
                        "action": pending_signal["action"],
                        "reason": pending_signal["reason"],
                        "score":  pending_signal["score"],
                        "time":   pending_signal["time"]
                    })
                else:
                    self.send_json(200, {"signal": False})

        # EA clears signal after trade
        elif self.path == "/signal/clear":
            with signal_lock:
                cleared        = pending_signal is not None
                pending_signal = None
            self.send_json(200, {
                "cleared": cleared,
                "message": "Signal cleared — ready for next"
            })
            if cleared:
                print("Signal cleared by EA")

        # Command Centre reads live account data
        elif self.path == "/status":
            with status_lock:
                self.send_json(200, account_status)

        else:
            self.send_json(404, {"error": "Not found"})

    # ── POST ─────────────────────────────────────────────────────
    def do_POST(self):
        global pending_signal, last_signal_time

        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)

        try:
            data = json.loads(raw)
        except Exception as e:
            self.send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        # ── EA posts live status ──────────────────────────────────
        if self.path == "/status":
            if data.get("secret") != SECRET:
                self.send_json(403, {"error": "Invalid secret"})
                return

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
            print(f"Status updated: Bal=${data.get('balance',0):.2f} "
                  f"Eq=${data.get('equity',0):.2f} "
                  f"Pos:{data.get('pos_count',0)}")
            self.send_json(200, {"status": "updated", "time": now_str})
            return

        # ── TradingView webhook signal ────────────────────────────
        if self.path == "/webhook":
            if data.get("secret") != SECRET:
                self.send_json(403, {"error": "Invalid secret"})
                return

            ticker = str(data.get("ticker", "")).upper().strip()
            action = str(data.get("action", "BUY")).upper().strip()
            price  = str(data.get("price",  "0"))

            if not ticker:
                self.send_json(400, {"error": "ticker required"}); return
            if action not in ("BUY","SELL"):
                self.send_json(400, {"error": "action must be BUY or SELL"}); return

            sym = resolve_symbol(ticker)
            print(f"Signal: {ticker}→{sym} {action}")

            # Claude AI validation
            score  = 4
            reason = "BB Breakout signal"
            if client:
                try:
                    score, reason = validate_with_claude(sym, action, price)
                    print(f"Claude: {score}/5 — {reason}")
                except Exception as e:
                    print(f"Claude failed: {e} — default 4")

            if score < 4:
                self.send_json(200, {
                    "status": "rejected", "score": score,
                    "reason": reason, "message": "Score too low"
                }); return

            now_str = datetime.now(SAST).strftime("%Y.%m.%d %H:%M:%S")
            with signal_lock:
                pending_signal   = {
                    "symbol": sym, "action": action,
                    "reason": reason, "score": score, "time": now_str
                }
                last_signal_time = now_str

            print(f"Signal stored: {sym} {action} {score}/5")

            if GMAIL_USER and GMAIL_PASS:
                threading.Thread(
                    target=send_email,
                    args=(sym, action, price, score, reason),
                    daemon=True
                ).start()

            self.send_json(200, {
                "status":  "signal_stored",
                "symbol":  sym, "action": action,
                "score":   f"{score}/5", "reason": reason,
                "message": "EA polls within 10 seconds"
            })
            return

        self.send_json(404, {"error": "Not found"})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ── HELPERS ───────────────────────────────────────────────────────
def resolve_symbol(ticker):
    mapping = {
        "XAUUSD":"GOLD","GOLD":"GOLD","XAGUSD":"SILVER","SILVER":"SILVER",
        "BTCUSD":"BTCUSD","BTC":"BTCUSD","BTCUSDT":"BTCUSD",
        "ETHUSD":"ETHUSD","ETH":"ETHUSD","ETHUSDT":"ETHUSD",
        "NAS100":"NAS100","US100":"NAS100","NASDAQ":"NAS100",
        "US_TECH100":"NAS100","NDX":"NAS100",
        "SPX":"US500","DOW":"US30","OIL":"USOIL","CRUDE":"USOIL"
    }
    return mapping.get(ticker, ticker)


def validate_with_claude(symbol, action, price):
    prompt = f"""Trading signal received:
Symbol: {symbol} | Action: {action} | Price: {price}

Score 1-5:
Box 1 — Major tradeable asset?
Box 2 — {action} correct direction?
Box 3 — Price reasonable?
Box 4 — 2% risk rule OK?
Box 5 — BB Breakout confirmed = AUTO PASS

Reply EXACTLY:
SCORE: X
REASON: one sentence"""

    resp = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=100,
        messages=[{"role":"user","content":prompt}]
    )
    text = resp.content[0].text.strip()
    score=4; reason="BB Breakout validated"
    for line in text.split("\n"):
        line=line.strip()
        if line.startswith("SCORE:"):
            try: score=int(line.replace("SCORE:","").strip().split("/")[0])
            except: pass
        elif line.startswith("REASON:"):
            reason=line.replace("REASON:","").strip()
    return score, reason


def send_email(symbol, action, price, score, reason):
    try:
        now_str = datetime.now(SAST).strftime("%H:%M SAST — %d %b %Y")
        subject = f"CM Signal: {action} {symbol} — Score {score}/5"
        body = (f"Claude-Market Signal\n\nTime: {now_str}\n"
                f"Symbol: {symbol}\nAction: {action}\nPrice: {price}\n"
                f"Score: {score}/5\nReason: {reason}\n\n"
                f"EA placing trade automatically.\nAccount: 107072723")
        msg = MIMEMultipart()
        msg["From"]=GMAIL_USER; msg["To"]=GMAIL_USER; msg["Subject"]=subject
        msg.attach(MIMEText(body,"plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
            s.login(GMAIL_USER,GMAIL_PASS); s.send_message(msg)
        print(f"Email sent: {subject}")
    except Exception as e:
        print(f"Email failed: {e}")


# ── MAIN ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    port   = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Claude-Market Webhook Server v3.2")
    print(f"Port: {port}")
    print(f"Endpoints:")
    print(f"  GET  /            — health")
    print(f"  GET  /signal      — EA polls")
    print(f"  GET  /signal/clear — EA clears")
    print(f"  POST /webhook     — TradingView")
    print(f"  POST /status      — EA reports live data")
    print(f"  GET  /status      — Command Centre reads live data")
    server.serve_forever()
