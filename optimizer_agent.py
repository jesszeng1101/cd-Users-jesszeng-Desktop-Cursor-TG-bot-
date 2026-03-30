"""
optimizer_agent.py — Weekly GPT-4o deep review of bot parameters.

Flow:
1. Every Sunday midnight SGT: analyse last 7 days of alert history
2. Write pending recommendations to config_overrides.json
3. Send Telegram message with numbered list: "/approve 1" or "/reject 1"
4. On approval → value takes effect IMMEDIATELY (hot-reload, no restart needed)
5. On rejection → dismissed and logged

Config priority order (highest → lowest):
  config_overrides.json (active approved overrides)
  > environment variables (.env / Railway)
  > hardcoded defaults
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_BASE_DIR = Path(__file__).parent
ALERT_HISTORY_FILE    = _BASE_DIR / "alert_history.json"
OPTIMIZER_REPORT_FILE = _BASE_DIR / "optimizer_report.json"
CONFIG_OVERRIDES_FILE = _BASE_DIR / "config_overrides.json"

UTC = timezone.utc
SGT = timezone(timedelta(hours=8))

MAX_HISTORY_ENTRIES = 500

# Parameters the agent is allowed to tune
TUNABLE_PARAMS = {
    "PRICE_MOVE_PCT":    {"type": float, "min": 0.5,   "max": 10.0,    "default": "2.0"},
    "RSI_HIGH":          {"type": float, "min": 60.0,  "max": 85.0,    "default": "72"},
    "RSI_LOW":           {"type": float, "min": 15.0,  "max": 40.0,    "default": "28"},
    "VOLUME_SPIKE_X":    {"type": float, "min": 1.2,   "max": 5.0,     "default": "1.8"},
    "ALERT_COOLDOWN_MIN":{"type": int,   "min": 15,    "max": 240,     "default": "60"},
    "DEX_MIN_LIQ":       {"type": float, "min": 10000, "max": 500000,  "default": "50000"},
    "DEX_WHALE_USD":     {"type": float, "min": 25000, "max": 1000000, "default": "100000"},
    "DEX_RUG_PCT":       {"type": float, "min": 20.0,  "max": 80.0,    "default": "40"},
    "FEAR_LOW":          {"type": int,   "min": 10,    "max": 35,      "default": "25"},
    "GREED_HIGH":        {"type": int,   "min": 65,    "max": 90,      "default": "75"},
}


# ─── Config overrides file ───────────────────────────────────────────────────

def _load_overrides() -> dict[str, Any]:
    if not CONFIG_OVERRIDES_FILE.exists():
        return {"active": {}, "pending": [], "history": []}
    try:
        data = json.loads(CONFIG_OVERRIDES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"active": {}, "pending": [], "history": []}
        data.setdefault("active", {})
        data.setdefault("pending", [])
        data.setdefault("history", [])
        return data
    except Exception:
        return {"active": {}, "pending": [], "history": []}


def _save_overrides(data: dict[str, Any]) -> None:
    CONFIG_OVERRIDES_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_config_value(param: str) -> str:
    """Read a config param — active override > env var > default."""
    overrides = _load_overrides()
    if param in overrides["active"]:
        return str(overrides["active"][param])
    env_val = os.getenv(param)
    if env_val:
        return env_val
    return TUNABLE_PARAMS.get(param, {}).get("default", "")


def get_all_active_config() -> dict[str, str]:
    """Return the full effective config (overrides merged over env vars)."""
    result = {}
    for param, meta in TUNABLE_PARAMS.items():
        result[param] = get_config_value(param)
    return result


def get_pending_recommendations() -> list[dict[str, Any]]:
    return _load_overrides().get("pending", [])


def approve_recommendation(index: int) -> Optional[dict[str, Any]]:
    """Approve pending rec by 1-based index. Returns the rec or None."""
    overrides = _load_overrides()
    pending = overrides.get("pending", [])
    if index < 1 or index > len(pending):
        return None
    rec = pending.pop(index - 1)
    param = rec["parameter"]
    meta = TUNABLE_PARAMS.get(param, {})
    # Validate value is within safe bounds
    try:
        val = meta["type"](rec["suggested_value"])
        val = max(meta["min"], min(meta["max"], val))
        rec["suggested_value"] = str(val)
    except Exception:
        pass
    overrides["active"][param] = rec["suggested_value"]
    rec["approved_at"] = datetime.now(SGT).isoformat()
    rec["status"] = "approved"
    overrides["history"].append(rec)
    overrides["history"] = overrides["history"][-50:]
    _save_overrides(overrides)
    return rec


def reject_recommendation(index: int) -> Optional[dict[str, Any]]:
    """Reject pending rec by 1-based index. Returns the rec or None."""
    overrides = _load_overrides()
    pending = overrides.get("pending", [])
    if index < 1 or index > len(pending):
        return None
    rec = pending.pop(index - 1)
    rec["rejected_at"] = datetime.now(SGT).isoformat()
    rec["status"] = "rejected"
    overrides["history"].append(rec)
    overrides["history"] = overrides["history"][-50:]
    _save_overrides(overrides)
    return rec


def reset_override(param: str) -> bool:
    """Remove an active override — reverts to env var / default."""
    overrides = _load_overrides()
    if param in overrides["active"]:
        del overrides["active"][param]
        _save_overrides(overrides)
        return True
    return False


# ─── Alert history helpers ───────────────────────────────────────────────────

def load_alert_history() -> list[dict[str, Any]]:
    if not ALERT_HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(ALERT_HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_alert_history(history: list[dict[str, Any]]) -> None:
    trimmed = history[-MAX_HISTORY_ENTRIES:]
    ALERT_HISTORY_FILE.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")


def record_alert(module: str, trigger: str, symbol: str, details: str) -> None:
    """Called from bot.py on every alert to build history for the optimizer."""
    history = load_alert_history()
    history.append({
        "ts": datetime.now(UTC).isoformat(),
        "module": module,
        "trigger": trigger,
        "symbol": symbol,
        "details": details,
    })
    save_alert_history(history)


# ─── GPT-4o analysis ─────────────────────────────────────────────────────────

def run_optimizer(openai_api_key: str) -> dict[str, Any]:
    """Call GPT-4o to review alert history and recommend threshold changes."""
    client = OpenAI(api_key=openai_api_key)
    history = load_alert_history()
    config = get_all_active_config()

    cutoff = datetime.now(UTC) - timedelta(days=7)
    recent = [
        h for h in history
        if datetime.fromisoformat(h["ts"].replace("Z", "+00:00")) >= cutoff
    ]

    by_module: dict[str, int] = {}
    by_trigger: dict[str, int] = {}
    for h in recent:
        by_module[h["module"]] = by_module.get(h["module"], 0) + 1
        key = f"{h['module']}:{h['trigger']}"
        by_trigger[key] = by_trigger.get(key, 0) + 1

    history_summary = json.dumps({
        "total_alerts_last_7d": len(recent),
        "by_module": by_module,
        "by_trigger": by_trigger,
        "sample_last_10": recent[-10:],
    }, indent=2)

    param_bounds = {k: {"min": v["min"], "max": v["max"]} for k, v in TUNABLE_PARAMS.items()}

    prompt = f"""You are a quantitative trading system analyst reviewing a Telegram alert bot for a beginner crypto trader.

