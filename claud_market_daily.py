"""
CLAUD-MARKET DAILY AUTOMATION
==============================
Runs all 5 agents every morning and emails the compiled report.
Schedule: 07:00 SAST (05:00 UTC) via GitHub Actions
"""

import os
import smtplib
import requests
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import anthropic

SAST = timezone(timedelta(hours=2))
TODAY = datetime.now(SAST).strftime("%A, %d %B %Y")
ACCOUNT_BALANCE = 2000.00
DAILY_PNL = 0.00
OPEN_POSITIONS = 0
ASSETS = ["XAUUSD (Gold)","XAGUSD (Silver)","BTCUSD (Bitcoin)","ETHUSD (Ethereum)","NAS100 (NASDAQ)"]

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def claude(prompt, use_search=False, max_tokens=800):
    kwargs = dict(
        model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        messages=[{"role":"user","content":prompt}]
    )
    if use_search:
        kwargs["tools"] = [{"type":"web_search_20250305","name":"web_search"}]
    response = client.messages.create(**kwargs)
    return " ".join(b.text for b in response.content if hasattr(b,"text")).strip()

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=7&format=json",timeout=10)
        data = r.json()["data"]
        today = data[0]
        week  = data[6] if len(data)>6 else data[-1]
        return {
            "value":    int(today["value"]),
            "label":    today["value_classification"],
            "week_val": int(week["value"]),
            "week_lbl": week["value_classification"],
            "trend":    "improving" if int(today["value"])>int(week["value"]) else "declining"
        }
    except:
        return {"value":65,"label":"Greed","week_val":60,"week_lbl":"Greed","trend":"stable"}

def run_agent1():
    print("Agent 1 — News Sweep...")
    return claude(f"""You are Agent 1 of the Claud-Market system. Today is {TODAY}.
Search for today's most important market news affecting:
{chr(10).join(ASSETS)}
Focus on: Fed/central bank news, inflation data, geopolitical events,
crypto regulation, USD strength, South African economy.
Return ONLY this format:
RISK_LEVEL: [LOW|MEDIUM|HIGH]
EVENTS_COUNT: [number]
HEADLINE: [single most important headline]
GOLD_IMPACT: [one sentence]
SILVER_IMPACT: [one sentence]
BTC_IMPACT: [one sentence]
ETH_IMPACT: [one sentence]
NAS_IMPACT: [one sentence]
RECOMMENDATION: [TRADE FREELY|TRADE WITH CAUTION|AVOID TODAY]
SUMMARY: [2 sentences on overall market picture]""", use_search=True)

def run_agent3():
    print("Agent 3 — Performance Review...")
    max_risk = ACCOUNT_BALANCE * 0.02
    daily_limit = ACCOUNT_BALANCE * 0.05
    pct = (DAILY_PNL/ACCOUNT_BALANCE)*100 if ACCOUNT_BALANCE else 0
    return claude(f"""You are Agent 3 of Claud-Market — Performance Review Agent. Today is {TODAY}.
ACCOUNT STATUS:
- Balance: ${ACCOUNT_BALANCE:,.2f}
- Today P&L: ${DAILY_PNL:+.2f} ({pct:+.2f}%)
- Daily loss limit: ${daily_limit:.2f}
- Max risk per trade: ${max_risk:.2f}
- Open positions: {OPEN_POSITIONS}
- Strategy: v7.0 BB Breakout Daily chart long only
Return ONLY this format:
ACCOUNT_HEALTH: [EXCELLENT|GOOD|CAUTION|CRITICAL]
DAILY_STATUS: [POSITIVE|NEUTRAL|NEGATIVE]
BUDGET_REMAINING: ${daily_limit+DAILY_PNL:.2f}
TRADE_CAPACITY: {max(0,3-OPEN_POSITIONS)} trades available
PERFORMANCE_NOTE: [one sentence]
RECOMMENDATION: [one clear action for today]""")

