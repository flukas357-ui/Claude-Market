# 🤖 Claude-Market — Fully Automated AI Trading System
> Built from scratch in 3 days — Pretoria, South Africa 🇿🇦
> Developer: Lukas Ferreira | Account: 107072723 | Broker: Ava-Demo 1-MT5
> Started: 04 May 2026 | First Automatic Trade: 06 May 2026
> 30-Day Challenge: 05 May → 04 June 2026

--- 

## 🌐 Live Links

| Component | URL |
|---|---|
| 📱 Dashboard | https://claude-market-za.netlify.app |
| 🖥️ Command Centre | Hosted on Netlify — same repo |
| 🔗 GitHub Repo | https://github.com/flukas357-ui/Claude-Market |
| ⚙️ GitHub Actions | https://github.com/flukas357-ui/Claude-Market/actions |
| 🌐 Webhook Server | https://claude-market.onrender.com |
| 📡 Signal Check | https://claude-market.onrender.com/signal |

---

## ✅ Current System Status — FULLY OPERATIONAL

```
✅ GitHub Actions          — runs 07:00 SAST daily
✅ 5 AI Agents             — email report working
✅ Webhook Server v3.1     — live on Render
✅ TradingView BB v7.0     — all 5 alerts connected & repeating
✅ MT5 Desktop             — account 107072723 connected
✅ ClaudMarket_EA v7.6     — running on all 5 charts
✅ Dynamic Position Sizing — auto-calculates lots from balance
✅ Smart Scale-In          — automatic 4-condition check
✅ Duplicate Protection    — max 2 trades per symbol enforced
✅ Command Centre          — full desktop dashboard
✅ First Auto Trade        — GOLD BUY placed 06 May 14:28 SAST
```

---

## 🏗️ Full System Architecture

```
╔══════════════════════════════════════════════════════════════╗
║              CLAUDE-MARKET AUTOMATION PIPELINE               ║
╚══════════════════════════════════════════════════════════════╝

DAILY INTELLIGENCE FLOW (07:00 SAST):
──────────────────────────────────────
GitHub Actions fires automatically
        ↓
claud_market_daily.py runs
        ↓
Agent 1 — News Sweep (live web search)
        ↓ (65s sleep)
Agent 3 — Performance Review
        ↓ (65s sleep)
Agent 4 — Risk Manager (2% rule)
        ↓ (65s sleep)
Agent 5 — Sentiment Scorer (Fear & Greed)
        ↓
Master Compiler → Email Report
        ↓
Gmail inbox 07:05 SAST daily

REAL-TIME TRADING FLOW (any time):
────────────────────────────────────
TradingView BB Breakout v7.0 fires
(Once Per Bar Close — 1D chart)
        ↓
POST → https://claude-market.onrender.com/webhook
        ↓
Webhook Server v3.1
→ Claude AI scores signal (5-box, min 4/5)
→ Stores signal in memory
        ↓
ClaudMarket_EA v7.6 polls GET /signal every 10s
        ↓
EA receives signal
→ Checks existing positions
→ Calculates dynamic lot size (2% of balance)
→ Smart scale-in if all 4 conditions met
→ OrderSend → AvaTrade MT5 Demo
        ↓
Trade placed with SL + TP automatically
        ↓
EA calls GET /signal/clear
        ↓
Email notification sent
        ↓
YOU JUST WATCH 🎯
```

---

## 🤖 The 5 AI Agents

| Agent | Role | Output |
|---|---|---|
| Agent 1 | News Sweep — live web search | Risk level, headlines, per-asset impact |
| Agent 3 | Performance Review | Account health, daily P&L, budget remaining |
| Agent 4 | Risk Manager | 2% rule check, position sizes, trade clearance |
| Agent 5 | Sentiment Scorer | Fear & Greed index, market mood score |
| Master | Report Compiler | Full email brief + approval cards |

---

## 📊 5-Box Signal Validation System

Every TradingView signal scored before trading:

| Box | Check | Pass Condition |
|---|---|---|
| Box 1 | Major asset? | GOLD/SILVER/BTC/ETH/NAS100 |
| Box 2 | Direction correct? | Claude AI analysis |
| Box 3 | Price reasonable? | Within normal range |
| Box 4 | Account health? | 2% risk rule respected |
| Box 5 | BB Signal confirmed? | AUTO PASS — TradingView confirmed |

**Minimum score: 4/5** — below 4 = rejected, no trade

---

## 🧠 EA v7.6 — Smart Trading Logic

### Dynamic Position Sizing
```
Account Balance × Risk% = Risk Amount
Risk Amount ÷ (SL distance × pip value) = Lot Size

Example at $9,664 balance:
$9,664 × 2% = $193 risk per trade
Lots calculated automatically — grows/shrinks with balance
```

### Smart Scale-In (Fully Automatic)
```
Signal arrives on existing position:

Condition 1 → Score must be 5/5 (maximum confidence)
Condition 2 → Existing trade profit > 1% of balance
Condition 3 → Total open risk < 3% of balance
Condition 4 → Less than 2 trades open on that symbol

ALL 4 PASS → Opens second trade at 1% risk (half size)
ANY FAIL   → Skips silently — no duplicate
```

### Position Rules
```
No position open          → Open new trade (2% risk)
Same direction + conditions met  → Scale in (1% risk)
Same direction + conditions fail → Skip (protect capital)
Opposite direction        → Close existing → Open new
Max trades per symbol     → 2 (hard limit, never exceeded)
```

