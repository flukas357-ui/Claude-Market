"""
CLAUDE-MARKET DAILY INTELLIGENCE SYSTEM v2.0
==============================================
5 AI Agents — Global Multi-Asset Scanner
Runs: 07:00 SAST daily via GitHub Actions
Account: 107072723 — Lukas Ferreira — Pretoria 🇿🇦

AGENT OVERVIEW:
  Agent 1 — Global News Sweep (multinational)
  Agent 2 — Global Opportunity Scanner (any asset worldwide)
  Agent 3 — Performance Review (open positions + P&L)
  Agent 4 — Risk Manager (2% rule + scale-in check)
  Agent 5 — Sentiment Scorer (Fear & Greed + global mood)
  Master  — Compiler + Email Report
"""

import os
import json
import time
import smtplib
import anthropic
import requests

from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── TIMEZONE ──────────────────────────────────────────────────────
SAST = timezone(timedelta(hours=2))

# ── CONFIG ────────────────────────────────────────────────────────
GMAIL_USER    = os.environ.get("GMAIL_USER",        "")
GMAIL_PASS    = os.environ.get("GMAIL_PASS",        "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── PRIMARY ASSETS (always monitored) ────────────────────────────
PRIMARY_ASSETS = ["GOLD", "SILVER", "BTCUSD", "ETHUSD", "NAS100"]

# ── GLOBAL SCAN LIST (Agent 2 watches these too) ─────────────────
# Any strong signal here gets flagged for TradingView setup
GLOBAL_ASSETS = [
    # Forex majors
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF",
    # Forex minors
    "GBPJPY", "EURJPY", "EURGBP",
    # Indices
    "US30", "US500", "GER40", "UK100", "JPN225",
    # Commodities
    "USOIL", "UKOIL", "NATGAS", "COPPER",
    # Crypto
    "SOLUSD", "BNBUSD", "XRPUSD", "ADAUSD",
    # SA specific
    "USDZAR", "XAUUSD"
]

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# ── HELPERS ───────────────────────────────────────────────────────
def now_sast():
    return datetime.now(SAST).strftime("%H:%M SAST — %d %b %Y")

def sleep_between(label):
    print(f"\n[{now_sast()}] ⏳ Waiting 65s before {label}...")
    time.sleep(65)

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        return {"value": d["value"], "label": d["value_classification"]}
    except:
        return {"value": "50", "label": "Neutral"}

def call_claude(prompt, max_tokens=800):
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip()


# ── AGENT 1 — GLOBAL NEWS SWEEP ──────────────────────────────────
def agent1_global_news():
    print(f"\n[{now_sast()}] 🌍 Agent 1: Global News Sweep starting...")

    prompt = """You are Agent 1 — Global Market Intelligence for Claude-Market.

Scan and analyse the current global market environment across ALL regions:

REGIONS TO COVER:
- United States (Fed policy, inflation, jobs, earnings)
- Europe (ECB, UK economy, energy)  
- Asia (BOJ, China economy, tech sector)
- Africa/SA (USDZAR, commodity prices, Eskom)
- Middle East (oil supply, geopolitics)
- Crypto (regulatory news, Bitcoin halving effects)

ASSETS TO ASSESS:
Primary: GOLD, SILVER, BTCUSD, ETHUSD, NAS100
Global watch: EURUSD, GBPUSD, USDJPY, USOIL, US500, US30, USDZAR

For each region and asset provide:
- Key headline driving markets today
- Direction bias (BULLISH/BEARISH/NEUTRAL)
- Risk level (LOW/MEDIUM/HIGH)

Format your response EXACTLY like this:
GLOBAL_RISK: [LOW/MEDIUM/HIGH]
DOMINANT_THEME: [one sentence — what is THE story driving markets today]

PRIMARY ASSETS:
GOLD: [BULLISH/BEARISH/NEUTRAL] — [reason]
SILVER: [BULLISH/BEARISH/NEUTRAL] — [reason]  
BTCUSD: [BULLISH/BEARISH/NEUTRAL] — [reason]
ETHUSD: [BULLISH/BEARISH/NEUTRAL] — [reason]
NAS100: [BULLISH/BEARISH/NEUTRAL] — [reason]

GLOBAL WATCH:
EURUSD: [BULLISH/BEARISH/NEUTRAL] — [reason]
GBPUSD: [BULLISH/BEARISH/NEUTRAL] — [reason]
USDJPY: [BULLISH/BEARISH/NEUTRAL] — [reason]
USOIL: [BULLISH/BEARISH/NEUTRAL] — [reason]
USDZAR: [BULLISH/BEARISH/NEUTRAL] — [reason]

HEADLINE: [the single most important market news right now]
RECOMMENDATION: [what this means for trading today]"""

    result = call_claude(prompt, 1000)
    print(f"Agent 1 complete ✓")
    return result


# ── AGENT 2 — GLOBAL OPPORTUNITY SCANNER ─────────────────────────
def agent2_global_scanner(agent1_report):
    print(f"\n[{now_sast()}] 🔭 Agent 2: Global Opportunity Scanner starting...")

    prompt = f"""You are Agent 2 — Global Opportunity Scanner for Claude-Market.

The system can now trade ANY asset — not just the 5 primary ones.
Your job is to find the BEST trading opportunities globally right now.

CONTEXT FROM AGENT 1:
{agent1_report}

ASSETS TO SCAN FOR OPPORTUNITIES:
Forex: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, GBPJPY, EURJPY, USDZAR
Indices: US30, US500, GER40, UK100, JPN225
Commodities: USOIL, UKOIL, COPPER, NATGAS
Crypto: SOLUSD, BNBUSD, XRPUSD
Primary: GOLD, SILVER, BTCUSD, ETHUSD, NAS100

For each asset you identify as a HIGH opportunity, provide:
- Asset name (exact broker symbol)
- Direction: BUY or SELL
- Strength: STRONG/MODERATE
- Reason: why this setup is compelling right now
- Risk: LOW/MEDIUM/HIGH

Format EXACTLY like this:
OPPORTUNITIES_FOUND: [number]

TOP_OPPORTUNITY_1:
ASSET: [symbol]
DIRECTION: [BUY/SELL]
STRENGTH: [STRONG/MODERATE]
REASON: [one clear sentence]
RISK: [LOW/MEDIUM/HIGH]

TOP_OPPORTUNITY_2:
[same format]

TOP_OPPORTUNITY_3:
[same format]

GLOBAL_SUMMARY: [2 sentences — what are markets doing overall today]
ACTION: [specific recommendation for Claude-Market system today]"""

    result = call_claude(prompt, 1000)
    print(f"Agent 2 complete ✓")
    return result


# ── AGENT 3 — PERFORMANCE REVIEW ─────────────────────────────────
def agent3_performance():
    print(f"\n[{now_sast()}] 📊 Agent 3: Performance Review starting...")

    prompt = """You are Agent 3 — Performance Review for Claude-Market.

Account details:
- Account: 107072723 — Lukas Ferreira
- Broker: Ava-Demo 1-MT5
- Starting balance: $10,000
- Current balance: approximately $9,589 (as of last check)
- Assets traded: GOLD, SILVER, BTCUSD, ETHUSD, NAS100 + any global asset
- Risk per trade: 2% of balance (dynamic)
- Scale-in: 1% additional (when score 5/5 + profit > 1% balance)
- Max total risk: 3% of balance
- TP Ratio: 1.5×
- SL: Per asset (GOLD=100pip, SILVER=120pip, NAS=150pip, BTC/ETH=200pip)
- Unknown assets: ATR×1.5 calculated automatically

30-Day Challenge: 05 May 2026 → 04 June 2026 (Day 2 today)

Provide a structured performance review:
ACCOUNT_HEALTH: [EXCELLENT/GOOD/CAUTION/DANGER]
BALANCE_STATUS: [$X — X% from starting $10,000]
DAILY_STATUS: [what happened yesterday]
RISK_CAPACITY: [how many more trades can we take today]
DRAWDOWN_STATUS: [are we within acceptable drawdown?]
CHALLENGE_STATUS: [on track / behind / ahead for 30-day challenge]
RECOMMENDATION: [specific action for today]"""

    result = call_claude(prompt, 600)
    print(f"Agent 3 complete ✓")
    return result


# ── AGENT 4 — RISK MANAGER ────────────────────────────────────────
def agent4_risk(agent2_report):
    print(f"\n[{now_sast()}] 🛡️ Agent 4: Risk Manager starting...")

    prompt = f"""You are Agent 4 — Risk Manager for Claude-Market.

Review the opportunities identified by Agent 2 and apply strict risk rules:

AGENT 2 OPPORTUNITIES:
{agent2_report}

RISK RULES TO APPLY:
1. Max 2% risk per trade (dynamic lot sizing — already in EA)
2. Max 3% total open risk across all positions
3. Max 2 trades per symbol (hard limit)
4. Scale-in only at score 5/5 + profit > 1% balance
5. No trading 30 mins before major news events
6. No crypto trades on weekends (high spread)
7. Avoid correlated trades (e.g. GOLD+SILVER both long = double exposure)
8. Account must have >50% free margin at all times

For each opportunity from Agent 2, give APPROVED or REJECTED with reason.
Then provide overall risk assessment for today.

Format EXACTLY:
OPPORTUNITY_1: [APPROVED/REJECTED] — [reason]
OPPORTUNITY_2: [APPROVED/REJECTED] — [reason]
OPPORTUNITY_3: [APPROVED/REJECTED] — [reason]

TOTAL_RISK_TODAY: [LOW/MEDIUM/HIGH]
CORRELATION_WARNING: [any assets too correlated to trade together]
MAX_TRADES_TODAY: [how many new trades are safe today]
RISK_NOTE: [most important risk factor to watch today]
CLEARANCE: [FULL TRADING/REDUCED TRADING/CAUTION/NO TRADING]"""

    result = call_claude(prompt, 700)
    print(f"Agent 4 complete ✓")
    return result


# ── AGENT 5 — SENTIMENT SCORER ────────────────────────────────────
def agent5_sentiment(fng):
    print(f"\n[{now_sast()}] 💭 Agent 5: Sentiment Scorer starting...")

    prompt = f"""You are Agent 5 — Global Sentiment Scorer for Claude-Market.

Current Fear & Greed Index: {fng['value']}/100 — {fng['label']}

Analyse global market sentiment across multiple dimensions:

1. CRYPTO SENTIMENT (Fear & Greed: {fng['value']}/100)
2. EQUITY SENTIMENT (US markets mood)
3. COMMODITY SENTIMENT (Gold/Silver/Oil demand)
4. FOREX SENTIMENT (USD strength/weakness)
5. EMERGING MARKET SENTIMENT (SA, Brazil, India mood)
6. GEOPOLITICAL RISK (global tension level)

Ideal trading conditions:
- Fear & Greed 30-70: Normal — trade normally
- Fear & Greed <30: Extreme Fear — buy opportunities in quality assets
- Fear & Greed >70: Extreme Greed — be cautious, tighten stops
- VIX high: volatility risk — reduce position sizes
- USD strong: bearish for GOLD/crypto, consider SELL setups

Format EXACTLY:
OVERALL_SENTIMENT: [BULLISH/BEARISH/NEUTRAL/MIXED]
CRYPTO_MOOD: [FEARFUL/NEUTRAL/GREEDY] — [reason]
EQUITY_MOOD: [BEARISH/NEUTRAL/BULLISH] — [reason]
COMMODITY_MOOD: [BEARISH/NEUTRAL/BULLISH] — [reason]
FOREX_MOOD: [USD STRONG/WEAK/NEUTRAL] — [reason]
FNG_SCORE: {fng['value']}/100 — {fng['label']}
GEOPOLITICAL_RISK: [LOW/MEDIUM/HIGH]
BEST_ASSETS_TODAY: [top 3 assets with best sentiment alignment]
AVOID_TODAY: [assets to avoid based on sentiment]
SENTIMENT_SCORE: [X/10 — overall trading conditions today]
NOTE: [one key sentiment observation for today]"""

    result = call_claude(prompt, 700)
    print(f"Agent 5 complete ✓")
    return result


# ── MASTER COMPILER ───────────────────────────────────────────────
def master_compiler(a1, a2, a3, a4, a5, fng):
    print(f"\n[{now_sast()}] 🎯 Master: Compiling final report...")

    prompt = f"""You are the Master Compiler for Claude-Market.

Combine all agent reports into a clear, actionable daily brief.

AGENT 1 — GLOBAL NEWS:
{a1}

AGENT 2 — OPPORTUNITIES:
{a2}

AGENT 3 — PERFORMANCE:
{a3}

AGENT 4 — RISK MANAGER:
{a4}

AGENT 5 — SENTIMENT:
{a5}

Fear & Greed: {fng['value']}/100 — {fng['label']}

Create a MASTER BRIEF with:
1. HEADLINE: The single most important thing to know today
2. SYSTEM STATUS: Is Claude-Market cleared to trade today?
3. TOP 3 OPPORTUNITIES: Best setups approved by Risk Manager
4. AVOID TODAY: What NOT to trade
5. KEY RISK: Most important risk to watch
6. SENTIMENT: Market mood in one line
7. ACTION PLAN: Exact instructions for the system today

Keep it clear and direct. This brief goes to the trader every morning.
End with a motivational line for Lukas in Pretoria! 🇿🇦"""

    result = call_claude(prompt, 1000)
    print(f"Master compilation complete ✓")
    return result


# ── EMAIL BUILDER ─────────────────────────────────────────────────
def build_email(a1, a2, a3, a4, a5, master, fng):
    time_str = now_sast()

    html = f"""
<html><body style="font-family:Arial,sans-serif;background:#020608;color:#c8e6f0;padding:20px;">

<div style="max-width:700px;margin:0 auto;">

<!-- HEADER -->
<div style="background:linear-gradient(135deg,#071520,#040d12);border:1px solid #0a2535;
            border-top:3px solid #00e5ff;padding:24px;border-radius:6px;margin-bottom:16px;">
  <h1 style="font-family:monospace;color:#00e5ff;letter-spacing:4px;margin:0;">
    🤖 CLAUDE-MARKET</h1>
  <p style="color:#4a7a8a;margin:6px 0 0;font-size:13px;">
    Daily Intelligence Brief · {time_str}</p>
  <p style="color:#4a7a8a;font-size:12px;margin:4px 0 0;">
    Lukas Ferreira · Account 107072723 · Ava-Demo 1-MT5</p>
</div>

<!-- FEAR & GREED -->
<div style="background:#071520;border:1px solid #0a2535;border-left:3px solid #ffcc00;
            padding:16px;border-radius:4px;margin-bottom:12px;">
  <span style="color:#ffcc00;font-family:monospace;font-size:12px;letter-spacing:2px;">
    ⚡ FEAR & GREED INDEX</span>
  <h2 style="color:#00ff88;margin:8px 0 0;font-family:monospace;">
    {fng['value']}/100 — {fng['label']}</h2>
</div>

<!-- MASTER BRIEF -->
<div style="background:#071520;border:1px solid #0a2535;border-left:3px solid #00e5ff;
            padding:16px;border-radius:4px;margin-bottom:12px;">
  <p style="color:#00e5ff;font-family:monospace;font-size:11px;letter-spacing:2px;margin:0 0 12px;">
    🎯 MASTER BRIEF</p>
  <pre style="color:#c8e6f0;font-size:13px;white-space:pre-wrap;margin:0;line-height:1.8;">
{master}</pre>
</div>

<!-- AGENT 1 -->
<div style="background:#071520;border:1px solid #0a2535;padding:16px;
            border-radius:4px;margin-bottom:12px;">
  <p style="color:#00e5ff;font-family:monospace;font-size:11px;letter-spacing:2px;margin:0 0 12px;">
    🌍 AGENT 1 — GLOBAL NEWS SWEEP</p>
  <pre style="color:#c8e6f0;font-size:12px;white-space:pre-wrap;margin:0;line-height:1.7;">
{a1}</pre>
</div>

<!-- AGENT 2 -->
<div style="background:#071520;border:1px solid #0a2535;border-left:3px solid #00ff88;
            padding:16px;border-radius:4px;margin-bottom:12px;">
  <p style="color:#00ff88;font-family:monospace;font-size:11px;letter-spacing:2px;margin:0 0 12px;">
    🔭 AGENT 2 — GLOBAL OPPORTUNITY SCANNER</p>
  <pre style="color:#c8e6f0;font-size:12px;white-space:pre-wrap;margin:0;line-height:1.7;">
{a2}</pre>
</div>

<!-- AGENT 3 -->
<div style="background:#071520;border:1px solid #0a2535;padding:16px;
            border-radius:4px;margin-bottom:12px;">
  <p style="color:#00e5ff;font-family:monospace;font-size:11px;letter-spacing:2px;margin:0 0 12px;">
    📊 AGENT 3 — PERFORMANCE REVIEW</p>
  <pre style="color:#c8e6f0;font-size:12px;white-space:pre-wrap;margin:0;line-height:1.7;">
{a3}</pre>
</div>

<!-- AGENT 4 -->
<div style="background:#071520;border:1px solid #0a2535;border-left:3px solid #ff2d55;
            padding:16px;border-radius:4px;margin-bottom:12px;">
  <p style="color:#ff2d55;font-family:monospace;font-size:11px;letter-spacing:2px;margin:0 0 12px;">
    🛡️ AGENT 4 — RISK MANAGER</p>
  <pre style="color:#c8e6f0;font-size:12px;white-space:pre-wrap;margin:0;line-height:1.7;">
{a4}</pre>
</div>

<!-- AGENT 5 -->
<div style="background:#071520;border:1px solid #0a2535;border-left:3px solid #ffcc00;
            padding:16px;border-radius:4px;margin-bottom:12px;">
  <p style="color:#ffcc00;font-family:monospace;font-size:11px;letter-spacing:2px;margin:0 0 12px;">
    💭 AGENT 5 — SENTIMENT SCORER</p>
  <pre style="color:#c8e6f0;font-size:12px;white-space:pre-wrap;margin:0;line-height:1.7;">
{a5}</pre>
</div>

<!-- FOOTER -->
<div style="background:#071520;border:1px solid #0a2535;padding:12px 16px;
            border-radius:4px;text-align:center;">
  <p style="color:#4a7a8a;font-size:11px;font-family:monospace;margin:0;">
    Claude-Market v3.1 · EA v7.8 · 5 AI Agents · All systems operational</p>
  <p style="color:#4a7a8a;font-size:10px;font-family:monospace;margin:4px 0 0;">
    30-Day Challenge: 05 May → 04 Jun 2026 · Pretoria 🇿🇦</p>
</div>

</div>
</body></html>"""

    return html


# ── SEND EMAIL ────────────────────────────────────────────────────
def send_email(html_body, subject):
    if not GMAIL_USER or not GMAIL_PASS:
        print("⚠️  No email credentials — skipping email")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["From"]    = GMAIL_USER
        msg["To"]      = GMAIL_USER
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.send_message(msg)

        print(f"✅ Email sent: {subject}")
    except Exception as e:
        print(f"❌ Email failed: {e}")


# ── MAIN ─────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"  CLAUDE-MARKET DAILY INTELLIGENCE v2.0")
    print(f"  {now_sast()}")
    print(f"  Lukas Ferreira · Account 107072723")
    print("=" * 60)

    # Fear & Greed
    fng = get_fear_greed()
    print(f"\n📊 Fear & Greed: {fng['value']}/100 — {fng['label']}")

    # Run all 5 agents
    a1 = agent1_global_news()
    sleep_between("Agent 2")

    a2 = agent2_global_scanner(a1)
    sleep_between("Agent 3")

    a3 = agent3_performance()
    sleep_between("Agent 4")

    a4 = agent4_risk(a2)
    sleep_between("Agent 5")

    a5 = agent5_sentiment(fng)
    sleep_between("Master Compiler")

    master = master_compiler(a1, a2, a3, a4, a5, fng)

    # Build and send email
    html   = build_email(a1, a2, a3, a4, a5, master, fng)
    subject = f"🤖 CM Brief: {now_sast()} | F&G {fng['value']}/100"
    send_email(html, subject)

    print("\n" + "=" * 60)
    print(f"  ALL AGENTS COMPLETE — {now_sast()}")
    print(f"  Next run: Tomorrow 07:00 SAST")
    print("=" * 60)


if __name__ == "__main__":
    main()