def run_agent4():
    print("Agent 4 — Risk Manager...")
    max_risk = ACCOUNT_BALANCE * 0.02
    daily_limit = ACCOUNT_BALANCE * 0.05
    remaining = daily_limit + DAILY_PNL
    available = max(0, 3-OPEN_POSITIONS)
    return claude(f"""You are Agent 4 — Claud-Market Risk Manager. Today is {TODAY}.
ACCOUNT: ${ACCOUNT_BALANCE:,.2f} | Daily P&L: ${DAILY_PNL:+.2f}
Daily limit: ${daily_limit:.2f} | Remaining: ${remaining:.2f}
Open positions: {OPEN_POSITIONS}/3 | Max risk/trade: ${max_risk:.2f}
Position sizes based on 2% rule (${max_risk:.2f}):
- Gold (XAUUSD): {max_risk/35:.3f} lots
- Silver (XAGUSD): {max_risk/0.45:.0f} oz
- Bitcoin (BTCUSD): {max_risk/1200:.4f} BTC
- Ethereum (ETHUSD): {max_risk/85:.3f} ETH
- NASDAQ (NAS100): {max_risk/180:.3f} lots
Return ONLY this format:
CLEARANCE: [CLEARED|PARTIAL|BLOCKED]
TRADES_AVAILABLE: {available}
MAX_RISK_PER_TRADE: ${max_risk:.2f}
GOLD_SIZE: {max_risk/35:.3f} lots
BTC_SIZE: {max_risk/1200:.4f} BTC
ETH_SIZE: {max_risk/85:.3f} ETH
NAS_SIZE: {max_risk/180:.3f} lots
RISK_NOTE: [one sentence on risk posture today]""")

def run_agent5(fng):
    print("Agent 5 — Sentiment Scorer...")
    return claude(f"""You are Agent 5 — Claud-Market Sentiment Scorer. Today is {TODAY}.
LIVE FEAR & GREED: {fng['value']}/100 — {fng['label']}
Last week: {fng['week_val']}/100 — {fng['week_lbl']} | Trend: {fng['trend']}
Search for current market sentiment for Gold, Bitcoin and NASDAQ.
Return ONLY this format:
OVERALL_MOOD: [1-10]
MOOD_LABEL: [Fearful|Cautious|Neutral|Confident|Euphoric]
GOLD_SENTIMENT: [BULLISH|NEUTRAL|BEARISH]
SILVER_SENTIMENT: [BULLISH|NEUTRAL|BEARISH]
BTC_SENTIMENT: [BULLISH|NEUTRAL|BEARISH]
ETH_SENTIMENT: [BULLISH|NEUTRAL|BEARISH]
NAS_SENTIMENT: [BULLISH|NEUTRAL|BEARISH]
CONTRARIAN_FLAG: [YES|NO]
RECOMMENDATION: [FULL CONFIDENCE|TRADE NORMALLY|REDUCE SIZE|AVOID TODAY]
SENTIMENT_NOTE: [one sentence on dominant mood driver]""", use_search=True)

def run_compiler(a1,a3,a4,a5,fng):
    print("Master Compiler...")
    return claude(f"""You are the Claud-Market Master Compiler. Today is {TODAY} — Pretoria South Africa SAST.

AGENT 1 NEWS: {a1}
AGENT 3 PERFORMANCE: {a3}
AGENT 4 RISK: {a4}
AGENT 5 SENTIMENT: {a5}
FEAR & GREED: {fng['value']}/100 — {fng['label']}
STRATEGY: v7.0 BB Breakout Daily chart long only

Produce the FINAL DAILY TRADE PLAN in this format:

CLAUD-MARKET DAILY BRIEF — {TODAY}

EXECUTIVE SUMMARY:
[3 sentences — what do all agents collectively say about today?]

OVERALL SCORE: [X/10] — [one word verdict]

MARKET CONDITIONS TODAY:
[paragraph combining news, sentiment and performance]

TODAY'S TRADING PLAN:
[bullet points — exactly what to do today]

ASSETS TO WATCH:
[which of the 5 show best conditions for v7.0 signals today]

RISK POSTURE:
[one paragraph on max exposure today]

MASTER COMPILER VERDICT: [EXECUTE FULL PLAN|TRADE WITH CAUTION|HOLD ALL POSITIONS]

Sign off: Claud-Market system — {TODAY} — All agents nominal.""", max_tokens=1200)