---

## 💹 Trading Assets & TradingView Alerts

| Asset | Broker Symbol | Alert Status | Interval |
|---|---|---|---|
| Gold | GOLD | ✅ Active | 1D |
| Silver | SILVER | ✅ Active | 1D |
| Bitcoin | BTCUSD | ✅ Active | 1D |
| Ethereum | ETHUSD | ✅ Active | 1D |
| NASDAQ | NAS100 | ✅ Active | 1D |

**TradingView Webhook URL:**
```
https://claude-market.onrender.com/webhook
```

**Alert Message Format:**
```json
{"ticker": "{{ticker}}", "action": "BUY", "price": "{{close}}", "secret": "claude-market-2026"}
```

---

## 🗂️ File Structure

```
Claude-Market/
├── claud_market_daily.py      # 5 AI agents — 07:00 SAST daily
├── webhook_server.py          # v3.1 — receives TradingView alerts
├── ClaudMarket_EA.mq5         # v7.6 — MT5 Expert Advisor
├── CreateSignal.mq5           # v2.1 — manual test script
├── command_centre.html        # Full desktop trading dashboard
├── test_signal.html           # Signal test page
├── index.html                 # Mobile dashboard — Netlify
├── .github/
│   └── workflows/
│       └── daily_report.yml   # GitHub Actions — 07:00 SAST
└── README.md                  # This file
```

---

## 🔧 Webhook Server v3.1 Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Health check |
| `/webhook` | POST | Receives TradingView signals |
| `/signal` | GET | EA polls every 10 seconds |
| `/signal/clear` | GET | EA calls after trade placed |

---

## ⚙️ MT5 Setup Requirements

```
Tools → Options → Expert Advisors:
☑ Allow algorithmic trading
☑ Allow WebRequest for listed URLs:
  + https://claude-market.onrender.com
```

---

## 🐛 Full Bug Fix Log

| Date | Error | Fix Applied |
|---|---|---|
| 04 May | Invalid API key 401 | Regenerated Anthropic key |
| 04 May | No API credits 400 | Purchased $20 credits |
| 04 May | Agent 2 not defined | Removed — agents are 1,3,4,5 |
| 04 May | Rate limit 429 | Sleep increased to 65s |
| 04 May | Gmail auth failed | Created App Password |
| 04 May | GitHub Pages blocked | Moved to Netlify |
| 05 May | TradingView webhook blocked | Enabled 2FA |
| 05 May | MT5 web only | Installed MT5 Desktop |
| 06 May | Signal file on cloud server | Rebuilt to HTTP polling |
| 06 May | ERR 5004 file lock | Fixed FileClose() order |
| 06 May | XAUUSD not found | Added ResolveSymbol() |
| 06 May | Hedge not allowed 10030 | Added CloseExisting() |
| 06 May | ERR 4014 WebRequest | Enabled in MT5 Options |
| 06 May | Python global declaration | Moved global to function top |
| 06 May | ORDER_FILLING_RETURN rejected | Added GetFilling() auto-detector |
| 06 May | Invalid volume 10014 | Added NormalizeLots() |
| 06 May | **FIRST AUTOMATIC TRADE** | ✅ GOLD BUY 0.05 @ 4687.44 |
| 06 May | Duplicate trades opening | Added CountPositions() check |
| 06 May | Fixed lot sizes | Replaced with dynamic CalcLots() |
| 06 May | No scale-in logic | Built 4-condition smart scale-in |

---

## 🚀 Roadmap

### Phase 1 — COMPLETE ✅
- GitHub Actions + 5 AI Agents
- Webhook server live
- MT5 EA fully automated
- All 5 TradingView alerts connected
- Dynamic position sizing
- Smart scale-in logic
- Command Centre dashboard
- First automatic trade placed

### Phase 2 — Signal Quality (Next)
- Add SELL signal support
- Review first 10 trades — win rate analysis
- Adjust SL/TP based on real results
- Add news filter (no trades 30min before major events)

### Phase 3 — Risk Hardening
- Daily loss limit (stop if -$200 in a day)
- Weekend/holiday detection
- Back-test 3 months of BB Breakout data
- Per-asset performance tracking

### Phase 4 — Scale Up
- Retire underperforming assets
- Scale winners
- EA posts live account data to Command Centre

### Phase 5 — Live Account (After 30-Day Proof)
- Review 30-day demo results
- If profitable → live account $1,000
- Scale up as system proves itself

---

## 💭 Trading Philosophy

> "I don't need millions — I just don't want to lose my money"
> — Lukas Ferreira, Pretoria 🇿🇦

- 2% rule protects account at all times
- System discipline beats emotions
- Demo first — prove it before real money
- Small consistent profits compound faster than big wins

---

## ☀️ Daily Routine

```
07:00 SAST — GitHub Actions fires automatically
07:05 SAST — Email report arrives
07:10 SAST — Open Command Centre
Any time   — BB signal fires on TradingView
             EA places trade automatically
             Email alert arrives
End of day — Check results
```

---

## 👤 Developer

**Lukas Ferreira** — Pretoria, South Africa 🇿🇦
Built with **Claude AI** (Anthropic) — May 2026
*From zero to fully automated AI trading in 3 days*

---

*For educational purposes only. Not financial advice.*
*Claude-Market v3.1 — EA v7.6 — All systems operational*
*30-Day Challenge: 05 May 2026 → 04 June 2026* 🎯
