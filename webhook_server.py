"""
CLAUDE-MARKET WEBHOOK SERVER
==============================
Receives TradingView BB Breakout signals
Validates with Claude AI (5-box system)
Notifies via email when signal fires
Ready for AvaTrade API execution (Phase 4)

Deploy on Render.com — free forever
"""

import os
import json
import smtplib
import requests
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import HTTPServer, BaseHTTPRequestHandler
import anthropic

# ── TIMEZONE ──
SAST = timezone(timedelta(hours=2))

# ── ACCOUNT SETTINGS ──
ACCOUNT_BALANCE = 10000.00
MAX_RISK_PER_TRADE = ACCOUNT_BALANCE * 0.02  # $200
DAILY_LIMIT = ACCOUNT_BALANCE * 0.05          # $500

# ── POSITION SIZES (2% rule) ──
POSITION_SIZES = {
    "XAUUSD": "0.057 lots",
    "XAGUSD": "444 oz",
    "BTCUSD": "0.0016 BTC",
    "ETHUSD": "0.117 ETH",
    "NAS100": "0.055 lots",
}

# ── ASSET NAMES ──
ASSET_NAMES = {
    "XAUUSD": "Gold",
    "XAGUSD": "Silver",
    "BTCUSD": "Bitcoin",
    "ETHUSD": "Ethereum",
    "NAS100": "NASDAQ",
}

# ── CLIENTS ──
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def get_fear_greed():
    """Fetch live Fear & Greed index"""
    try:
        r = requests.get(
            "https://api.alternative.me/fng/?limit=1&format=json", timeout=10
        )
        data = r.json()["data"][0]
        return {
            "value": int(data["value"]),
            "label": data["value_classification"],
        }
    except:
        return {"value": 50, "label": "Neutral"}


def validate_signal_with_claude(ticker, signal_type, price, fng):
    """Use Claude AI to validate the TradingView signal against 5-box system"""

    asset_name = ASSET_NAMES.get(ticker, ticker)
    size = POSITION_SIZES.get(ticker, "unknown")
    now_sast = datetime.now(SAST).strftime("%A, %d %B %Y %H:%M SAST")

    prompt = f"""You are the Claude-Market Signal Validator.
A TradingView BB Breakout v7.0 signal just fired!

SIGNAL DETAILS:
- Asset: {asset_name} ({ticker})
- Signal: {signal_type}
- Price: {price}
- Time: {now_sast}
- Position Size: {size}
- Max Risk: ${MAX_RISK_PER_TRADE:.0f} (2% rule)

LIVE DATA:
- Fear & Greed: {fng['value']}/100 — {fng['label']}
- Account Balance: ${ACCOUNT_BALANCE:,.0f}
- Daily Limit: ${DAILY_LIMIT:.0f}

Validate this signal against our 5-box system:
Box 1 — News Risk: Is this a reasonable time to trade? (not extreme geopolitical crisis)
Box 2 — Sentiment: Does {signal_type} align with current market mood?
Box 3 — Fear & Greed: Is {fng['value']}/100 between 30-75?
Box 4 — Account Health: Is $10,000 balance in good standing?
Box 5 — BB Signal: Signal came directly from BB Breakout v7.0 = AUTO PASS

Return ONLY this exact format:
VERDICT: [EXECUTE|CAUTION|REJECT]
SCORE: [X/5]
BOX1_NEWS: [PASS|FAIL] — [one reason]
BOX2_SENTIMENT: [PASS|FAIL] — [one reason]
BOX3_FNG: [PASS|FAIL] — {fng['value']}/100
BOX4_ACCOUNT: [PASS|FAIL] — [one reason]
BOX5_BB: PASS — Signal confirmed by TradingView BB Breakout v7.0
CONFIDENCE: [HIGH|MEDIUM|LOW]
ACTION: [exact one sentence instruction]
NOTE: [one sentence overall assessment]"""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def send_signal_email(ticker, signal_type, price, validation, fng):
    """Send instant email alert when BB signal fires"""

    asset_name = ASSET_NAMES.get(ticker, ticker)
    size = POSITION_SIZES.get(ticker, "unknown")
    now_sast = datetime.now(SAST).strftime("%H:%M SAST — %d %B %Y")

    # Determine verdict color
    verdict_color = "#2ECC71"  # green
    if "CAUTION" in validation:
        verdict_color = "#F39C12"  # orange
    elif "REJECT" in validation:
        verdict_color = "#E74C3C"  # red

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;background:#f0f0f0">

<div style="background:#1B4F8A;border-radius:12px 12px 0 0;padding:18px 24px">
  <h1 style="color:#fff;margin:0;font-size:18px">⚡ BB Signal Alert — Claude-Market</h1>
  <p style="color:rgba(255,255,255,0.8);margin:4px 0 0;font-size:12px">{now_sast}</p>
</div>