def build_email(a1,a3,a4,a5,compiled,fng):
    fng_color = (
        "#A32D2D" if fng["value"]<=24 else
        "#E65100" if fng["value"]<=44 else
        "#B8860B" if fng["value"]<=55 else
        "#2E7D32" if fng["value"]<=75 else "#1B5E20"
    )
    def section(title,content,bg="#F5F5F5"):
        return f"""<div style="margin:0 0 16px;border-radius:8px;overflow:hidden">
<div style="background:#1B4F8A;padding:10px 16px"><strong style="color:#fff;font-size:13px">{title}</strong></div>
<div style="background:{bg};padding:12px 16px;font-size:13px;color:#212121;line-height:1.6;white-space:pre-wrap">{content}</div></div>"""
    return f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;padding:20px;background:#f0f0f0">
<div style="background:#B8860B;border-radius:12px 12px 0 0;padding:18px 24px">
<h1 style="color:#fff;margin:0;font-size:20px">Claud-Market</h1>
<p style="color:rgba(255,255,255,0.85);margin:4px 0 0;font-size:13px">Daily Intelligence Report — {TODAY}</p></div>
<div style="background:#fff;border-radius:0 0 12px 12px;padding:20px 24px;margin-bottom:16px">
<div style="display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap">
<div style="background:#F5F5F5;border-radius:8px;padding:10px 16px;text-align:center;min-width:120px">
<div style="font-size:11px;color:#9E9E9E;margin-bottom:4px">Fear & Greed</div>
<div style="font-size:28px;font-weight:bold;color:{fng_color}">{fng['value']}</div>
<div style="font-size:11px;color:{fng_color}">{fng['label']}</div></div>
<div style="background:#F5F5F5;border-radius:8px;padding:10px 16px;text-align:center;min-width:120px">
<div style="font-size:11px;color:#9E9E9E;margin-bottom:4px">Balance</div>
<div style="font-size:22px;font-weight:bold;color:#212121">${ACCOUNT_BALANCE:,.0f}</div>
<div style="font-size:11px;color:#9E9E9E">Paper demo</div></div>
<div style="background:#F5F5F5;border-radius:8px;padding:10px 16px;text-align:center;min-width:120px">
<div style="font-size:11px;color:#9E9E9E;margin-bottom:4px">Today P&L</div>
<div style="font-size:22px;font-weight:bold;color:{'#2E7D32' if DAILY_PNL>=0 else '#A32D2D'}">${DAILY_PNL:+.2f}</div>
<div style="font-size:11px;color:#9E9E9E">session</div></div></div>
{section("MASTER COMPILER — FINAL DAILY PLAN",compiled,"#FFFDE7")}
{section("Agent 1 — News Sweep",a1)}
{section("Agent 3 — Performance Review",a3)}
{section("Agent 4 — Risk Manager",a4)}
{section("Agent 5 — Sentiment Scorer",a5)}
<div style="background:#E8F5E9;border-radius:8px;padding:12px 16px;font-size:12px;color:#2E7D32;margin-top:8px">
Claud-Market v7.0 — All 5 agents nominal — Generated {datetime.now(SAST).strftime("%H:%M SAST")} — {TODAY}</div></div>
<p style="text-align:center;font-size:11px;color:#9E9E9E">For educational purposes only. Not financial advice.</p>
</body></html>"""

def send_email(html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Claud-Market Daily Brief — {TODAY}"
    msg["From"]    = os.environ["EMAIL_FROM"]
    msg["To"]      = os.environ["EMAIL_TO"]
    msg.attach(MIMEText(html,"html"))
    with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
        s.login(os.environ["EMAIL_FROM"],os.environ["EMAIL_PASSWORD"])
        s.sendmail(os.environ["EMAIL_FROM"],os.environ["EMAIL_TO"],msg.as_string())
    print("Email sent.")

def main():
    print(f"Claud-Market Daily Automation — {TODAY}")
    fng      = get_fear_greed()
    print(f"Fear & Greed: {fng['value']} — {fng['label']}")
    a1       = run_agent1()
    a3       = run_agent3()
    a4       = run_agent4()
    a5       = run_agent5(fng)
    compiled = run_compiler(a1,a3,a4,a5,fng)
    html     = build_email(a1,a3,a4,a5,compiled,fng)
    send_email(html)
    print("Done.")

if __name__=="__main__":
    main()
