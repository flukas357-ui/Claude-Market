"""
Claude-Market Daily Agents v2.0
Lukas Ferreira - Pretoria ZA
6 Agents: Global Scanner, Market Analysis, Performance Review,
          Risk Manager, Global Sentiment, Master Report
Runs: 07:00 SAST daily via GitHub Actions
"""

import anthropic
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import json
import requests
from datetime import datetime

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
WEBHOOK = os.environ.get("WEBHOOK_URL", "https://claude-market.onrender.com")

# ─── Shared Context ────────────────────────────────────────────────────────────
PRIMARY_ASSETS = ["GOLD", "SILVER", "BTCUSD", "ETHUSD", "NAS100"]

GLOBAL_ASSET_GROUPS = {
    "Forex Majors":  ["EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","USDCAD","NZDUSD","EURGBP","EURJPY"],
    "Forex Minors":  ["GBPJPY","AUDJPY","CADJPY","CHFJPY","EURAUD","EURNZD","GBPAUD","AUDCAD","NZDCAD"],
    "Indices":       ["NAS100","US500","US30","GER40","UK100","JPN225","AUS200","FRA40","HKG50"],
    "Commodities":   ["GOLD","SILVER","USOIL","COPPER","NATGAS","PLATINUM","PALLADIUM","WHEAT","COFFEE"],
    "Crypto":        ["BTCUSD","ETHUSD","SOLUSD","BNBUSD","XRPUSD","ADAUSD","DOTUSD","AVAXUSD","LINKUSD"],
    "SA & Emerging": ["USDZAR","EURZAR","GBPZAR","XAUUSD","USDMXN","USDBRL","USDTRY","USDCNH"]
}

def call_claude(prompt: str, max_tokens: int = 800) -> str:
    """Core Claude API call"""
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip()

