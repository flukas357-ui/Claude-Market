# Claude-Market — Smart Gate Parameter History

> This document tracks every parameter set we have tried.
> When a set doesn't work — we note why and move to the next.
> We never lose track of what was tried and what the outcome was.

---

## How the Smart Gate System Works

Before every trade the EA asks 5 questions:

| Gate | Question | Blocks trade if... |
|------|----------|-------------------|
| 1 — Quality | Is signal score ≥ MinScore? | Score too weak |
| 2 — Cooldown | Has enough time passed since last close on this symbol? | Too soon after last trade |
| 3 — Heat | Is total open risk below HeatLimit? | Account overexposed |
| 4 — Type Match | Does signal type match market condition? | Mismatch without 5/5 score |
| 5 — Direction | Does direction make sense after last close? | Chasing a big move without 5/5 |

**Reversal Scalp Mode** activates automatically when:
- Last close profit ≥ BigClosePct% of balance
- New signal direction is OPPOSITE to last close
- All 5 gates pass
- SL is tighter (ReversalSLPct% of normal)

---

## Parameter Sets

---

### ✅ SET v1 — CURRENT (started: 8 May 2026)

| Parameter | Value | Reason |
|-----------|-------|--------|
| BigClosePct | 0.5% | Triggers reversal mode after 0.5% profit close |
| CooldownMins | 90 | 90 minutes between trades on same symbol |
| HeatLimitPct | 6.0% | Max total open risk = 6% of balance |
| MinScore | 4 | Minimum signal score = 4/5 |
| ReversalSLPct | 40% | Reversal scalp SL = 40% of normal SL |
| TPRatio | 1.5 | Take profit = 1.5× SL distance |

**Status:** 🟡 TESTING

**Started:** 8 May 2026

**Results:**
- Date: --
- Trades taken: --
- Trades blocked: --
- Win rate: --
- P&L: --
- Notes: --

**Decision:** Pending — review after 5 trading days

---

### ⬜ SET v2 — NEXT (use if v1 blocks too many trades)

| Parameter | Value | Change from v1 |
|-----------|-------|---------------|
| BigClosePct | 0.3% | Lower threshold — triggers reversal mode more often |
| CooldownMins | 60 | Shorter cooldown — allows more trades per day |
| HeatLimitPct | 6.0% | Same |
| MinScore | 4 | Same |
| ReversalSLPct | 40% | Same |
| TPRatio | 1.5 | Same |

**Status:** ⬜ NOT YET TRIED

**Use when:** v1 is blocking too many good setups due to cooldown

---

### ⬜ SET v3 — CONSERVATIVE (use if v1 is too aggressive)

| Parameter | Value | Change from v1 |
|-----------|-------|---------------|
| BigClosePct | 1.0% | Higher threshold — reversal mode only on very big wins |
| CooldownMins | 120 | Longer cooldown — more time between trades |
| HeatLimitPct | 4.0% | Lower heat limit — more conservative |
| MinScore | 5 | Only 5/5 signals — maximum quality filter |
| ReversalSLPct | 40% | Same |
| TPRatio | 1.5 | Same |

**Status:** ⬜ NOT YET TRIED

**Use when:** v1 opens too many trades and losses are stacking up

---

### ⬜ SET v4 — AGGRESSIVE (use if markets are trending strongly)

| Parameter | Value | Change from v1 |
|-----------|-------|---------------|
| BigClosePct | 0.3% | Lower — reversal mode triggers easily |
| CooldownMins | 45 | Short cooldown — more active trading |
| HeatLimitPct | 8.0% | Higher heat — allows more simultaneous trades |
| MinScore | 3 | Allows score 3/5 — more signals pass |
| ReversalSLPct | 35% | Even tighter reversal SL |
| TPRatio | 2.0 | Bigger TP target for trending markets |

**Status:** ⬜ NOT YET TRIED

**Use when:** Markets are trending strongly and we are missing moves

---

## How to Switch Parameter Sets

1. In MetaEditor — open `ClaudMarket_EA.mq5`
2. Find the input section at the top
3. Change the values to match the new set
4. Compile (F7) → reattach to all charts
5. Update this document with start date and results

---

## Rules for Switching

- **Minimum test period:** 5 trading days before switching
- **Switch reason must be documented** in this file
- **Never switch during a live trade** — wait for all positions to close
- **Always record results** before switching — even if just "not enough data"

---

## Silver Example — Why This System Exists

**Old system (EA v8.1):**
```
Silver closes at +$311 profit (12:04)
System had max 2 trades per symbol rule
Silver was blocked for rest of day
New perfect setup at 14:00 → MISSED ❌
```

**New system (EA v8.2):**
```
Silver closes at +$311 profit (12:04)
Gate 2 Cooldown: 90 min wait starts
At 14:04 → Gates re-checked:
  Gate 1: Score 5/5 ✅
  Gate 2: 120 min passed ✅ (>90 min)
  Gate 3: Heat 3.2% < 6% ✅
  Gate 4: Range signal ✅
  Gate 5: Direction = SELL (reversal) ✅
→ REVERSAL SCALP MODE → TRADE PLACED ✅
```

The system no longer blocks by counting.
It blocks by context. 🧠

---

*Claude-Market | Lukas Ferreira — Pretoria 🇿🇦*
*"Small change → test → decide"*
