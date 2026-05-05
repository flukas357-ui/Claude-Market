"""
CLAUDE-MARKET WEBHOOK SERVER v2.0
====================================
Receives TradingView BB Breakout signals
Validates with Claude AI (5-box system)
Writes signal file for MT5 Expert Advisor
Sends email + push notification on signal

FULL AUTOMATION FLOW:
TradingView BB Signal fires
        ↓
Webhook server receives JSON
        ↓
Claude AI validates (5-box system)
        ↓
Writes signal file → MT5 reads it
        ↓
ClaudMarket_EA places trade automatically
        ↓
Stop-loss + Take-profit set
        ↓
Email notification sent

Deploy on Render.com — free forever
Account: 107072723 — Lukas Ferreira
Server:  Ava-Demo 1-MT5
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
ACCOUNT_BALANCE    = 10000.00
MAX_RISK_PER_TRADE = ACCOUNT_BALANCE * 0.02  # $200
DAILY_LIMIT        = ACCOUNT_BALANCE * 0.05  # $500

# ── POSITION SIZES (2% rule on $10,000) ──
POSITION_SIZES = {
    "XAUUSD":    "0.057 lots",
    "XAGUSD":    "0.050 lots",
    "BTCUSD":    "0.002 BTC",
    "ETHUSD":    "0.117 ETH",
    "NAS100":    "0.055 lots",
    "US_TECH100":"0.055 lots",
}

# ── ASSET NAMES ──
ASSET_NAMES = {
    "XAUUSD":    "Gold",
    "XAGUSD":    "Silver",
    "BTCUSD":    "Bitcoin",
    "ETHUSD":    "Ethereum",
    "NAS100":    "NASDAQ",
    "US_TECH100":"NASDAQ",
}

# ── MT5 SIGNAL FILE PATH ──
# This file is written by webhook and read by MT5 EA
MT5_SIGNAL_FILE = "claude_market_signal.txt"

# ── ANTHROPIC CLIENT ──
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def get_fear_greed():
    """Fetch live Fear & Greed index"""
    try:
        r = requests.get(
            "https://api.alternative.me/fng/?limit=1&format=json",
            timeout=10
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
    size       = POSITION_SIZES.get(ticker, "unknown")
    now_sast   = datetime.now(SAST).strftime("%A, %d %B %Y %H:%M SAST")

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
Box 1 — News Risk: Is this a reasonable time to trade?
Box 2 — Sentiment: Does {signal_type} align with current market mood?
Box 3 — Fear & Greed: Is {fng['value']}/100 between 30-75?
Box 4 — Account Health: Is $10,000 balance in good standing?
Box 5 — BB Signal: Signal came from BB Breakout v7.0 = AUTO PASS

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


def extract_score(validation_text):
    """Extract the numeric score from validation text"""
    try:
        for line in validation_text.split('\n'):
            if line.startswith('SCORE:'):
                score_str = line.replace('SCORE:', '').strip()
                return int(score_str.split('/')[0])
    except:
        pass
    return 4  # Default to 4 if parsing fails


def write_mt5_signal_file(ticker, action, price, score):
    """
    Write signal file that MT5 Expert Advisor reads
    Format: SYMBOL|ACTION|PRICE|SCORE
    Example: XAUUSD|BUY|3250.50|5
    """
    try:
        signal_content = f"{ticker}|{action}|{price}|{score}"

        # Write to local file (Render server)
        with open(MT5_SIGNAL_FILE, 'w') as f:
            f.write(signal_content)

        print(f"✅ MT5 signal file written: {signal_content}")
        print(f"   File: {MT5_SIGNAL_FILE}")
        print(f"   NOTE: MT5 must read this via shared storage")
        print(f"   For local testing: copy file to MT5 Common folder")

        return True
    except Exception as e:
        print(f"❌ Failed to write signal file: {e}")
        return False


def send_signal_email(ticker, signal_type, price, validation, fng, score):
    """Send instant email alert when BB signal fires"""

    asset_name = ASSET_NAMES.get(ticker, ticker)
    size       = POSITION_SIZES.get(ticker, "unknown")
    now_sast   = datetime.now(SAST).strftime("%H:%M SAST — %d %B %Y")

    # Determine colors based on verdict
    if "EXECUTE" in validation:
        verdict_color = "#2ECC71"
        verdict_text  = "✅ EXECUTE"
        verdict_bg    = "#E8F5E9"
    elif "CAUTION" in validation:
        verdict_color = "#F39C12"
        verdict_text  = "⚠️ CAUTION"
        verdict_bg    = "#FFF8E1"
    else:
        verdict_color = "#E74C3C"
        verdict_text  = "❌ REJECT"
        verdict_bg    = "#FFEBEE"

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;background:#f0f0f0">

<div style="background:#1B4F8A;border-radius:12px 12px 0 0;padding:18px 24px">
  <h1 style="color:#fff;margin:0;font-size:18px">⚡ BB Signal Alert — Claude-Market</h1>
  <p style="color:rgba(255,255,255,0.8);margin:4px 0 0;font-size:12px">{now_sast}</p>
</div>

<div style="background:#fff;border-radius:0 0 12px 12px;padding:20px 24px">

  <!-- Signal Banner -->
  <div style="background:{verdict_color};border-radius:10px;padding:16px;text-align:center;margin-bottom:20px">
    <div style="font-size:24px;font-weight:bold;color:#fff">{asset_name} ({ticker})</div>
    <div style="font-size:18px;color:rgba(255,255,255,0.9);margin-top:4px">{signal_type} @ {price}</div>
    <div style="font-size:14px;color:rgba(255,255,255,0.9);margin-top:4px">{verdict_text} — Score: {score}/5</div>
    <div style="font-size:13px;color:rgba(255,255,255,0.8);margin-top:4px">Size: {size} | Risk: ${MAX_RISK_PER_TRADE:.0f}</div>
  </div>

  <!-- Stats Row -->
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
    <div style="flex:1;background:{verdict_bg};border-radius:8px;padding:12px;text-align:center">
      <div style="font-size:11px;color:#9E9E9E">AI Score</div>
      <div style="font-size:24px;font-weight:bold;color:{verdict_color}">{score}/5</div>
      <div style="font-size:11px;color:{verdict_color}">boxes</div>
    </div>
  </div>

  <!-- MT5 Status -->
  <div style="background:#E3F2FD;border-radius:8px;padding:12px;margin-bottom:16px">
    <div style="font-weight:bold;color:#1565C0;margin-bottom:6px;font-size:13px">🤖 MT5 Expert Advisor Status</div>
    <div style="font-family:monospace;font-size:12px;color:#333">
      ClaudMarket_EA → Signal received<br>
      Account: 107072723 — Lukas Ferreira<br>
      {'✅ Trade being placed automatically...' if 'EXECUTE' in validation else '⚠️ Trade not placed — score too low'}
    </div>
  </div>

  <!-- AI Validation -->
  <div style="background:#F5F5F5;border-radius:8px;padding:14px;margin-bottom:16px">
    <div style="font-weight:bold;color:#1B4F8A;margin-bottom:8px;font-size:13px">🧠 Claude AI 5-Box Validation</div>
    <pre style="font-size:11px;color:#333;line-height:1.8;white-space:pre-wrap;margin:0">{validation}</pre>
  </div>

  <!-- Dashboard Link -->
  <div style="text-align:center;margin-bottom:16px">
    <a href="https://claude-market-za.netlify.app"
       style="display:inline-block;background:#B8860B;color:#fff;padding:12px 28px;
              border-radius:8px;text-decoration:none;font-weight:bold;font-size:13px">
      📱 Open Command Center →
    </a>
  </div>

  <div style="font-size:11px;color:#9E9E9E;text-align:center">
    Claude-Market v1.0 — BB Breakout v7.0 — Pretoria 🇿🇦<br>
    For educational purposes only. Not financial advice.
  </div>
</div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚡ BB Signal: {signal_type} {asset_name} @ {price} — Score {score}/5 — {now_sast}"
    msg["From"]    = os.environ["EMAIL_FROM"]
    msg["To"]      = os.environ["EMAIL_TO"]
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["EMAIL_FROM"], os.environ["EMAIL_PASSWORD"])
        s.sendmail(os.environ["EMAIL_FROM"], os.environ["EMAIL_TO"], msg.as_string())

    print(f"📧 Signal email sent for {ticker}")


# ── WEBHOOK HANDLER ──
class WebhookHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        """Health check endpoint"""
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        status = {
            "status":    "Claude-Market Webhook Server v2.0 Running",
            "time_sast": datetime.now(SAST).strftime("%H:%M SAST — %d %B %Y"),
            "version":   "2.0",
            "account":   "107072723 — Lukas Ferreira",
            "server":    "Ava-Demo 1-MT5",
            "assets":    list(POSITION_SIZES.keys()),
        }
        self.wfile.write(json.dumps(status, indent=2).encode())

    def do_POST(self):
        """Receive TradingView webhook signal"""
        try:
            # Read incoming data
            content_length = int(self.headers.get("Content-Length", 0))
            body           = self.rfile.read(content_length)
            data           = json.loads(body.decode("utf-8"))

            print(f"\n{'='*50}")
            print(f"📡 SIGNAL RECEIVED: {data}")
            print(f"{'='*50}")

            # Extract signal fields
            ticker = data.get("ticker", "UNKNOWN").upper()
            action = data.get("action", "BUY").upper()
            price  = data.get("price", "N/A")
            secret = data.get("secret", "")

            # Security check
            expected_secret = os.environ.get("WEBHOOK_SECRET", "claude-market-2026")
            if secret != expected_secret:
                print("❌ Invalid secret — signal rejected")
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized"}')
                return

            # Validate ticker
            if ticker not in POSITION_SIZES:
                print(f"⚠️  Unknown ticker: {ticker}")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "Unknown ticker"}')
                return

            # Get Fear & Greed
            print("📊 Fetching Fear & Greed...")
            fng = get_fear_greed()
            print(f"   Fear & Greed: {fng['value']} — {fng['label']}")

            # Validate with Claude AI
            print(f"🤖 Validating {ticker} {action} signal with Claude AI...")
            validation = validate_signal_with_claude(ticker, action, price, fng)
            score      = extract_score(validation)
            print(f"   Score: {score}/5")
            print(f"   Validation:\n{validation}")

            # Write MT5 signal file if score is good enough
            if score >= 4 and "REJECT" not in validation:
                print(f"✅ Score {score}/5 — writing MT5 signal file...")
                write_mt5_signal_file(ticker, action, price, score)
            else:
                print(f"⚠️  Score {score}/5 — signal not strong enough for MT5")

            # Always send email notification
            print("📧 Sending email notification...")
            send_signal_email(ticker, action, price, validation, fng, score)

            # Respond to TradingView
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            response = {
                "status":     "Signal processed",
                "ticker":     ticker,
                "action":     action,
                "price":      price,
                "score":      f"{score}/5",
                "mt5_signal": score >= 4 and "REJECT" not in validation,
            }
            self.wfile.write(json.dumps(response).encode())
            print(f"✅ Signal processing complete for {ticker}")

        except Exception as e:
            print(f"❌ Error processing signal: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        now = datetime.now(SAST).strftime("%H:%M:%S SAST")
        print(f"[{now}] {format % args}")


# ── START SERVER ──
if __name__ == "__main__":
    port   = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    now    = datetime.now(SAST).strftime("%H:%M SAST — %d %B %Y")
    print("╔══════════════════════════════════════════╗")
    print("║   Claude-Market Webhook Server v2.0     ║")
    print(f"║   Started: {now}    ║")
    print("║   Account: 107072723 — Lukas Ferreira   ║")
    print("║   Strategy: BB Breakout v7.0             ║")
    print("║   Pretoria, South Africa 🇿🇦              ║")
    print("╚══════════════════════════════════════════╝")
    print(f"🌐 Listening on port {port}")
    print("📡 Waiting for TradingView signals...")
    print("Assets monitored:", list(POSITION_SIZES.keys()))
    server.serve_forever()