<div style="background:#fff;border-radius:0 0 12px 12px;padding:20px 24px">

  <!-- Signal Header -->
  <div style="background:{verdict_color};border-radius:10px;padding:16px;text-align:center;margin-bottom:20px">
    <div style="font-size:28px;font-weight:bold;color:#fff">{asset_name} ({ticker})</div>
    <div style="font-size:20px;color:rgba(255,255,255,0.9);margin-top:4px">{signal_type} @ {price}</div>
    <div style="font-size:14px;color:rgba(255,255,255,0.8);margin-top:4px">Size: {size} | Risk: ${MAX_RISK_PER_TRADE:.0f}</div>
  </div>

  <!-- Fear & Greed -->
  <div style="display:flex;gap:10px;margin-bottom:16px">
    <div style="flex:1;background:#F5F5F5;border-radius:8px;padding:12px;text-align:center">
      <div style="font-size:11px;color:#9E9E9E">Fear & Greed</div>
      <div style="font-size:24px;font-weight:bold;color:#E65100">{fng['value']}</div>
      <div style="font-size:11px;color:#E65100">{fng['label']}</div>
    </div>
    <div style="flex:1;background:#F5F5F5;border-radius:8px;padding:12px;text-align:center">
      <div style="font-size:11px;color:#9E9E9E">Account</div>
      <div style="font-size:24px;font-weight:bold;color:#212121">${ACCOUNT_BALANCE:,.0f}</div>
      <div style="font-size:11px;color:#9E9E9E">Demo balance</div>
    </div>
  </div>

  <!-- AI Validation -->
  <div style="background:#F5F5F5;border-radius:8px;padding:14px;margin-bottom:16px">
    <div style="font-weight:bold;color:#1B4F8A;margin-bottom:10px;font-size:13px">🤖 Claude AI Validation</div>
    <pre style="font-size:12px;color:#333;line-height:1.7;white-space:pre-wrap;margin:0">{validation}</pre>
  </div>

  <!-- Action Button -->
  <div style="background:#FFFDE7;border-radius:8px;padding:14px;text-align:center;margin-bottom:16px">
    <div style="font-weight:bold;color:#B8860B;font-size:14px">📱 Open Claude-Market Dashboard</div>
    <a href="https://claude-market-za.netlify.app" 
       style="display:inline-block;margin-top:10px;background:#B8860B;color:#fff;padding:10px 24px;border-radius:8px;text-decoration:none;font-weight:bold;font-size:13px">
      REVIEW & EXECUTE →
    </a>
  </div>

  <div style="font-size:11px;color:#9E9E9E;text-align:center">
    Claude-Market v1.0 — BB Breakout v7.0 Signal Alert<br>
    For educational purposes only. Not financial advice.
  </div>
</div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚡ BB Signal: {signal_type} {asset_name} @ {price} — {now_sast}"
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["EMAIL_FROM"], os.environ["EMAIL_PASSWORD"])
        s.sendmail(
            os.environ["EMAIL_FROM"], os.environ["EMAIL_TO"], msg.as_string()
        )
    print(f"✅ Signal email sent for {ticker}")


# ── WEBHOOK HANDLER ──
class WebhookHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        """Health check endpoint"""
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        status = {
            "status": "Claude-Market Webhook Server Running",
            "time_sast": datetime.now(SAST).strftime("%H:%M SAST — %d %B %Y"),
            "version": "1.0",
        }
        self.wfile.write(json.dumps(status).encode())

    def do_POST(self):
        """Receive TradingView webhook signal"""
        try:
            # Read incoming data
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode("utf-8"))

            print(f"📡 Signal received: {data}")

            # Extract signal data
            # TradingView sends: {"ticker": "XAUUSD", "action": "BUY", "price": "3250.50"}
            ticker = data.get("ticker", "UNKNOWN").upper()
            action = data.get("action", "BUY").upper()
            price = data.get("price", "N/A")
            secret = data.get("secret", "")

            # Security check — verify secret token
            expected_secret = os.environ.get("WEBHOOK_SECRET", "claude-market-2026")
            if secret != expected_secret:
                print("❌ Invalid secret — signal rejected")
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized"}')
                return

            # Validate ticker
            if ticker not in POSITION_SIZES:
                print(f"⚠️ Unknown ticker: {ticker}")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "Unknown ticker"}')
                return

            # Get live Fear & Greed
            fng = get_fear_greed()
            print(f"Fear & Greed: {fng['value']} — {fng['label']}")

            # Validate with Claude AI
            print(f"🤖 Validating {ticker} {action} signal with Claude...")
            validation = validate_signal_with_claude(ticker, action, price, fng)
            print(f"Validation: {validation[:100]}...")

            # Send email alert
            send_signal_email(ticker, action, price, validation, fng)

            # Respond to TradingView
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            response = {
                "status": "Signal processed",
                "ticker": ticker,
                "action": action,
                "price": price,
            }
            self.wfile.write(json.dumps(response).encode())

        except Exception as e:
            print(f"❌ Error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        """Clean logging"""
        now = datetime.now(SAST).strftime("%H:%M:%S SAST")
        print(f"[{now}] {format % args}")


# ── START SERVER ──
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    now = datetime.now(SAST).strftime("%H:%M SAST — %d %B %Y")
    print(f"🚀 Claude-Market Webhook Server started")
    print(f"⏰ {now}")
    print(f"🌐 Listening on port {port}")
    print(f"📡 Waiting for TradingView signals...")
    server.serve_forever()
