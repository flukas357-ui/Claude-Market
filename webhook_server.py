"""
CLAUDE-MARKET WEBHOOK SERVER v3.1
====================================
Receives TradingView BB Breakout signals
Validates with Claude AI (5-box system)
Stores signal in memory for EA to poll
MT5 EA polls /signal every 10 seconds

FULL AUTOMATION FLOW:
TradingView BB fires
        ↓
POST → /webhook (this server on Render)
        ↓
Claude AI validates (5-box system)
        ↓
Signal stored in memory here
        ↓
MT5 EA polls GET /signal every 10s
        ↓
EA gets signal → places trade
        ↓
EA calls GET /signal/clear
        ↓
Email notification sent

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

# ── TIMEZONE ──
SAST = timezone(timedelta(hours=2))

# ── SIGNAL STORE (in-memory) ──
pending_signal   = None
signal_lock      = threading.Lock()
last_signal_time = None

# ── ENV VARS ──
GMAIL_USER    = os.environ.get("GMAIL_USER",        "")
GMAIL_PASS    = os.environ.get("GMAIL_PASS",        "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SECRET        = os.environ.get("WEBHOOK_SECRET",    "claude-market-2026")

# ── ANTHROPIC CLIENT ──
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None


# ──────────────────────────────────────────────
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

    # ── GET ──────────────────────────────────
    def do_GET(self):
        global pending_signal  # declared at top of function — fixes SyntaxError

        now_sast = datetime.now(SAST).strftime("%H:%M SAST — %d %b %Y")

        # Health check
        if self.path in ("/", "/health"):
            self.send_json(200, {
                "status":         "Claude-Market Webhook Server v3.1 Running",
                "time_sast":      now_sast,
                "version":        "3.1",
                "account":        "107072723 — Lukas Ferreira",
                "server":         "Ava-Demo 1-MT5",
                "assets":         ["GOLD","SILVER","BTCUSD","ETHUSD","NAS100"],
                "pending_signal": pending_signal is not None
            })

        # EA polls this every 10 seconds
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

        # EA calls this after placing trade
        elif self.path == "/signal/clear":
            with signal_lock:
                cleared        = pending_signal is not None
                pending_signal = None
            self.send_json(200, {
                "cleared": cleared,
                "message": "Signal cleared — ready for next signal"
            })
            if cleared:
                print("Signal cleared by EA — trade was placed")

        else:
            self.send_json(404, {"error": "Not found"})

    # ── POST ─────────────────────────────────
    def do_POST(self):
        global pending_signal, last_signal_time  # declared at top of function

        if self.path != "/webhook":
            self.send_json(404, {"error": "Not found"})
            return

        # Read body
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)

        try:
            data = json.loads(raw)
        except Exception as e:
            self.send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        print(f"Received: {data}")

        # Validate secret
        if data.get("secret") != SECRET:
            self.send_json(403, {"error": "Invalid secret"})
            return

        ticker = str(data.get("ticker", "")).upper().strip()
        action = str(data.get("action", "BUY")).upper().strip()
        price  = str(data.get("price",  "0"))

        if not ticker:
            self.send_json(400, {"error": "ticker is required"})
            return

        if action not in ("BUY", "SELL"):
            self.send_json(400, {"error": "action must be BUY or SELL"})
            return

        # Map TradingView ticker to broker symbol
        sym = resolve_symbol(ticker)
        print(f"Symbol: {ticker} → {sym}")

        # Claude AI validation
        score  = 4
        reason = "BB Breakout signal"

        if client:
            try:
                score, reason = validate_with_claude(sym, action, price)
                print(f"Claude score: {score}/5 — {reason}")
            except Exception as e:
                print(f"Claude validation failed: {e} — default score 4")
        else:
            print("No ANTHROPIC_API_KEY — default score 4")

        # Score filter
        if score < 4:
            print(f"Score {score}/5 too low — rejected")
            self.send_json(200, {
                "status":  "rejected",
                "score":   score,
                "reason":  reason,
                "message": "Score below threshold — no trade"
            })
            return

        # Store signal for EA to poll
        now_str = datetime.now(SAST).strftime("%Y.%m.%d %H:%M:%S")
        with signal_lock:
            pending_signal   = {
                "symbol": sym,
                "action": action,
                "reason": reason,
                "score":  score,
                "time":   now_str
            }
            last_signal_time = now_str

        print(f"Signal stored: {sym}|{action}|{reason}|{score} — EA polls within 10s")

        # Email notification (non-blocking)
        if GMAIL_USER and GMAIL_PASS:
            threading.Thread(
                target=send_email,
                args=(sym, action, price, score, reason),
                daemon=True
            ).start()

        self.send_json(200, {
            "status":  "signal_stored",
            "symbol":  sym,
            "action":  action,
            "score":   f"{score}/5",
            "reason":  reason,
            "message": "EA will poll and trade within 10 seconds"
        })

    # ── OPTIONS (CORS preflight) ──────────────
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ──────────────────────────────────────────────
def resolve_symbol(ticker):
    mapping = {
        "XAUUSD":     "GOLD",
        "GOLD":       "GOLD",
        "XAGUSD":     "SILVER",
        "SILVER":     "SILVER",
        "BTCUSD":     "BTCUSD",
        "BTC":        "BTCUSD",
        "BTCUSDT":    "BTCUSD",
        "ETHUSD":     "ETHUSD",
        "ETH":        "ETHUSD",
        "ETHUSDT":    "ETHUSD",
        "NAS100":     "NAS100",
        "US100":      "NAS100",
        "NASDAQ":     "NAS100",
        "US_TECH100": "NAS100",
        "NDX":        "NAS100",
    }
    return mapping.get(ticker, ticker)


# ──────────────────────────────────────────────
def validate_with_claude(symbol, action, price):
    prompt = f"""You are a trading risk manager for Claude-Market.
