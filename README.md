# 📈 TG Trading Bot

A personal Telegram bot for learning crypto trading — combining real-time alerts, AI-powered analysis, and daily market digests. Built for beginners who want to understand **why** markets move, not just react to price.

---

## What It Does

### 🌅 Daily Digests (8 AM + 8 PM SGT)
Two briefings per day powered by GPT:
- **Morning** — market regime snapshot, open setups review, top entry opportunity with entry zone / target / stop / R:R / position size
- **Evening** — market close review, what to watch overnight, setup updates

### 🔴 Smart News Alerts
Monitors CoinDesk + CoinTelegraph every 5 minutes. When a high-impact headline hits (Fed, ETF, SEC, hack, listing, etc.), GPT instantly explains:
- What actually happened (plain English)
- Bullish / Bearish / Neutral for crypto — and why
- Which asset will react most
- Whether to act now or wait for confirmation

### 📊 Price Alerts
Live Binance WebSocket on 15-min candles:
- Price moves ≥ 2% in 15 minutes
- RSI overbought (>72) or oversold (<28) on 1h
- Volume spike ≥ 1.8× 7-day average
- Manual price level crosses (BTC/ETH)

### 🤖 AI Chat (GPT-4o-mini)
Ask anything directly in chat or use commands:

| Command | What it does |
|---|---|
| `/ask {question}` | Free-form market question |
| `/analyse {symbol}` | Full technical + narrative analysis with trade setup |
| `/scenario {situation}` | What-if macro scenario thinking |
| `/learn {topic}` | Explain a trading concept for beginners |
| `/review` | Market regime + what to watch today |
| `/levels` | Key support/resistance levels |
| `/status` | Live prices across crypto + stocks + commodities |

### 💼 Portfolio Tracking

| Command | What it does |
|---|---|
| `/addposition BTC 0.01 65000` | Log a trade entry |
| `/removeposition BTC` | Remove a position |
| `/portfolio` | P&L snapshot with unrealised gains/losses |
| `/setbudget 5000` | Set your total budget |
| `/addsetup BTC LONG 95000 105000 90000` | Add a trade setup (entry / target / stop) |

### 🔧 Weekly Optimizer (GPT-4o)
Every Sunday midnight SGT, a GPT-4o agent reviews the past week of alerts and sends a Telegram report with:
- Alert quality score (Noisy / Good / Too Few)
- Specific threshold change recommendations (e.g. `RSI_HIGH: 72 → 68`)
- Which asset to focus on this week
- One trading insight from the week's patterns

---

## Architecture

```
bot.py                  — Main entrypoint, all AI logic, digests, commands
bot_impl.py             — Alert modules (price, news, DEX, sentiment)
optimizer_agent.py      — Weekly GPT-4o parameter review agent
src/
  config/constants.py   — Thresholds and limits
  storage/json_store.py — JSON persistence helpers
memory.json             — Conversation history, open setups (gitignored)
portfolio.json          — Positions, cash, budget (gitignored)
alert_history.json      — Alert log for optimizer (gitignored)
```

---

## Data Sources

| Source | Used for |
|---|---|
| Binance API + WebSocket | Live crypto prices, 15m candles, RSI, volume |
| yfinance | SPY, QQQ, VIX, Gold, Oil, DXY |
| CoinDesk + CoinTelegraph RSS | News alerts |
| DexScreener | New token listings, whale flows, rug detection |
| Fear & Greed Index API | Sentiment monitoring |
| CoinGecko | Fallback price lookups for any coin |
| GoPlusLabs | Smart contract security checks |

---

## Setup (Local)

```bash
# 1. Clone
git clone https://github.com/jesszeng1101/cd-Users-jesszeng-Desktop-Cursor-TG-bot-.git
cd cd-Users-jesszeng-Desktop-Cursor-TG-bot-

# 2. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env with your keys

# 5. Run
python bot.py
```

## Environment Variables

```env
TELEGRAM_BOT_TOKEN=       # From @BotFather
TELEGRAM_CHAT_ID=         # Your Telegram chat ID
OPENAI_API_KEY=           # From platform.openai.com

# Alert thresholds (tunable)
TRACKED_PAIRS=BTCUSDT,ETHUSDT,SOLUSDT
PRICE_MOVE_PCT=2.0        # % move to trigger alert
RSI_HIGH=72               # Overbought threshold
RSI_LOW=28                # Oversold threshold
VOLUME_SPIKE_X=1.8        # Volume multiplier
ALERT_COOLDOWN_MIN=60     # Minutes between same alert
DEX_MIN_LIQ=50000         # Min liquidity for DEX alerts ($)
DEX_WHALE_USD=100000      # Min whale flow size ($)
DEX_RUG_PCT=40            # Liquidity drop % for rug alert
FEAR_LOW=25               # Extreme fear threshold
GREED_HIGH=75             # Extreme greed threshold
```

---

## Deployment (Railway)

This repo includes a `Dockerfile` and `railway.toml` for one-click Railway deployment.

1. Connect this repo to [Railway](https://railway.app)
2. Add all environment variables from the table above
3. Railway auto-builds and runs 24/7

---

## Design Principles

- **Narrative first** — every alert explains *why*, not just *what*
- **Beginner-friendly** — no jargon without explanation
- **R:R ≥ 1:2** — only recommend trades with good risk/reward
- **$50–$100 position sizes** — sized for a $1k–$10k learning budget
- **No leverage, no futures** — spot only

---

*⚠️ Not financial advice. This bot is a learning tool.*