CURRENT EFFECTIVE THRESHOLDS:
{json.dumps(config, indent=2)}

PARAMETER BOUNDS (you must stay within these):
{json.dumps(param_bounds, indent=2)}

ALERT HISTORY (last 7 days):
{history_summary}

Goal: the trader wants QUALITY over QUANTITY — 3 to 8 meaningful alerts per day.
Too many = noise fatigue. Too few = missed opportunities.

Respond in EXACTLY this JSON format (raw JSON, no markdown):
{{
  "assessment": "2-3 sentences on what the alert patterns show",
  "alert_quality": "Good | Noisy | Too Few",
  "recommendations": [
    {{
      "parameter": "EXACT_PARAM_NAME",
      "current_value": "X",
      "suggested_value": "Y",
      "reason": "1 sentence — what problem this fixes and what outcome to expect"
    }}
  ],
  "priority_focus": "1 sentence — which asset/module to prioritise this week and why",
  "learning_insight": "1 sentence — one trading mechanic the alert patterns reveal"
}}

Only include recommendations where you are confident the change will meaningfully improve quality.
If nothing needs changing, return empty array for recommendations.
"""

    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=900,
        temperature=0.3,
    )
    raw = (resp.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        result = json.loads(raw)
    except Exception:
        result = {"raw_response": raw, "parse_error": True}

    result["generated_at"] = datetime.now(SGT).isoformat()
    result["config_snapshot"] = config
    result["alerts_analysed"] = len(recent)

    # Write recommendations as pending
    recs = result.get("recommendations", [])
    if isinstance(recs, list) and recs:
        overrides = _load_overrides()
        # Clear old pending before adding new batch
        overrides["pending"] = []
        for r in recs:
            if r.get("parameter") in TUNABLE_PARAMS:
                overrides["pending"].append({
                    "parameter": r["parameter"],
                    "current_value": r.get("current_value", ""),
                    "suggested_value": r.get("suggested_value", ""),
                    "reason": r.get("reason", ""),
                    "proposed_at": datetime.now(SGT).isoformat(),
                })
        _save_overrides(overrides)

    OPTIMIZER_REPORT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def format_report_for_telegram(report: dict[str, Any]) -> str:
    lines = [
        "🤖 WEEKLY OPTIMIZER REPORT",
        f"Alerts analysed: {report.get('alerts_analysed', 0)} (last 7 days)",
        f"Quality: {report.get('alert_quality', 'N/A')}",
        "",
        f"{report.get('assessment', '')}",
        "",
    ]
    recs = report.get("recommendations", [])
    if recs:
        lines.append("🔧 RECOMMENDED CHANGES")
        lines.append("Reply /approve N or /reject N for each:")
        lines.append("")
        for i, r in enumerate(recs, 1):
            lines.append(
                f"[{i}] {r.get('parameter')}: {r.get('current_value')} → {r.get('suggested_value')}"
            )
            lines.append(f"     {r.get('reason', '')}")
        lines.append("")
        lines.append("Changes take effect instantly — no restart needed.")
    else:
        lines.append("✅ No threshold changes needed this week.")

    lines.extend([
        "",
        f"🎯 Focus this week: {report.get('priority_focus', 'N/A')}",
        f"💡 Insight: {report.get('learning_insight', 'N/A')}",
        "",
        "⚠️ Suggestions only — you decide what to apply.",
    ])
    return "\n".join(lines)


# ─── Background task ─────────────────────────────────────────────────────────

async def weekly_optimizer_task(app: Any) -> None:
    """Fires every Sunday at 00:05 SGT. Wired into bot.py post_init."""
    while True:
        now_sgt = datetime.now(SGT)
        days_until_sunday = (6 - now_sgt.weekday()) % 7
        if days_until_sunday == 0 and (now_sgt.hour > 0 or now_sgt.minute >= 5):
            days_until_sunday = 7
        next_run = (now_sgt + timedelta(days=days_until_sunday)).replace(
            hour=0, minute=5, second=0, microsecond=0
        )
        await asyncio.sleep(max(60, int((next_run - now_sgt).total_seconds())))

        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not openai_key or not chat_id:
            continue
        try:
            report = await asyncio.to_thread(run_optimizer, openai_key)
            text = format_report_for_telegram(report)
            await app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            print(f"[optimizer_agent] weekly run failed: {exc}", flush=True)