A TradingView BB Breakout signal has fired:
Symbol: {symbol}
Action: {action}
Price:  {price}

Score this signal 1-5:
Box 1 — Is this a major asset? (GOLD/SILVER/BTC/ETH/NAS100)
Box 2 — Is {action} the correct direction?
Box 3 — Is the price reasonable?
Box 4 — 2% risk rule respected
Box 5 — BB Breakout confirmed = AUTO PASS

Reply in EXACTLY this format:
SCORE: X
REASON: one sentence"""

    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}]
    )
    text = resp.content[0].text.strip()

    score  = 4
    reason = "BB Breakout validated"

    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(line.replace("SCORE:", "").strip().split("/")[0])
            except:
                pass
        elif line.startswith("REASON:"):
            reason = line.replace("REASON:", "").strip()

    return score, reason


# ──────────────────────────────────────────────
def send_email(symbol, action, price, score, reason):
    try:
        now_str = datetime.now(SAST).strftime("%H:%M SAST — %d %b %Y")
        subject = f"CM Signal: {action} {symbol} — Score {score}/5"
        body    = (
            f"Claude-Market Signal Alert\n\n"
            f"Time:   {now_str}\n"
            f"Symbol: {symbol}\n"
            f"Action: {action}\n"
            f"Price:  {price}\n"
            f"Score:  {score}/5\n"
            f"Reason: {reason}\n\n"
            f"EA is placing the trade automatically.\n"
            f"Check MT5 Trade tab for confirmation.\n\n"
            f"Account: 107072723 — Lukas Ferreira\n"
            f"Ava-Demo 1-MT5"
        )
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_USER
        msg["To"]      = GMAIL_USER
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.send_message(msg)

        print(f"Email sent: {subject}")
    except Exception as e:
        print(f"Email failed: {e}")


# ──────────────────────────────────────────────
if __name__ == "__main__":
    port   = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Claude-Market Webhook Server v3.1 started on port {port}")
    print(f"Endpoints:")
    print(f"  GET  /            — health check")
    print(f"  GET  /signal      — EA polls every 10s")
    print(f"  GET  /signal/clear — EA calls after trade")
    print(f"  POST /webhook     — TradingView sends here")
    server.serve_forever()