def get_webhook_status() -> dict:
    """Fetch live data from webhook server"""
    try:
        r = requests.get(WEBHOOK + "/", timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def get_mt5_status() -> dict:
    """Fetch MT5 account data"""
    try:
        r = requests.get(WEBHOOK + "/status", timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def get_scanner_results() -> dict:
    """Fetch last scanner results"""
    try:
        r = requests.get(WEBHOOK + "/scanner/results", timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# ═════════════════════════════════════════════════════════════════════════════
# AGENT 1: Global Opportunity Scanner
# ═════════════════════════════════════════════════════════════════════════════
def agent1_global_scanner() -> str:
    """Scans all 54 assets across 6 groups for top opportunities today"""
    print("[Agent 1] Global Opportunity Scanner starting...")

    group_results = {}
    for group, assets in GLOBAL_ASSET_GROUPS.items():
        prompt = f"""You are the Claude-Market Global Opportunity Scanner.
        
Group: {group}
Assets to analyse: {', '.join(assets)}
Today: {datetime.now().strftime('%A %d %B %Y, %H:%M SAST')}

Analyse each asset for today's BEST TRADING OPPORTUNITIES considering:
- Current market phase (trending vs ranging)
- Key support and resistance levels
- Upcoming economic events or news catalysts
- Session timing (GOLD/SILVER = London/NY, Crypto = 24/7, NAS = US session)
- Risk/reward quality

Select the TOP 3 assets from this group most likely to provide profitable setups today.

Reply in this exact format:
GROUP: {group}
WINNER: [SYMBOL] | [BUY/SELL] | Confidence: [HIGH/MEDIUM/LOW] | Reason: [brief]
2ND:    [SYMBOL] | [BUY/SELL] | Confidence: [HIGH/MEDIUM/LOW] | Reason: [brief]
3RD:    [SYMBOL] | [BUY/SELL] | Confidence: [HIGH/MEDIUM/LOW] | Reason: [brief]"""

        result = call_claude(prompt, 300)
        group_results[group] = result
        print(f"  {group}: Done")

    # Compile into summary
    all_results = "\n\n".join(group_results.values())
    summary_prompt = f"""You are the Claude-Market Master Scanner. 
Here are the top picks from all 6 asset groups:

{all_results}

Compile the OVERALL TOP 5 OPPORTUNITIES across ALL groups for today.
Consider: highest confidence, best risk/reward, avoid duplicates, time-of-day suitability.

Format:
🏆 TOP 5 GLOBAL OPPORTUNITIES TODAY

1. [SYMBOL] | [BUY/SELL] | Score: X/5 | [Group] | [Reason]
2. [SYMBOL] | [BUY/SELL] | Score: X/5 | [Group] | [Reason]
3. [SYMBOL] | [BUY/SELL] | Score: X/5 | [Group] | [Reason]
4. [SYMBOL] | [BUY/SELL] | Score: X/5 | [Group] | [Reason]
5. [SYMBOL] | [BUY/SELL] | Score: X/5 | [Group] | [Reason]

⚠️ DO NOT TRADE: [list any assets to avoid today and why]"""

    return call_claude(summary_prompt, 600)

# ═════════════════════════════════════════════════════════════════════════════
# AGENT 2: Primary Asset Analysis
# ═════════════════════════════════════════════════════════════════════════════
def agent2_market_analysis() -> str:
    """Deep analysis of the 5 primary trading assets"""
    print("[Agent 2] Primary Asset Analysis starting...")
    prompt = f"""You are Claude-Market's Primary Asset Analyst.
Today: {datetime.now().strftime('%A %d %B %Y, %H:%M SAST')}

Provide detailed analysis for each of Claude-Market's 5 primary assets:
{', '.join(PRIMARY_ASSETS)}

For EACH asset provide:
- Market phase: TRENDING UP / TRENDING DOWN / RANGING / BREAKOUT
- Key levels: Support $X | Resistance $X
- BB Breakout signal: LIKELY / UNLIKELY / ALREADY FIRED
- Best entry window (time/session)
- Risk rating: LOW / MEDIUM / HIGH
- One-line verdict

Format clearly with asset name as header.
Keep each analysis to 5 lines maximum.
Be specific with price levels where possible."""

    return call_claude(prompt, 800)

# ═════════════════════════════════════════════════════════════════════════════
# AGENT 3: Performance Review
# ═════════════════════════════════════════════════════════════════════════════
def agent3_performance_review() -> str:
    """Reviews MT5 account performance and open positions"""
    print("[Agent 3] Performance Review starting...")
    mt5 = get_mt5_status()
    scanner = get_scanner_results()
    webhook = get_webhook_status()

    # Format live data for the agent
    live_data = json.dumps({
        "mt5": mt5,
        "scanner_last_run": scanner.get("last_run"),
        "scanner_winner": scanner.get("global_winner"),
        "pending_signal": webhook.get("pending_signal"),
        "trading_enabled": webhook.get("trading_enabled")
    }, indent=2)

    prompt = f"""You are Claude-Market's Performance Review Agent.
Today: {datetime.now().strftime('%A %d %B %Y, %H:%M SAST')}
Developer: Lukas Ferreira, Pretoria 🇿🇦

Live system data:
{live_data}

Provide a morning performance briefing covering:
1. ACCOUNT STATUS: Balance, Equity, Daily P&L, Margin usage
2. OPEN POSITIONS: Each open trade — entry, current status, recommendation (hold/close/trail)
3. TRAILING STOP STATUS: Which trades have trail activated (L1/L2/L3)?
4. SYSTEM HEALTH: Webhook, EA, Scanner — all green?
5. LAST 24H SUMMARY: What the scanner found and traded overnight

If no live data available, note "MT5 offline — check EA connection".
Be concise and action-oriented. Use emojis for quick scanning."""

    return call_claude(prompt, 800)

# ═════════════════════════════════════════════════════════════════════════════
# AGENT 4: Risk Manager
# ═════════════════════════════════════════════════════════════════════════════
def agent4_risk_manager(agent1_report: str, agent2_report: str) -> str:
    """Validates opportunities and enforces risk rules"""
    print("[Agent 4] Risk Manager starting...")
    mt5 = get_mt5_status()
    balance = mt5.get("balance", mt5.get("equity", 10000))

    prompt = f"""You are Claude-Market's Risk Manager.
Today: {datetime.now().strftime('%A %d %B %Y, %H:%M SAST')}
Account Balance: ${balance}
Risk per trade: 2% = ${float(balance)*0.02:.2f}
Max daily loss: 6% = ${float(balance)*0.06:.2f}
Max positions per symbol: 2

Agent 1 found these opportunities:
{agent1_report[:1000]}

Agent 2 primary analysis:
{agent2_report[:800]}

As Risk Manager, your job is to:
1. APPROVE or REJECT each opportunity based on risk rules
2. Check for over-exposure (too many correlated assets)
3. Flag if daily loss limit is near
4. Set appropriate lot sizes for approved trades
5. Identify if any assets should be avoided today (news events, gaps, volatility)

Format:
✅ APPROVED TRADES TODAY:
[List with lot sizes]

❌ REJECTED TRADES:
[List with reasons]

⚠️ RISK WARNINGS:
[Any alerts]

📋 RISK PARAMETERS:
- 2% rule: OK/BREACHED
- Correlation check: OK/WARNING
- News events: [list major events today]"""

    return call_claude(prompt, 600)

# ═════════════════════════════════════════════════════════════════════════════
# AGENT 5: Global Sentiment
# ═════════════════════════════════════════════════════════════════════════════
def agent5_global_sentiment() -> str:
    """Assesses global market sentiment for all asset classes"""
    print("[Agent 5] Global Sentiment starting...")
    prompt = f"""You are Claude-Market's Global Sentiment Analyst.
Today: {datetime.now().strftime('%A %d %B %Y, %H:%M SAST')}

Assess market sentiment across all asset classes for today's trading:

1. CRYPTO SENTIMENT
   - BTC/ETH market mood
   - Fear & Greed estimate
   - DeFi/Alt season indicator

2. EQUITY SENTIMENT
   - US markets (S&P, NASDAQ, DOW)
   - European markets (DAX, FTSE)
   - Asian markets (Nikkei, Hang Seng)
   - SA/Emerging markets

3. COMMODITY SENTIMENT
   - Gold/Silver safe haven demand
   - Oil supply/demand dynamics
   - Base metals outlook

4. FOREX SENTIMENT
   - USD strength/weakness
   - Risk-on vs Risk-off
   - USDZAR outlook for SA traders

5. KEY MACRO EVENTS THIS WEEK
   - Fed/SARB decisions
   - Major economic releases
   - Geopolitical factors

Score each category: BULLISH / NEUTRAL / BEARISH
Add one sentence on how this affects Claude-Market's 5 primary assets."""

    return call_claude(prompt, 800)

# ═════════════════════════════════════════════════════════════════════════════
# AGENT 6: Range Opportunity Scout
# ═════════════════════════════════════════════════════════════════════════════
def agent6_range_scout() -> str:
    """Specifically looks for range-trading opportunities (new agent)"""
    print("[Agent 6] Range Scout starting...")
    prompt = f"""You are Claude-Market's Range Trading Scout.
Today: {datetime.now().strftime('%A %d %B %Y, %H:%M SAST')}
Day of week: {datetime.now().strftime('%A')}

Claude-Market now has a dual strategy:
1. BB Breakout (for trending markets)
2. Range Trading (for sideways markets — 70% of market time)

Scan for RANGING market conditions across all asset classes:

For each ranging asset found, provide:
- Symbol (exact MT5 broker name)
- Range boundaries: Support $X — Resistance $X
- Range width in pips/points
- Current price position in range (near top/middle/bottom)
- RECOMMENDED ENTRY: BUY near support OR SELL near resistance
- Expected duration (hours/days)
- Confidence: HIGH/MEDIUM/LOW

Focus especially on:
- Weekend carryover ranges (markets that gapped and are settling)
- Holiday/low-volume consolidation
- Assets between major levels with clear floor and ceiling
- Small, tight, reliable ranges (2-4% width ideal)

Report the TOP 3 RANGE OPPORTUNITIES right now.
Be specific with price levels.
Note if any of the 5 primary assets (GOLD, SILVER, BTC, ETH, NAS100) are ranging."""

    return call_claude(prompt, 600)

# ═════════════════════════════════════════════════════════════════════════════
# MASTER AGENT: Compile Final Report
# ═════════════════════════════════════════════════════════════════════════════
def master_agent(reports: dict) -> str:
    """Compiles all 6 agent reports into the daily briefing"""
    print("[Master] Compiling final report...")
    prompt = f"""You are the Claude-Market Master Agent.
Today: {datetime.now().strftime('%A %d %B %Y, %H:%M SAST')}
Developer: Lukas Ferreira — Pretoria, South Africa 🇿🇦

You have received reports from 6 specialist agents. Compile them into the definitive daily briefing.

AGENT 1 (Global Scanner):
{reports['agent1'][:800]}

AGENT 2 (Primary Analysis):
{reports['agent2'][:800]}

AGENT 3 (Performance Review):
{reports['agent3'][:600]}

AGENT 4 (Risk Manager):
{reports['agent4'][:600]}

AGENT 5 (Global Sentiment):
{reports['agent5'][:600]}

AGENT 6 (Range Scout):
{reports['agent6'][:500]}

Compile the DEFINITIVE CLAUDE-MARKET DAILY BRIEFING:

Structure:
📊 CLAUDE-MARKET DAILY BRIEFING — {datetime.now().strftime('%A %d %B %Y')}
🇿🇦 Lukas Ferreira | Pretoria | {datetime.now().strftime('%H:%M')} SAST

💰 ACCOUNT STATUS
[From Agent 3 — balance, equity, open P&L, positions]

🏆 TOP OPPORTUNITIES TODAY
[Top 5 from Agent 1 — only risk-manager approved ones from Agent 4]

📈 PRIMARY ASSETS
[Key levels and signals for GOLD, SILVER, BTC, ETH, NAS100]

↔️ RANGE OPPORTUNITIES (Dual Strategy)
[Top 3 from Agent 6 — for the sideways market strategy]

🌍 GLOBAL SENTIMENT
[1-paragraph summary from Agent 5]

⚠️ RISK ALERTS
[From Agent 4 — what to watch, what to avoid]

🤖 SYSTEM HEALTH
[Webhook, EA, Scanner status from Agent 3]

📋 ACTION PLAN FOR TODAY
[3-5 specific actionable items]

Keep it sharp, professional, and actionable. Use emojis for visual scanning."""

    return call_claude(prompt, 1200)

# ═════════════════════════════════════════════════════════════════════════════
# EMAIL SENDER
# ═════════════════════════════════════════════════════════════════════════════
def send_email(report: str):
    """Send the daily briefing via email"""
    EMAIL_FROM    = os.environ.get("EMAIL_FROM", "")
    EMAIL_TO      = os.environ.get("EMAIL_TO", "")
    EMAIL_PASS    = os.environ.get("EMAIL_PASS", "")
    SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))

    if not EMAIL_FROM or not EMAIL_TO:
        print("[Email] Skipped — EMAIL_FROM or EMAIL_TO not configured")
        return

    subject = f"⚡ Claude-Market Briefing — {datetime.now().strftime('%a %d %b %Y')} | Lukas 🇿🇦"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO

    # Plain text version
    msg.attach(MIMEText(report, "plain"))

    # HTML version (basic formatting)
    html_report = report.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
    html_body = f"""
    <html><body style="font-family: monospace; background: #0a0e1a; color: #e2e8f0; padding: 20px;">
    <div style="max-width: 700px; margin: 0 auto;">
    <h2 style="color: #00ff88;">⚡ Claude-Market Daily Briefing</h2>
    <p style="color: #64748b;">{datetime.now().strftime('%A %d %B %Y — %H:%M SAST')} | Lukas Ferreira 🇿🇦</p>
    <hr style="border-color: #2d3748;">
    <pre style="white-space: pre-wrap; font-size: 13px;">{html_report}</pre>
    <hr style="border-color: #2d3748;">
    <p style="color: #64748b; font-size: 11px;">
      Webhook: <a href="{WEBHOOK}" style="color:#4fa3ff">{WEBHOOK}</a> |
      Dashboard: <a href="https://claude-market-za.netlify.app" style="color:#4fa3ff">claude-market-za.netlify.app</a>
    </p>
    </div></body></html>
    """
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASS)
            server.send_message(msg)
        print(f"[Email] Report sent to {EMAIL_TO}")
    except Exception as e:
        print(f"[Email] Failed: {e}")

# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════
def main():
    print("=" * 60)
    print(f"Claude-Market Agents v2.0 — {datetime.now().strftime('%H:%M SAST')}")
    print(f"Developer: Lukas Ferreira — Pretoria 🇿🇦")
    print("=" * 60)

    # Run all 6 agents
    reports = {}
    try: reports['agent1'] = agent1_global_scanner()
    except Exception as e: reports['agent1'] = f"Agent 1 error: {e}"

    try: reports['agent2'] = agent2_market_analysis()
    except Exception as e: reports['agent2'] = f"Agent 2 error: {e}"

    try: reports['agent3'] = agent3_performance_review()
    except Exception as e: reports['agent3'] = f"Agent 3 error: {e}"

    try: reports['agent4'] = agent4_risk_manager(reports.get('agent1',''), reports.get('agent2',''))
    except Exception as e: reports['agent4'] = f"Agent 4 error: {e}"

    try: reports['agent5'] = agent5_global_sentiment()
    except Exception as e: reports['agent5'] = f"Agent 5 error: {e}"

    try: reports['agent6'] = agent6_range_scout()
    except Exception as e: reports['agent6'] = f"Agent 6 error: {e}"

    # Master compilation
    try:
        final_report = master_agent(reports)
    except Exception as e:
        final_report = f"Master Agent error: {e}\n\n" + "\n\n".join(
            f"=== AGENT {i+1} ===\n{reports.get(f'agent{i+1}','N/A')}" for i in range(6)
        )

    print("\n" + "=" * 60)
    print("FINAL REPORT")
    print("=" * 60)
    print(final_report)

    # Send email
    send_email(final_report)
    print("\n[Done] All 6 agents completed.")

if __name__ == "__main__":
    main()
