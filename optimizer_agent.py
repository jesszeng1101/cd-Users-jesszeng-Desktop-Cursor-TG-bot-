"""
optimizer_agent.py — Weekly GPT-4o deep review of bot parameters.

This agent runs every Sunday at midnight SGT. It:
1. Reads the alert history log (alert_history.json)
2. Reads current thresholds from environment / config
3. Asks GPT-4o to analyse which alerts were useful, which were noise,
   and suggest concrete threshold improvements
4. Saves the recommendations to optimizer_report.json
5. Sends the report to Telegram so you can decide what to apply

To apply a recommendation, update your .env (local) or Railway env vars.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_BASE_DIR = Path(__file__).parent
ALERT_HISTORY_FILE = _BASE_DIR / "alert_history.json"
OPTIMIZER_REPORT_FILE = _BASE_DIR / "optimizer_report.json"

UTC = timezone.utc
SGT = timezone(timedelta(hours=8))

MAX_HISTORY_ENTRIES = 500   # cap to keep prompts manageable


# ─── Alert history helpers ──────────────────────────────────────────────────

def load_alert_history() -> list[dict[str, Any]]:
    if not ALERT_HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(ALERT_HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_alert_history(history: list[dict[str, Any]]) -> None:
    # Keep only the last MAX_HISTORY_ENTRIES entries
    trimmed = history[-MAX_HISTORY_ENTRIES:]
    ALERT_HISTORY_FILE.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")


def record_alert(module: str, trigger: str, symbol: str, details: str) -> None:
    """Call this from bot.py each time an alert fires to build history."""
    history = load_alert_history()
    history.append({
        "ts": datetime.now(UTC).isoformat(),
        "module": module,
        "trigger": trigger,
        "symbol": symbol,
        "details": details,
    })
    save_alert_history(history)


# ─── Current config snapshot ────────────────────────────────────────────────

def current_config_snapshot() -> dict[str, Any]:
    return {
        "PRICE_MOVE_PCT": os.getenv("PRICE_MOVE_PCT", "2.0"),
        "RSI_HIGH": os.getenv("RSI_HIGH", "72"),
        "RSI_LOW": os.getenv("RSI_LOW", "28"),
        "VOLUME_SPIKE_X": os.getenv("VOLUME_SPIKE_X", "1.8"),
        "ALERT_COOLDOWN_MIN": os.getenv("ALERT_COOLDOWN_MIN", "60"),
        "DEX_MIN_LIQ": os.getenv("DEX_MIN_LIQ", "50000"),
        "DEX_WHALE_USD": os.getenv("DEX_WHALE_USD", "100000"),
        "DEX_RUG_PCT": os.getenv("DEX_RUG_PCT", "40"),
        "FEAR_LOW": os.getenv("FEAR_LOW", "25"),
        "GREED_HIGH": os.getenv("GREED_HIGH", "75"),
    }


# ─── GPT-4o analysis ────────────────────────────────────────────────────────

def run_optimizer(openai_api_key: str) -> dict[str, Any]:
    """
    Calls GPT-4o to review the last week of alerts and recommend
    threshold changes. Returns a dict with the recommendations.
    """
    client = OpenAI(api_key=openai_api_key)
    history = load_alert_history()
    config = current_config_snapshot()

    # Summarise history to reduce tokens
    cutoff = datetime.now(UTC) - timedelta(days=7)
    recent = [
        h for h in history
        if datetime.fromisoformat(h["ts"].replace("Z", "+00:00")) >= cutoff
    ]

    # Count alerts by type
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

    prompt = f"""You are a quantitative trading system analyst reviewing a Telegram alert bot used by a beginner crypto trader.

CURRENT THRESHOLDS:
{json.dumps(config, indent=2)}

ALERT HISTORY (last 7 days):
{history_summary}

Your job is to analyse whether the current thresholds are optimal and suggest improvements.

Rules:
- The trader is a beginner with $1k-$10k budget who wants QUALITY over QUANTITY alerts
- Too many alerts = noise fatigue; too few = missed opportunities
- Ideal: 3-8 meaningful alerts per day total
- R:R must be >= 1:2 before recommending a trade alert

Please respond in EXACTLY this JSON format (no markdown, raw JSON only):
{{
  "assessment": "2-3 sentence summary of what the alert patterns show",
  "alert_quality": "Good / Noisy / Too Few",
  "recommendations": [
    {{
      "parameter": "PARAM_NAME",
      "current_value": "X",
      "suggested_value": "Y",
      "reason": "1 sentence why"
    }}
  ],
  "priority_focus": "Which asset/module should the trader focus on this week and why (1 sentence)",
  "learning_insight": "One trading mechanic insight the alert patterns reveal (1 sentence)"
}}

If no changes needed, return empty recommendations array.
"""

    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800,
        temperature=0.3,
    )
    raw = (resp.choices[0].message.content or "").strip()

    # Strip markdown code fences if present
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
    return result


def format_report_for_telegram(report: dict[str, Any]) -> str:
    lines = [
        "🤖 WEEKLY OPTIMIZER REPORT",
        f"Generated: {report.get('generated_at', 'N/A')}",
        f"Alerts analysed: {report.get('alerts_analysed', 0)} (last 7 days)",
        "",
        f"📊 QUALITY: {report.get('alert_quality', 'N/A')}",
        f"Assessment: {report.get('assessment', 'N/A')}",
        "",
    ]
    recs = report.get("recommendations", [])
    if recs:
        lines.append("🔧 RECOMMENDED CHANGES:")
        for r in recs:
            lines.append(
                f"  {r.get('parameter')}: {r.get('current_value')} → {r.get('suggested_value')}"
            )
            lines.append(f"  Why: {r.get('reason')}")
        lines.append("")
        lines.append("To apply: update your Railway env vars with the above values.")
    else:
        lines.append("✅ No threshold changes needed this week.")

    lines.append("")
    lines.append(f"🎯 Focus: {report.get('priority_focus', 'N/A')}")
    lines.append(f"💡 Insight: {report.get('learning_insight', 'N/A')}")
    lines.append("")
    lines.append("⚠️ These are suggestions only — review before applying.")
    return "\n".join(lines)


# ─── Background task (integrated into bot.py) ───────────────────────────────

async def weekly_optimizer_task(app: Any) -> None:
    """
    Background asyncio task. Sleeps until Sunday midnight SGT then runs.
    app must have bot_data["ai_manager"] and bot_data["state"].
    """
    import telegram

    while True:
        now_sgt = datetime.now(SGT)
        # Next Sunday at 00:05 SGT
        days_until_sunday = (6 - now_sgt.weekday()) % 7  # 6 = Sunday
        if days_until_sunday == 0 and now_sgt.hour >= 0 and now_sgt.minute >= 5:
            days_until_sunday = 7  # Already past this Sunday's window
        next_run = (now_sgt + timedelta(days=days_until_sunday)).replace(
            hour=0, minute=5, second=0, microsecond=0
        )
        sleep_secs = max(60, int((next_run - now_sgt).total_seconds()))
        await asyncio.sleep(sleep_secs)

        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not openai_key or not chat_id:
            continue

        try:
            # Run the GPT-4o analysis in a thread (sync OpenAI client)
            report = await asyncio.to_thread(run_optimizer, openai_key)

            # Persist report
            OPTIMIZER_REPORT_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")

            # Send to Telegram
            text = format_report_for_telegram(report)
            await app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            print(f"[optimizer_agent] weekly run failed: {exc}", flush=True)
