# Bot Architecture

This project is a Telegram trading assistant for a beginner profile. It combines:

- Alerting modules (price/news/sentiment/DEX and portfolio checks)
- AI assistant commands and free-chat responses
- Lightweight persistent memory and portfolio tracking
- Scheduled tasks (startup health ping, daily briefing/context, periodic checks)

## Structure

- `bot.py`  
  Runtime entrypoint used by scripts (`run.sh`, `stop.sh`, `logs.sh`). Keeps backward compatibility.

- `bot_impl.py`  
  Core monitoring/alert runtime (price/news/DEX/sentiment and scheduled briefing tasks).

- `app_constants.py`  
  Backward-compatible constants module used by existing runtime.

- `src/config/constants.py`  
  Central constants source for intervals, thresholds, and AI limits.

- `src/storage/json_store.py`  
  Shared JSON persistence helpers for local state files.

- `memory.json`  
  Persistent user profile + conversation memory + open setups.

- `portfolio.json`  
  Persistent portfolio positions and budget/cash state.

## What the bot should do

- Stream and monitor selected assets continuously
- Send actionable, low-noise alerts with confidence framing
- Provide on-demand AI analysis in plain language for beginner users
- Keep context over time (memory + portfolio + recent market state)
- Operate safely in the background via scripts with log visibility
