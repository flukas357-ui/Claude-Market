"""
WEBHOOK ADDITIONS — webhook_server_v7_2.py
Add these to your existing webhook_server.py

Changes:
  - mcapi_config dict: master config for all engines
  - /settings GET endpoint: Config Tab reads from here
  - /settings/update POST endpoint: Config Tab writes here
  - Scanner now reads engine4_personality FROM config
    instead of hardcoded personality dict
  - Version bump: 7.1 → 7.2
"""

# ─────────────────────────────────────────────────────────────────
# 1. REPLACE your existing PERSONALITY_CONFIG dict with this:
#    (add near top of file, after imports)
# ─────────────────────────────────────────────────────────────────

mcapi_config = {
    "scanner": {
        "interval_mins": 30,
        "min_score":     3,
        "cooldown_mins": 90,
        "secret":        "claude-market-2026",
    },
    "engine1_regime": {
        "ema_fast":      50,
        "ema_slow":      200,
        "adx_threshold": 25,
        "timeframe":     "H1",
    },
    "engine2_session": {
        "us_indices":  {"open": "14:30", "close": "21:00"},
        "eu_indices":  {"open": "07:00", "close": "15:30"},
        "forex_metals":{"open": "00:00", "close": "22:00"},
    },
    "engine4_personality": {
        "GOLD": {
            "priority": 2, "min_score": 3, "max_daily": 3,
            "sl_pips": 150, "tp_ratio": 1.5, "trail_pct": 20,
            "location_zone": 0.33, "blacklist": False,
        },
        "SILVER": {
            "priority": 2, "min_score": 3, "max_daily": 3,
            "sl_pips": 200, "tp_ratio": 1.5, "trail_pct": 20,
            "location_zone": 0.33, "blacklist": False,
        },
        "BTCUSD": {
            "priority": 2, "min_score": 4, "max_daily": 1,
            "sl_pips": 500, "tp_ratio": 2.0, "trail_pct": 30,
            "location_zone": 0.33, "blacklist": False,
        },
        "ETHUSD": {
            "priority": 2, "min_score": 3, "max_daily": 2,
            "sl_pips": 300, "tp_ratio": 1.8, "trail_pct": 25,
            "location_zone": 0.33, "blacklist": False,
        },
        "US_TECH100": {
            "priority": 2, "min_score": 3, "max_daily": 2,
            "sl_pips": 300, "tp_ratio": 1.5, "trail_pct": 20,
            "location_zone": 0.33, "blacklist": False,
        },
        "USDZAR": {
            "priority": 3, "min_score": 3, "max_daily": 4,
            "sl_pips": 200, "tp_ratio": 1.5, "trail_pct": 20,
            "location_zone": 0.33, "blacklist": False,
        },
        "AUDUSD": {
            "priority": 0, "min_score": 5, "max_daily": 0,
            "sl_pips": 100, "tp_ratio": 1.5, "trail_pct": 20,
            "location_zone": 0.33, "blacklist": True,
            "blacklist_reason": "Sprint 0: 0/3 wins, -$526 total loss",
        },
    },
    "engine6_structure": {
        "bb_period":    20,
        "bb_deviation": 2.0,
        "buy_zone":     0.33,
        "sell_zone":    0.33,
    },
    "engine7_risk": {
        "risk_pct":           2.0,
        "max_heat_pct":       8.0,
        "daily_loss_limit":   5.0,
        "tp_ratio":           1.5,
        "trail_activate_pct": 20.0,
    },
}


# ─────────────────────────────────────────────────────────────────
# 2. ADD these two endpoints (paste near /personality/update)
# ─────────────────────────────────────────────────────────────────

@app.route("/settings", methods=["GET"])
def get_settings():
    """Config Tab: read all engine settings"""
    return jsonify({
        **mcapi_config,
        "version":   "7.2",
        "timestamp": datetime.utcnow().isoformat(),
    })


@app.route("/settings/update", methods=["POST"])
def update_settings():
    """Config Tab: write engine settings (live, no redeploy)"""
    data = request.get_json(force=True)

    # Auth check
    incoming_secret = data.get("secret") or data.get("config", {}).get("scanner", {}).get("secret", "")
    if incoming_secret != mcapi_config["scanner"]["secret"]:
        return jsonify({"error": "Unauthorized"}), 403

    cfg = data.get("config", {})

    # Merge top-level sections
    for section in ["scanner", "engine1_regime", "engine2_session",
                    "engine6_structure", "engine7_risk"]:
        if section in cfg:
            mcapi_config[section].update(cfg[section])

    # Merge per-symbol personality (preserve blacklist_reason)
    if "engine4_personality" in cfg:
        for sym, vals in cfg["engine4_personality"].items():
            if sym in mcapi_config["engine4_personality"]:
                existing = mcapi_config["engine4_personality"][sym]
                reason = existing.get("blacklist_reason", "")
                existing.update(vals)
                if reason and not existing.get("blacklist_reason"):
                    existing["blacklist_reason"] = reason
            else:
                mcapi_config["engine4_personality"][sym] = vals

    print(f"[CONFIG] Settings updated via Config Tab at {datetime.utcnow().isoformat()}")

    return jsonify({
        "status":    "ok",
        "message":   "Config updated — active immediately, no redeploy needed",
        "timestamp": datetime.utcnow().isoformat(),
    })


# ─────────────────────────────────────────────────────────────────
# 3. UPDATE _get_personality() to read from mcapi_config
#    Replace your existing function with this:
# ─────────────────────────────────────────────────────────────────

def _get_personality(symbol):
    """Engine 4: get per-symbol config from mcapi_config (live-editable)"""
    sym = symbol.upper()
    p   = mcapi_config["engine4_personality"]
    return p.get(sym, p.get("GOLD", {}))  # fallback to GOLD defaults


# ─────────────────────────────────────────────────────────────────
# 4. UPDATE _check_structure() to read buy_zone from mcapi_config
#    Change this line inside _check_structure():
#
#    BEFORE:  zone = p.get("location_zone", 0.33)
#    AFTER:   zone = p.get("location_zone",
#                     mcapi_config["engine6_structure"].get("buy_zone", 0.33))
#
# 5. BUMP VERSION in root endpoint:
#    "version": "7.2"
# ─────────────────────────────────────────────────────────────────
