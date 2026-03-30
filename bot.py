import asyncio
import html
import json
import logging
import os
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import feedparser
import httpx
import websockets
import yfinance as yf
from openai import OpenAI
from src.config.constants import AI_DAILY_CALL_LIMIT, AI_MAX_INPUT_CHARS, AI_MAX_TOKENS
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
log = logging.getLogger(__name__)


def log_err(msg: str) -> None:
    print(f"[{now_utc().isoformat()}] {msg}", file=sys.stderr, flush=True)


def parse_float(value: str, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def parse_int(value: str, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def time_ago(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    delta = now_utc() - dt.astimezone(UTC)
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


class TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, datetime] = {}

    def add(self, key: str, ttl_seconds: int) -> None:
        self._store[key] = now_utc() + timedelta(seconds=ttl_seconds)

    def contains(self, key: str) -> bool:
        exp = self._store.get(key)
        if exp is None:
            return False
        if exp < now_utc():
            self._store.pop(key, None)
            return False
        return True

    def cleanup(self) -> None:
        current = now_utc()
        expired = [k for k, v in self._store.items() if v < current]
        for k in expired:
            self._store.pop(k, None)


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    tracked_pairs: list[str]
    btc_level: Optional[float]
    eth_level: Optional[float]
    news_vote_threshold: int
    price_move_pct: float
    rsi_high: float
    rsi_low: float
    volume_spike_x: float
    alert_cooldown_min: int
    dex_min_liq: float
    dex_whale_usd: float
    dex_rug_pct: float
    fear_low: int
    greed_high: int

    @staticmethod
    def from_env() -> "Config":
        load_dotenv()
        pairs = [
            p.strip().upper()
            for p in os.getenv("TRACKED_PAIRS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",")
            if p.strip()
        ]
        cfg = Config(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            tracked_pairs=pairs,
            btc_level=parse_float(os.getenv("BTC_LEVEL", "")),
            eth_level=parse_float(os.getenv("ETH_LEVEL", "")),
            news_vote_threshold=parse_int(os.getenv("NEWS_VOTE_THRESHOLD", "40"), 40) or 40,
            price_move_pct=parse_float(os.getenv("PRICE_MOVE_PCT", "2.0"), 2.0) or 2.0,
            rsi_high=parse_float(os.getenv("RSI_HIGH", "72"), 72.0) or 72.0,
            rsi_low=parse_float(os.getenv("RSI_LOW", "28"), 28.0) or 28.0,
            volume_spike_x=parse_float(os.getenv("VOLUME_SPIKE_X", "1.8"), 1.8) or 1.8,
            alert_cooldown_min=parse_int(os.getenv("ALERT_COOLDOWN_MIN", "60"), 60) or 60,
            dex_min_liq=parse_float(os.getenv("DEX_MIN_LIQ", "50000"), 50000.0) or 50000.0,
            dex_whale_usd=parse_float(os.getenv("DEX_WHALE_USD", "100000"), 100000.0) or 100000.0,
            dex_rug_pct=parse_float(os.getenv("DEX_RUG_PCT", "40"), 40.0) or 40.0,
            fear_low=parse_int(os.getenv("FEAR_LOW", "25"), 25) or 25,
            greed_high=parse_int(os.getenv("GREED_HIGH", "75"), 75) or 75,
        )
        if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
        return cfg


@dataclass
class BotState:
    config: Config
    paused_until: Optional[datetime] = None
    last_alert_time: dict[str, Optional[datetime]] = field(
        default_factory=lambda: {
            "news": None,
            "price": None,
            "dex": None,
            "sentiment": None,
        }
    )
    dedup_urls: TTLCache = field(default_factory=TTLCache)
    seen_pairs: TTLCache = field(default_factory=TTLCache)
    cooldowns: dict[str, datetime] = field(default_factory=dict)
    last_prices: dict[str, float] = field(default_factory=dict)
    level_state: dict[str, str] = field(default_factory=dict)
    volumes_15m: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=672)))
    dex_liquidity_history: dict[str, deque[tuple[datetime, float]]] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=120)))
    sentiment_crossed_today: dict[str, datetime.date] = field(default_factory=dict)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task] = field(default_factory=list)
    module_runner: Optional[asyncio.Task] = None

    def is_paused(self) -> bool:
        if self.paused_until is None:
            return False
        if now_utc() >= self.paused_until:
            self.paused_until = None
            return False
        return True


HIGH_IMPACT_KEYWORDS = [
    "SEC",
    "ETF",
    "hack",
    "exploit",
    "listing",
    "delisting",
    "Fed",
    "CPI",
    "BlackRock",
    "liquidation",
    "sanctions",
    "partnership",
    "ban",
]


def compute_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


async def send_alert(app: Application, state: BotState, module_name: str, text: str) -> None:
    if state.is_paused():
        return
    try:
        await app.bot.send_message(chat_id=state.config.telegram_chat_id, text=text)
        state.last_alert_time[module_name] = now_utc()
        # Record for weekly optimizer analysis (fire-and-forget, never crash alert)
        try:
            from optimizer_agent import record_alert
            # Extract first line as trigger summary
            trigger = text.split("\n")[0][:80]
            record_alert(module=module_name, trigger=trigger, symbol="", details=text[:200])
        except Exception:
            pass
    except Exception as exc:
        log_err(f"telegram send failed ({module_name}): {exc}")


def can_fire_cooldown(state: BotState, symbol: str, trigger: str) -> bool:
    key = f"{symbol}:{trigger}"
    until = state.cooldowns.get(key)
    if until and until > now_utc():
        return False
    state.cooldowns[key] = now_utc() + timedelta(minutes=state.config.alert_cooldown_min)
    return True


def already_seen(state: BotState, key: str, ttl_seconds: int = 21600) -> bool:
    if state.dedup_urls.contains(key):
        return True
    state.dedup_urls.add(key, ttl_seconds=ttl_seconds)
    return False


async def get_json(client: httpx.AsyncClient, url: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    response = await client.get(url, params=params, timeout=25.0)
    response.raise_for_status()
    return response.json()


async def _gpt_news_impact(app: Application, title: str, url: str) -> str:
    """Call GPT to explain a high-impact headline in beginner-friendly terms."""
    ai_manager: Optional[AiManager] = app.bot_data.get("ai_manager")
    if not ai_manager or not ai_manager.client:
        return ""
    ok, _ = ai_manager.can_call()
    if not ok:
        return ""
    ai_manager.mark_called()
    prompt = (
        f"Headline: {title}\n\n"
        "You are a macro analyst briefing a beginner crypto trader.\n"
        "In exactly 4 lines:\n"
        "📌 MEANING: [1 sentence — what actually happened in plain English]\n"
        "📈 PRICE IMPACT: [Bullish / Bearish / Neutral for crypto — and briefly WHY]\n"
        "🎯 WATCH: [Which asset(s) will react most — BTC, ETH, altcoins, gold, stocks?]\n"
        "⚡ ACTION: [Should the reader buy/sell/wait? Give a specific level or trigger, or say 'wait for confirmation']\n"
        "Keep each line under 25 words. No extra text."
    )
    try:
        resp = await asyncio.to_thread(
            ai_manager.client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        log_err(f"news impact gpt error: {exc}")
        return ""


async def news_monitor(app: Application, state: BotState, client: httpx.AsyncClient) -> None:
    log.info("news_monitor started")
    rss_urls = [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://feeds.feedburner.com/CoinDesk",
    ]
    while not state.stop_event.is_set():
        sleep_seconds = 300
        try:
            for feed_url in rss_urls:
                # Some RSS endpoints return redirects; follow them so feedparser can parse.
                response = await client.get(feed_url, timeout=25.0, follow_redirects=True)
                response.raise_for_status()
                parsed = feedparser.parse(response.content)
                for entry in parsed.entries:
                    title = (entry.get("title") or "").strip()
                    url = (entry.get("link") or "").strip()
                    if not title or not url or already_seen(state, url, ttl_seconds=6 * 3600):
                        continue
                    lower_title = title.lower()
                    if not any(k.lower() in lower_title for k in HIGH_IMPACT_KEYWORDS):
                        continue
                    published_raw = entry.get("published") or entry.get("updated") or ""
                    published_iso = ""
                    if published_raw:
                        try:
                            published_iso = parsedate_to_datetime(published_raw).astimezone(UTC).isoformat()
                        except Exception:
                            published_iso = ""
                    # GPT-powered impact analysis
                    impact = await _gpt_news_impact(app, title, url)
                    msg_parts = [
                        "🔴 HIGH IMPACT NEWS",
                        f"📰 {title}",
                        f"⏱ {time_ago(published_iso) if published_iso else 'unknown'}",
                    ]
                    if impact:
                        msg_parts.append("")
                        msg_parts.append(impact)
                    msg_parts.append(f"\n🔗 {url}")
                    await send_alert(app, state, "news", "\n".join(msg_parts))
            state.dedup_urls.cleanup()
        except Exception as exc:
            log_err(f"news monitor error: {exc}")
            sleep_seconds = 60
        await asyncio.sleep(sleep_seconds)


async def fetch_klines(client: httpx.AsyncClient, symbol: str, interval: str, limit: int) -> list[list[Any]]:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = await get_json(client, "https://api.binance.com/api/v3/klines", params=params)
    if not isinstance(data, list):
        return []
    return data


async def preload_15m_volumes(state: BotState, client: httpx.AsyncClient) -> None:
    for symbol in state.config.tracked_pairs:
        try:
            klines = await fetch_klines(client, symbol, "15m", 672)
            for k in klines:
                volume = float(k[5])
                state.volumes_15m[symbol].append(volume)
        except Exception as exc:
            log_err(f"volume preload error {symbol}: {exc}")


async def get_hourly_rsi(client: httpx.AsyncClient, symbol: str) -> Optional[float]:
    try:
        klines = await fetch_klines(client, symbol, "1h", 200)
        closes = [float(k[4]) for k in klines]
        return compute_rsi(closes, 14)
    except Exception as exc:
        log_err(f"rsi fetch error {symbol}: {exc}")
        return None


def get_level_for_symbol(state: BotState, symbol: str) -> Optional[float]:
    if symbol == "BTCUSDT":
        return state.config.btc_level
    if symbol == "ETHUSDT":
        return state.config.eth_level
    return None


async def price_monitor(app: Application, state: BotState, client: httpx.AsyncClient) -> None:
    log.info("price_monitor started")
    await preload_15m_volumes(state, client)
    while not state.stop_event.is_set():
        streams = "/".join(f"{p.lower()}@kline_15m" for p in state.config.tracked_pairs)
        ws_url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                log.info("price_monitor connected to Binance WS")
                async for raw in ws:
                    if state.stop_event.is_set():
                        break
                    try:
                        payload = json.loads(raw)
                        kline = payload.get("data", {}).get("k", {})
                        symbol = kline.get("s")
                        if symbol not in state.config.tracked_pairs:
                            continue
                        open_p = float(kline.get("o", 0.0))
                        close_p = float(kline.get("c", 0.0))
                        volume = float(kline.get("v", 0.0))
                        is_closed = bool(kline.get("x"))
                        state.last_prices[symbol] = close_p
                        level = get_level_for_symbol(state, symbol)
                        if level:
                            prev_side = state.level_state.get(symbol)
                            current_side = "above" if close_p >= level else "below"
                            if prev_side and prev_side != current_side and can_fire_cooldown(state, symbol, "level"):
                                msg = (
                                    f"⚡ PRICE ALERT — {symbol}\n"
                                    f"🎯 Level cross: {level:,.2f}\n"
                                    f"💵 Price: ${close_p:,.2f}\n"
                                    f"⏱ {now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}"
                                )
                                await send_alert(app, state, "price", msg)
                            state.level_state[symbol] = current_side
                        if not is_closed or open_p <= 0:
                            continue
                        move_pct = ((close_p - open_p) / open_p) * 100.0
                        avg_volume = sum(state.volumes_15m[symbol]) / len(state.volumes_15m[symbol]) if state.volumes_15m[symbol] else 0.0
                        volume_ratio = (volume / avg_volume) if avg_volume > 0 else 0.0
                        state.volumes_15m[symbol].append(volume)
                        rsi = await get_hourly_rsi(client, symbol)
                        rsi_signal = None
                        if rsi is not None and rsi >= state.config.rsi_high:
                            rsi_signal = "Overbought"
                        elif rsi is not None and rsi <= state.config.rsi_low:
                            rsi_signal = "Oversold"
                        trigger_fired = False
                        if abs(move_pct) >= state.config.price_move_pct and can_fire_cooldown(state, symbol, "move15m"):
                            trigger_fired = True
                        elif rsi_signal and can_fire_cooldown(state, symbol, "rsi1h"):
                            trigger_fired = True
                        elif volume_ratio >= state.config.volume_spike_x and can_fire_cooldown(state, symbol, "volspike"):
                            trigger_fired = True
                        if trigger_fired:
                            rsi_text = f"{rsi:.1f} — {rsi_signal}" if rsi is not None and rsi_signal else (f"{rsi:.1f} — Neutral" if rsi is not None else "N/A")
                            msg = (
                                f"⚡ PRICE ALERT — {symbol}\n"
                                f"📊 Move: {move_pct:+.2f}% in 15m  |  Price: ${close_p:,.2f}\n"
                                f"📈 RSI(14)h: {rsi_text}\n"
                                f"🔊 Volume: {volume_ratio:.2f}× 7d avg\n"
                                f"⏱ {now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}"
                            )
                            await send_alert(app, state, "price", msg)
                    except Exception as inner_exc:
                        log_err(f"price message parse error: {inner_exc}")
        except Exception as exc:
            log_err(f"price websocket error: {exc}")
            await asyncio.sleep(60)


def parse_pair_age_hours(pair_created_at_ms: Optional[int]) -> Optional[float]:
    if not pair_created_at_ms:
        return None
    created = datetime.fromtimestamp(pair_created_at_ms / 1000, tz=UTC)
    age = now_utc() - created
    return age.total_seconds() / 3600.0


async def fetch_goplus_security(client: httpx.AsyncClient, chain_id: str, contract_addr: str) -> dict[str, Any]:
    try:
        url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
        data = await get_json(client, url, params={"contract_addresses": contract_addr})
        result = data.get("result", {})
        if isinstance(result, dict):
            token_info = result.get(contract_addr.lower()) or result.get(contract_addr) or {}
            if isinstance(token_info, dict):
                return token_info
    except Exception as exc:
        log_err(f"goplus error ({contract_addr}): {exc}")
    return {}


async def dex_monitor(app: Application, state: BotState, client: httpx.AsyncClient) -> None:
    log.info("dex_monitor started")
    tracked_tokens = [p.replace("USDT", "") for p in state.config.tracked_pairs]
    while not state.stop_event.is_set():
        sleep_seconds = 180
        try:
            for token in tracked_tokens:
                data = await get_json(client, "https://api.dexscreener.com/latest/dex/search", params={"q": token})
                pairs = data.get("pairs", []) or []
                for pair in pairs:
                    dex_id = (pair.get("dexId") or "").lower()
                    if dex_id not in {"uniswap", "pancakeswap"}:
                        continue
                    pair_addr = pair.get("pairAddress", "")
                    if not pair_addr:
                        continue
                    liq_usd = float((pair.get("liquidity") or {}).get("usd") or 0.0)
                    age_h = parse_pair_age_hours(pair.get("pairCreatedAt"))
                    # Trigger 1: New token listing signal
                    if age_h is not None and age_h < 2.0 and liq_usd >= state.config.dex_min_liq and not state.seen_pairs.contains(pair_addr):
                        base = pair.get("baseToken") or {}
                        chain_id = str(pair.get("chainId") or "1")
                        contract_addr = base.get("address", "")
                        sec = await fetch_goplus_security(client, chain_id, contract_addr) if contract_addr else {}
                        honeypot = sec.get("is_honeypot", "0")
                        buy_tax = sec.get("buy_tax", "0")
                        sell_tax = sec.get("sell_tax", "0")
                        holders = sec.get("holder_count", "N/A")
                        hp_text = "YES" if str(honeypot) == "1" else "NO"
                        msg = (
                            f"🆕 NEW TOKEN — {base.get('name', 'Unknown')} ({base.get('symbol', 'N/A')})\n"
                            f"💧 Liquidity: ${liq_usd:,.0f}  |  🕐 Age: {age_h:.1f}h\n"
                            f"🔒 Honeypot: {hp_text}  |  Tax: {buy_tax}% / {sell_tax}%\n"
                            f"👥 Holders: {holders}\n"
                            f"🔗 {pair.get('url', '')}"
                        )
                        await send_alert(app, state, "dex", msg)
                        state.seen_pairs.add(pair_addr, ttl_seconds=24 * 3600)
                    # Trigger 2: Large per-minute flow proxy from txns/volume
                    volume_m5 = float((pair.get("volume") or {}).get("m5") or 0.0)
                    if volume_m5 >= state.config.dex_whale_usd and can_fire_cooldown(state, pair_addr, "dex_whale_flow"):
                        msg = (
                            f"🐋 DEX WHALE FLOW — {token}\n"
                            f"💵 5m traded value: ${volume_m5:,.0f}\n"
                            f"💧 Liquidity: ${liq_usd:,.0f}\n"
                            f"🔗 {pair.get('url', '')}"
                        )
                        await send_alert(app, state, "dex", msg)
                    # Trigger 3: Rug risk on liquidity drawdown over 10 minutes
                    hist = state.dex_liquidity_history[pair_addr]
                    current_time = now_utc()
                    hist.append((current_time, liq_usd))
                    while hist and (current_time - hist[0][0]) > timedelta(minutes=10):
                        hist.popleft()
                    if len(hist) >= 2:
                        old_liq = hist[0][1]
                        if old_liq > 0:
                            drop_pct = ((old_liq - liq_usd) / old_liq) * 100.0
                            if drop_pct >= state.config.dex_rug_pct and can_fire_cooldown(state, pair_addr, "dex_rug"):
                                base = pair.get("baseToken") or {}
                                msg = (
                                    f"🚨 POSSIBLE RUG — {base.get('symbol', token)}\n"
                                    f"💧 Liquidity drop: {drop_pct:.1f}% in 10m\n"
                                    f"From ${old_liq:,.0f} to ${liq_usd:,.0f}\n"
                                    f"🔗 {pair.get('url', '')}"
                                )
                                await send_alert(app, state, "dex", msg)
            state.seen_pairs.cleanup()
        except Exception as exc:
            log_err(f"dex monitor error: {exc}")
            sleep_seconds = 60
        await asyncio.sleep(sleep_seconds)


def classify_fng(value: int) -> str:
    if value <= 20:
        return "EXTREME FEAR"
    if value <= 45:
        return "FEAR"
    if value <= 55:
        return "NEUTRAL"
    if value <= 80:
        return "GREED"
    return "EXTREME GREED"


async def sentiment_monitor(app: Application, state: BotState, client: httpx.AsyncClient) -> None:
    log.info("sentiment_monitor started")
    while not state.stop_event.is_set():
        sleep_seconds = 3600
        try:
            data = await get_json(client, "https://api.alternative.me/fng/", params={"limit": 8})
            values = data.get("data", []) or []
            if values:
                latest = int(values[0].get("value", 0))
                trend = [v.get("value", "?") for v in reversed(values[:5])]
                trend_text = " -> ".join(str(x) for x in trend)
                today = now_utc().date()
                if latest <= state.config.fear_low:
                    if state.sentiment_crossed_today.get("fear") != today:
                        msg = (
                            "😨 SENTIMENT SHIFT\n"
                            f"Fear & Greed: {latest} — {classify_fng(latest)}\n"
                            f"7-day trend: {trend_text}\n"
                            "💡 Historically a long-term buy zone"
                        )
                        await send_alert(app, state, "sentiment", msg)
                        state.sentiment_crossed_today["fear"] = today
                elif latest >= state.config.greed_high:
                    if state.sentiment_crossed_today.get("greed") != today:
                        msg = (
                            "😨 SENTIMENT SHIFT\n"
                            f"Fear & Greed: {latest} — {classify_fng(latest)}\n"
                            f"7-day trend: {trend_text}\n"
                            "💡 Historically a risk-off / distribution zone"
                        )
                        await send_alert(app, state, "sentiment", msg)
                        state.sentiment_crossed_today["greed"] = today
        except Exception as exc:
            log_err(f"sentiment monitor error: {exc}")
            sleep_seconds = 60
        await asyncio.sleep(sleep_seconds)


async def run_modules(app: Application, state: BotState, client: httpx.AsyncClient) -> None:
    state.tasks = [
        asyncio.create_task(news_monitor(app, state, client), name="news_monitor"),
        asyncio.create_task(price_monitor(app, state, client), name="price_monitor"),
        asyncio.create_task(dex_monitor(app, state, client), name="dex_monitor"),
        asyncio.create_task(sentiment_monitor(app, state, client), name="sentiment_monitor"),
    ]
    await asyncio.gather(*state.tasks)


def format_dt(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "never"


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    prices = []
    for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        px = state.last_prices.get(symbol)
        prices.append(f"{symbol}: ${px:,.2f}" if px is not None else f"{symbol}: N/A")
    text = (
        "📡 BOT STATUS\n"
        + "\n".join(prices)
        + "\n\nLast alerts:\n"
        f"News: {format_dt(state.last_alert_time['news'])}\n"
        f"Price: {format_dt(state.last_alert_time['price'])}\n"
        f"DEX: {format_dt(state.last_alert_time['dex'])}\n"
        f"Sentiment: {format_dt(state.last_alert_time['sentiment'])}"
    )
    await update.message.reply_text(text)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    state.paused_until = now_utc() + timedelta(hours=1)
    await update.message.reply_text("⏸ Alerts paused for 1 hour.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    state.paused_until = None
    await update.message.reply_text("▶️ Alerts resumed.")


def _set_value(state: BotState, key: str, val: str) -> bool:
    k = key.upper()
    cfg = state.config
    try:
        if k == "PRICE_MOVE_PCT":
            cfg.price_move_pct = float(val)
        elif k == "RSI_HIGH":
            cfg.rsi_high = float(val)
        elif k == "RSI_LOW":
            cfg.rsi_low = float(val)
        elif k == "VOLUME_SPIKE_X":
            cfg.volume_spike_x = float(val)
        elif k == "ALERT_COOLDOWN_MIN":
            cfg.alert_cooldown_min = int(float(val))
        elif k == "NEWS_VOTE_THRESHOLD":
            cfg.news_vote_threshold = int(float(val))
        elif k == "DEX_MIN_LIQ":
            cfg.dex_min_liq = float(val)
        elif k == "DEX_WHALE_USD":
            cfg.dex_whale_usd = float(val)
        elif k == "DEX_RUG_PCT":
            cfg.dex_rug_pct = float(val)
        elif k == "FEAR_LOW":
            cfg.fear_low = int(float(val))
        elif k == "GREED_HIGH":
            cfg.greed_high = int(float(val))
        elif k == "BTC_LEVEL":
            cfg.btc_level = float(val)
        elif k == "ETH_LEVEL":
            cfg.eth_level = float(val)
        else:
            return False
        return True
    except ValueError:
        return False


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /set KEY VALUE")
        return
    key, value = context.args[0], context.args[1]
    state: BotState = context.application.bot_data["state"]
    ok = _set_value(state, key, value)
    if not ok:
        await update.message.reply_text(f"❌ Unable to set {key}.")
        return
    await update.message.reply_text(f"✅ Updated {key.upper()} to {value}.")


async def on_startup(app: Application) -> None:
    state: BotState = app.bot_data["state"]
    client = httpx.AsyncClient()
    app.bot_data["httpx_client"] = client
    state.module_runner = asyncio.create_task(run_modules(app, state, client), name="module_runner")


async def on_shutdown(app: Application) -> None:
    state: BotState = app.bot_data["state"]
    state.stop_event.set()
    for task in state.tasks:
        task.cancel()
    if state.tasks:
        await asyncio.gather(*state.tasks, return_exceptions=True)
    if state.module_runner:
        state.module_runner.cancel()
        await asyncio.gather(state.module_runner, return_exceptions=True)
    client: Optional[httpx.AsyncClient] = app.bot_data.get("httpx_client")
    if client:
        await client.aclose()


def build_app(state: BotState) -> Application:
    app = Application.builder().token(state.config.telegram_bot_token).build()
    app.bot_data["state"] = state
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("set", cmd_set))
    app.post_init = on_startup
    app.post_shutdown = on_shutdown
    return app


class AiManager:
    def __init__(self, openai_api_key: str) -> None:
        self.openai_api_key = openai_api_key
        self.client = OpenAI(api_key=openai_api_key) if openai_api_key else None
        self.daily_limit = AI_DAILY_CALL_LIMIT
        self.daily_used = 0
        self.daily_date_sgt: Optional[datetime.date] = None

        # user_id -> deque of {"role": ..., "content": ...} (last 6 messages)
        self.user_history: dict[int, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=6))
        self.user_last_activity: dict[int, datetime] = {}

        self.history_ttl = timedelta(hours=2)

        # SGT is UTC+8
        self.sgt_tz = timezone(timedelta(hours=8))
        self._lock = asyncio.Lock()
        self.memory = _read_json_file(MEMORY_FILE, DEFAULT_MEMORY)
        self.portfolio = _read_json_file(PORTFOLIO_FILE, DEFAULT_PORTFOLIO)
        self.position_alert_sent: dict[str, datetime.date] = {}

    def _today_sgt(self) -> datetime.date:
        return datetime.now(tz=self.sgt_tz).date()

    def _reset_daily_if_needed(self) -> None:
        today = self._today_sgt()
        if self.daily_date_sgt != today:
            self.daily_date_sgt = today
            self.daily_used = 0

    def _get_history(self, user_id: int, now: datetime) -> deque[dict[str, str]]:
        last = self.user_last_activity.get(user_id)
        if last is None or (now - last) > self.history_ttl:
            self.user_history[user_id].clear()
        self.user_last_activity[user_id] = now
        return self.user_history[user_id]

    def can_call(self) -> tuple[bool, int]:
        self._reset_daily_if_needed()
        if self.daily_used >= self.daily_limit:
            return False, self.daily_used
        return True, self.daily_used

    def mark_called(self) -> None:
        self._reset_daily_if_needed()
        self.daily_used += 1

    def persist(self) -> None:
        _write_json_file(MEMORY_FILE, self.memory)
        _write_json_file(PORTFOLIO_FILE, self.portfolio)


CRYPTO_TRACKED = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "BNBUSDT": "BNB",
    "AVAXUSDT": "AVAX",
}
CRYPTO_KEYS_BY_INPUT = {**{v: k for k, v in CRYPTO_TRACKED.items()}, **{k: k for k in CRYPTO_TRACKED.keys()}}

STOCK_KEYS_BY_INPUT = {
    "SPY": "SPY",
    "QQQ": "QQQ",
    "GLD": "GLD",
    "USO": "USO",
    "UUP": "UUP",
    "VIX": "^VIX",
    "GOLD": "GLD",
    "OIL": "USO",
    "DXY": "UUP",
}

COMMOD_KEYS_BY_INPUT = {
    "GC=F": "GC=F",
    "CL=F": "CL=F",
    "SI=F": "SI=F",
    "GOLD FUTURES": "GC=F",
    "CRUDE OIL": "CL=F",
    "SILVER": "SI=F",
}

_BASE_DIR = Path(__file__).parent
MEMORY_FILE = _BASE_DIR / "memory.json"
PORTFOLIO_FILE = _BASE_DIR / "portfolio.json"


DEFAULT_MEMORY = {
    "user_profile": {
        "budget": "$1,000-$10,000",
        "goal": "small consistent gains",
        "experience": "beginner",
        "location": "Taiwan",
        "interests": ["BTC", "ETH", "Gold", "SOL", "MNT"],
    },
    "conversation_history": [],
    "noted_interests": [],
    "open_setups": [],
    "last_updated": "",
}

DEFAULT_PORTFOLIO = {
    "positions": [],
    "cash_remaining": 5000.0,
    "total_budget": 10000.0,
}


def fmt_fng(value: Optional[int]) -> tuple[int, str]:
    if value is None:
        return 0, "Neutral"
    if value <= 20:
        return value, "Extreme Fear"
    if value <= 45:
        return value, "Fear"
    if value <= 55:
        return value, "Neutral"
    if value <= 80:
        return value, "Greed"
    return value, "Extreme Greed"


def rsi_state_label(rsi: Optional[float]) -> str:
    if rsi is None:
        return "Neutral"
    if rsi < 35:
        return "Oversold"
    if rsi > 65:
        return "Overbought"
    return "Neutral"


def escape_for_html(text: str) -> str:
    # We keep replies simple (no HTML tags), but this prevents accidental breakage.
    return html.escape(text, quote=False)


def _read_json_file(path: Path, default_obj: dict[str, Any]) -> dict[str, Any]:
    try:
        if not path.exists():
            path.write_text(json.dumps(default_obj, indent=2), encoding="utf-8")
            return json.loads(json.dumps(default_obj))
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        log_err(f"json read error {path.name}: {exc}")
    return json.loads(json.dumps(default_obj))


def _write_json_file(path: Path, data: dict[str, Any]) -> None:
    try:
        data["last_updated"] = now_utc().isoformat()
    except Exception:
        pass
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        log_err(f"json write error {path.name}: {exc}")


def extract_coin_candidates(text: str) -> list[str]:
    # Detect forms like $MNT, mnt, pepe, arb; keep short uppercase tokens.
    if not text:
        return []
    hits = []
    for m in re.findall(r"\$?([A-Za-z]{2,10})\b", text):
        s = m.upper()
        if s in {"WHAT", "THIS", "THAT", "LONG", "SHORT", "WITH", "FROM", "WEEK", "MONTH"}:
            continue
        hits.append(s)
    dedup: list[str] = []
    for h in hits:
        if h not in dedup:
            dedup.append(h)
    return dedup[:5]


async def resolve_coin_price(httpx_client: httpx.AsyncClient, symbol: str) -> tuple[Optional[str], Optional[float], str]:
    # 1) Binance SYMBOLUSDT
    pair = f"{symbol.upper()}USDT"
    try:
        r = await httpx_client.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": pair}, timeout=15.0)
        if r.status_code == 200:
            data = r.json()
            px = float(data.get("price"))
            return pair, px, "binance"
    except Exception:
        pass

    # 2) CoinGecko fallback
    try:
        # Search coin id
        s = symbol.lower()
        search = await httpx_client.get("https://api.coingecko.com/api/v3/search", params={"query": s}, timeout=15.0)
        if search.status_code == 200:
            coins = search.json().get("coins", []) or []
            if coins:
                coin_id = coins[0].get("id")
                if coin_id:
                    p = await httpx_client.get(
                        "https://api.coingecko.com/api/v3/simple/price",
                        params={"ids": coin_id, "vs_currencies": "usd"},
                        timeout=15.0,
                    )
                    if p.status_code == 200:
                        px = float(p.json().get(coin_id, {}).get("usd"))
                        return symbol.upper(), px, "coingecko"
    except Exception:
        pass
    return None, None, "none"


def update_memory_conversation(memory: dict[str, Any], user_text: str, ai_reply: str) -> None:
    conv = memory.get("conversation_history")
    if not isinstance(conv, list):
        conv = []
    conv.append({"ts": now_utc().isoformat(), "role": "user", "content": user_text})
    conv.append({"ts": now_utc().isoformat(), "role": "assistant", "content": ai_reply})
    memory["conversation_history"] = conv[-20:]


def add_noted_interest(memory: dict[str, Any], symbol: str) -> None:
    arr = memory.get("noted_interests")
    if not isinstance(arr, list):
        arr = []
    symbol_u = symbol.upper()
    if symbol_u not in arr:
        arr.append(symbol_u)
    memory["noted_interests"] = arr[-30:]


async def fetch_weekly_context(application: Application, state: Any) -> str:
    httpx_client: Optional[httpx.AsyncClient] = application.bot_data.get("httpx_client")
    created_temp = False
    if httpx_client is None:
        httpx_client = httpx.AsyncClient()
        created_temp = True
    try:
        btc7 = 0.0
        eth7 = 0.0
        try:
            btc_kl = await fetch_klines(httpx_client, "BTCUSDT", "1d", 8)
            eth_kl = await fetch_klines(httpx_client, "ETHUSDT", "1d", 8)
            if len(btc_kl) >= 8:
                b0 = float(btc_kl[0][4])
                b1 = float(btc_kl[-1][4])
                btc7 = ((b1 - b0) / b0) * 100.0 if b0 else 0.0
            if len(eth_kl) >= 8:
                e0 = float(eth_kl[0][4])
                e1 = float(eth_kl[-1][4])
                eth7 = ((e1 - e0) / e0) * 100.0 if e0 else 0.0
        except Exception:
            pass

        # Fear & Greed 7d trend
        fng_trend = "N/A"
        try:
            resp = await httpx_client.get("https://api.alternative.me/fng/", params={"limit": 7}, timeout=20.0)
            resp.raise_for_status()
            vals = [v.get("value", "?") for v in (resp.json().get("data", []) or [])]
            if vals:
                fng_trend = " -> ".join(reversed([str(v) for v in vals]))
        except Exception:
            pass

        # Top 2 headlines from last 48h
        headlines: list[str] = []
        try:
            rss_urls = [
                "https://www.coindesk.com/arc/outboundfeeds/rss/",
                "https://cointelegraph.com/rss",
            ]
            cutoff = now_utc() - timedelta(hours=48)
            for u in rss_urls:
                rr = await httpx_client.get(u, timeout=20.0, follow_redirects=True)
                rr.raise_for_status()
                parsed = feedparser.parse(rr.content)
                for entry in parsed.entries:
                    title = (entry.get("title") or "").strip()
                    if not title:
                        continue
                    published = entry.get("published_parsed") or entry.get("updated_parsed")
                    entry_dt = None
                    if published:
                        entry_dt = datetime(
                            published.tm_year, published.tm_mon, published.tm_mday,
                            published.tm_hour, published.tm_min, published.tm_sec,
                            tzinfo=UTC,
                        )
                    if entry_dt and entry_dt < cutoff:
                        continue
                    headlines.append(title)
            # dedup
            dedup: list[str] = []
            for h in headlines:
                if h not in dedup:
                    dedup.append(h)
            headlines = dedup[:2]
        except Exception:
            headlines = []

        htext = " | ".join(headlines) if headlines else "No major headline captured"
        return f"WEEKLY CONTEXT: BTC 7d: {btc7:+.2f}%. ETH 7d: {eth7:+.2f}%. F&G trend: {fng_trend}. Headlines: {htext}"
    finally:
        if created_temp and httpx_client is not None:
            await httpx_client.aclose()


def compute_regime(spy_pct: Optional[float], vix_pct: Optional[float], btc_pct: Optional[float], gold_pct: Optional[float]) -> tuple[str, str, str, str]:
    regime = "Transitional"
    if spy_pct is not None and vix_pct is not None and btc_pct is not None and spy_pct > 0 and vix_pct < 0 and btc_pct > 0:
        regime = "Risk-On"
    elif spy_pct is not None and vix_pct is not None and gold_pct is not None and spy_pct < 0 and vix_pct > 0 and gold_pct > 0:
        regime = "Risk-Off"
    narrative = (
        "Liquidity and growth optimism are supporting risk assets."
        if regime == "Risk-On"
        else "Defensive positioning is active as macro uncertainty rises."
        if regime == "Risk-Off"
        else "Cross-asset signals are mixed, suggesting indecision."
    )
    divergence = (
        "Gold and crypto are moving together more than usual."
        if regime == "Risk-Off"
        else "Dollar-linked moves are not fully confirming equity direction."
    )
    edge = (
        "Focus on clean pullbacks in stronger assets with volume confirmation."
        if regime == "Risk-On"
        else "Prioritize capital preservation and only take high R:R setups."
    )
    return regime, narrative, divergence, edge


def portfolio_snapshot_text(portfolio: dict[str, Any], price_map: dict[str, float]) -> str:
    positions = portfolio.get("positions", [])
    cash = float(portfolio.get("cash_remaining", 0.0))
    total_budget = float(portfolio.get("total_budget", 0.0))
    lines = ["💼 MY PORTFOLIO", "━━━━━━━━━━━━━━━━━━━━━━━"]
    invested = 0.0
    value = 0.0
    best = None
    worst = None
    best_pct = -10**9
    worst_pct = 10**9
    for pos in positions:
        sym = str(pos.get("symbol", "")).upper()
        amt = float(pos.get("amount", 0.0))
        buy = float(pos.get("buy_price", 0.0))
        cur = float(price_map.get(sym, buy))
        inv = amt * buy
        curv = amt * cur
        pnl = curv - inv
        pct = (pnl / inv * 100.0) if inv > 0 else 0.0
        invested += inv
        value += curv
        lines.append(f"🪙 {sym} {amt} @ ${buy:,.2f}")
        lines.append(f"   Now: ${cur:,.2f} | P&L: {pnl:+,.2f} ({pct:+.2f}%)")
        if pct > best_pct:
            best_pct = pct
            best = sym
        if pct < worst_pct:
            worst_pct = pct
            worst = sym

    total_value = value + cash
    total_pnl = total_value - total_budget
    total_pct = (total_pnl / total_budget * 100.0) if total_budget > 0 else 0.0
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Invested: ${invested:,.2f} | Value: ${value:,.2f}")
    lines.append(f"Total P&L: {total_pnl:+,.2f} ({total_pct:+.2f}%)")
    lines.append(f"Cash: ${cash:,.2f} | Total: ${total_value:,.2f}")
    lines.append(f"Best: {best or 'N/A'} | Worst: {worst or 'N/A'}")
    lines.append("📌 Not financial advice.")
    return "\n".join(lines)


async def fetch_live_binance_24h(httpx_client: httpx.AsyncClient, symbols: list[str]) -> dict[str, dict[str, float]]:
    # Single request for efficiency.
    resp = await httpx_client.get("https://api.binance.com/api/v3/ticker/24hr", timeout=25.0)
    resp.raise_for_status()
    data = resp.json()
    by_symbol: dict[str, dict[str, float]] = {}
    for item in data:
        sym = str(item.get("symbol", "")).upper()
        if sym not in set(symbols):
            continue
        try:
            by_symbol[sym] = {
                "last": float(item.get("lastPrice")),
                "pct": float(item.get("priceChangePercent")),
            }
        except Exception:
            continue
    return by_symbol


def _yf_last_and_pct_sync(ticker: str) -> tuple[Optional[float], Optional[float], bool]:
    t = yf.Ticker(ticker)
    try:
        info = t.fast_info or {}
        last = info.get("last_price") or info.get("lastPrice")
        prev = info.get("previous_close") or info.get("previousClose")
        market_open = bool(info.get("is_market_open", False))
        if last is not None and prev not in (None, 0):
            last_f = float(last)
            prev_f = float(prev)
            pct = (last_f - prev_f) / prev_f * 100.0 if prev_f else None
            return last_f, pct, market_open
    except Exception:
        pass

    try:
        hist = t.history(period="7d", interval="1d", progress=False, auto_adjust=False)
        if hist is None or hist.empty or len(hist) < 2:
            return None, None, False
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        pct = (last - prev) / prev * 100.0 if prev else None
        return last, pct, False
    except Exception:
        return None, None, False


async def fetch_live_yf_prices(tickers: list[str]) -> dict[str, dict[str, float]]:
    results: dict[str, dict[str, float | bool]] = {}
    for t in tickers:
        last, pct, market_open = await asyncio.to_thread(_yf_last_and_pct_sync, t)
        if last is None:
            continue
        results[t] = {
            "last": float(last),
            "pct": float(pct) if pct is not None else 0.0,
            "market_open": bool(market_open),
        }
    return results


async def fetch_fear_greed(httpx_client: httpx.AsyncClient, state: Any) -> Optional[int]:
    try:
        if getattr(state, "fng_value", None) is not None:
            return int(state.fng_value)
    except Exception:
        pass
    try:
        resp = await httpx_client.get("https://api.alternative.me/fng/", params={"limit": 8}, timeout=25.0)
        resp.raise_for_status()
        data = resp.json()
        values = data.get("data", []) or []
        if not values:
            return None
        return int(values[0].get("value", 0))
    except Exception:
        return None


async def fetch_live_market_data(application: Application, state: Any) -> str:
    # Must fetch current prices before each API call.
    httpx_client: Optional[httpx.AsyncClient] = application.bot_data.get("httpx_client")
    created_temp = False
    if httpx_client is None:
        httpx_client = httpx.AsyncClient()
        created_temp = True
    try:
        binance_symbols = list(CRYPTO_TRACKED.keys())
        binance_prices = await fetch_live_binance_24h(httpx_client, binance_symbols)

        yf_tickers = ["SPY", "QQQ", "UUP", "GLD", "USO", "GC=F", "CL=F", "SI=F", "^VIX"]
        yf_prices = await fetch_live_yf_prices(yf_tickers)

        fng = await fetch_fear_greed(httpx_client, state)
        fng_val, fng_label = fmt_fng(fng)

        def seg(name: str, value: Optional[float], pct: Optional[float], market_open: Optional[bool] = True, is_index: bool = False) -> str:
            if value is None or pct is None:
                return f"{name} N/A"
            sign = "+" if pct >= 0 else ""
            if is_index and market_open is False:
                return f"{name} last close: ${value:,.0f} ({sign}{pct:.1f}%)"
            return f"{name} ${value:,.0f} ({sign}{pct:.1f}%)"

        btc = binance_prices.get("BTCUSDT", {}).get("last")
        btc_pct = binance_prices.get("BTCUSDT", {}).get("pct")
        eth = binance_prices.get("ETHUSDT", {}).get("last")
        eth_pct = binance_prices.get("ETHUSDT", {}).get("pct")
        sol = binance_prices.get("SOLUSDT", {}).get("last")
        sol_pct = binance_prices.get("SOLUSDT", {}).get("pct")
        bnb = binance_prices.get("BNBUSDT", {}).get("last")
        bnb_pct = binance_prices.get("BNBUSDT", {}).get("pct")
        avax = binance_prices.get("AVAXUSDT", {}).get("last")
        avax_pct = binance_prices.get("AVAXUSDT", {}).get("pct")

        spx = yf_prices.get("SPY", {}).get("last")
        spx_pct = yf_prices.get("SPY", {}).get("pct")
        spx_open = bool(yf_prices.get("SPY", {}).get("market_open", False))
        qqq = yf_prices.get("QQQ", {}).get("last")
        qqq_pct = yf_prices.get("QQQ", {}).get("pct")
        qqq_open = bool(yf_prices.get("QQQ", {}).get("market_open", False))
        dxy = yf_prices.get("UUP", {}).get("last")
        dxy_pct = yf_prices.get("UUP", {}).get("pct")
        dxy_open = bool(yf_prices.get("UUP", {}).get("market_open", False))

        gold = yf_prices.get("GLD", {}).get("last")
        gold_pct = yf_prices.get("GLD", {}).get("pct")
        oil = yf_prices.get("USO", {}).get("last")
        oil_pct = yf_prices.get("USO", {}).get("pct")
        gc = yf_prices.get("GC=F", {}).get("last")
        gc_pct = yf_prices.get("GC=F", {}).get("pct")
        cl = yf_prices.get("CL=F", {}).get("last")
        cl_pct = yf_prices.get("CL=F", {}).get("pct")
        si = yf_prices.get("SI=F", {}).get("last")
        si_pct = yf_prices.get("SI=F", {}).get("pct")
        vix = yf_prices.get("^VIX", {}).get("last")
        vix_pct = yf_prices.get("^VIX", {}).get("pct")

        parts = [
            seg("BTC", btc, btc_pct),
            seg("ETH", eth, eth_pct),
            seg("SOL", sol, sol_pct),
            seg("BNB", bnb, bnb_pct),
            seg("AVAX", avax, avax_pct),
            seg("SPY", spx, spx_pct, spx_open, is_index=True),
            seg("QQQ", qqq, qqq_pct, qqq_open, is_index=True),
            seg("DXY", dxy, dxy_pct, dxy_open, is_index=True),
            seg("VIX", vix, vix_pct, True, is_index=False),
            seg("Gold", gold, gold_pct),
            seg("Oil", oil, oil_pct),
            seg("GC=F", gc, gc_pct),
            seg("CL=F", cl, cl_pct),
            seg("SI=F", si, si_pct),
            f"Fear&Greed: {fng_val} ({'Fear' if 'Fear' in fng_label else ('Greed' if 'Greed' in fng_label else 'Neutral')})",
        ]
        return ", ".join(parts)
    finally:
        if created_temp and httpx_client is not None:
            await httpx_client.aclose()


async def call_openai_for_text(
    application: Application,
    state: Any,
    user_id: int,
    user_text: str,
    extra_user_instruction: Optional[str] = None,
) -> tuple[Optional[str], Optional[int], Optional[str]]:
    # Returns (reply_text, total_tokens, error_message)
    ai_manager: AiManager = application.bot_data.get("ai_manager")
    if ai_manager is None:
        return None, None, "missing ai_manager"
    async with ai_manager._lock:
        ok, _ = ai_manager.can_call()
        if not ok:
            return "Daily AI limit reached.\nResets at midnight SGT. Use /status for live data.", None, None

        if not ai_manager.client:
            return "AI unavailable right now, try again in a minute", None, None

        # Reserve the call budget before making the API request.
        ai_manager.mark_called()

    now = now_utc()
    history = ai_manager._get_history(user_id, now)

    truncated = user_text.strip()
    if len(truncated) > AI_MAX_INPUT_CHARS:
        truncated = truncated[:AI_MAX_INPUT_CHARS]

    live_market_data = await fetch_live_market_data(application, state)
    weekly_context = await fetch_weekly_context(application, state)

    # Any-coin lookup injection (Binance -> CoinGecko fallback).
    httpx_client: Optional[httpx.AsyncClient] = application.bot_data.get("httpx_client")
    created_temp = False
    if httpx_client is None:
        httpx_client = httpx.AsyncClient()
        created_temp = True
    coin_injections: list[str] = []
    try:
        for candidate in extract_coin_candidates(truncated):
            if candidate in CRYPTO_KEYS_BY_INPUT or candidate in STOCK_KEYS_BY_INPUT or candidate in COMMOD_KEYS_BY_INPUT:
                continue
            resolved_sym, px, src = await resolve_coin_price(httpx_client, candidate)
            if px is not None:
                coin_injections.append(f"{candidate}: ${px:,.6f} ({src})")
                add_noted_interest(ai_manager.memory, candidate)
            else:
                coin_injections.append(f"{candidate}: not found on Binance/CoinGecko")
    except Exception:
        pass
    finally:
        if created_temp and httpx_client is not None:
            await httpx_client.aclose()
    any_coin_context = " | ".join(coin_injections) if coin_injections else "none"

    # Memory injection
    conv_hist = ai_manager.memory.get("conversation_history", [])
    last5 = conv_hist[-5:] if isinstance(conv_hist, list) else []
    memory_last5 = " | ".join([f"{m.get('role','?')}: {str(m.get('content',''))[:120]}" for m in last5]) if last5 else "none"
    portfolio_text = json.dumps(ai_manager.portfolio, ensure_ascii=False)

    system_prompt = (
        "You are a professional macro analyst and swing trader writing for a beginner in Taiwan "
        "with a $1,000-$10,000 budget whose goal is small consistent gains.\n\n"
        "Analysis style:\n"
        "- Lead with NARRATIVE not data\n"
        "- Identify divergences — these are the signals\n"
        "- Explain WHO is buying/selling and WHY\n"
        "- Connect every move to bigger macro picture\n"
        "- Give specific actionable levels\n"
        "- Think in scenarios: confirms vs kills thesis\n"
        "- R:R must be >= 1:2 or don't recommend\n\n"
        "Rules:\n"
        "- Never recommend leverage or futures\n"
        "- Always suggest $50-100 position size\n"
        "- Explain all jargon for a beginner\n"
        "- Keep replies under 5 sentences unless user asks for more or command requires structure\n"
        "- End every response: ⚠️ Not financial advice.\n\n"
        f"Context injected:\nLive data: {live_market_data}\n"
        f"Weekly context: {weekly_context}\n"
        f"Portfolio: {portfolio_text}\n"
        f"Memory: {memory_last5}\n"
        f"Any-coin lookup: {any_coin_context}\n"
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    # last 6 messages stored as role/content entries
    messages.extend(list(history))

    user_prompt = truncated
    if extra_user_instruction:
        user_prompt = f"{extra_user_instruction}\n\n{truncated}"
    messages.append({"role": "user", "content": user_prompt})

    try:
        resp = await asyncio.to_thread(
            ai_manager.client.chat.completions.create,
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=AI_MAX_TOKENS,
        )
        reply_text = resp.choices[0].message.content or ""
        usage_total = None
        try:
            usage_total = int(resp.usage.total_tokens) if resp.usage and resp.usage.total_tokens is not None else None
        except Exception:
            usage_total = None

        # Token usage logging
        if usage_total is not None:
            print(f"OpenAI usage: total_tokens={usage_total} | AI calls used today: {ai_manager.daily_used}/100")
        else:
            print(f"OpenAI usage: total_tokens=unknown | AI calls used today: {ai_manager.daily_used}/100")

        # Update history
        history.append({"role": "user", "content": truncated})
        history.append({"role": "assistant", "content": reply_text})
        update_memory_conversation(ai_manager.memory, truncated, reply_text)
        ai_manager.persist()
        return reply_text, usage_total, None
    except Exception as exc:
        # On failure, do not crash.
        log_err(f"openai call failed: {exc}")
        return "AI unavailable right now, try again in a minute", None, str(exc)


async def handle_free_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    state = context.application.bot_data.get("state")
    if state is None:
        return
    user_id = update.effective_user.id
    text = update.message.text
    reply, _, _ = await call_openai_for_text(
        application=context.application,
        state=state,
        user_id=user_id,
        user_text=text,
    )
    if not reply:
        return
    await update.message.reply_text(escape_for_html(reply), parse_mode="HTML", disable_web_page_preview=True)


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /ask {question}", parse_mode="HTML")
        return
    question = " ".join(context.args).strip()
    state = context.application.bot_data.get("state")
    user_id = update.effective_user.id
    reply, _, _ = await call_openai_for_text(
        application=context.application,
        state=state,
        user_id=user_id,
        user_text=question,
    )
    if reply:
        await update.message.reply_text(escape_for_html(reply), parse_mode="HTML", disable_web_page_preview=True)


async def cmd_analyse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.application.bot_data.get("state")
    if state is None or not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /analyse {symbol}", parse_mode="HTML")
        return
    symbol_in = " ".join(context.args).strip().upper()

    # Determine if it is crypto / stocks / commodities
    crypto_pair = CRYPTO_KEYS_BY_INPUT.get(symbol_in)
    stock_ticker = STOCK_KEYS_BY_INPUT.get(symbol_in) or STOCK_KEYS_BY_INPUT.get(symbol_in.upper())
    commod_ticker = COMMOD_KEYS_BY_INPUT.get(symbol_in)
    ticker_for_yf = None
    display_name = symbol_in
    is_crypto = crypto_pair is not None

    if is_crypto:
        display_name = CRYPTO_TRACKED.get(crypto_pair, crypto_pair)
    else:
        if stock_ticker:
            ticker_for_yf = stock_ticker
            display_name = "Gold" if ticker_for_yf == "GLD" else ("Oil" if ticker_for_yf == "USO" else ticker_for_yf)
        elif commod_ticker:
            ticker_for_yf = commod_ticker
            display_name = commod_ticker
        else:
            await update.message.reply_text("Unknown symbol. Try BTC, ETH, SOL, BNB, AVAX, SPY, QQQ, Gold, Oil.", parse_mode="HTML")
            return

    httpx_client: Optional[httpx.AsyncClient] = context.application.bot_data.get("httpx_client")
    created_temp = False
    if httpx_client is None:
        httpx_client = httpx.AsyncClient()
        created_temp = True

    try:
        fng_value = getattr(state, "fng_value", None)
        fng_val, fng_label = fmt_fng(int(fng_value) if fng_value is not None else None)

        last: Optional[float] = None
        pct24: Optional[float] = None
        rsi: Optional[float] = None
        volume_ratio: Optional[float] = None
        trend7: Optional[float] = None

        if is_crypto:
            binance = await fetch_live_binance_24h(httpx_client, [crypto_pair])
            last = binance.get(crypto_pair, {}).get("last")
            pct24 = binance.get(crypto_pair, {}).get("pct")

            klines = await fetch_klines(httpx_client, crypto_pair, "1h", 200)
            closes = [float(k[4]) for k in klines if len(k) > 4]
            volumes = [float(k[5]) for k in klines if len(k) > 5]
            rsi = compute_rsi(closes, 14) if closes else None

            if len(closes) >= 169:
                close_now = closes[-1]
                close_7d = closes[-169]
                trend7 = ((close_now - close_7d) / close_7d) * 100.0 if close_7d else 0.0
            else:
                trend7 = 0.0

            if len(volumes) >= 169:
                prev_vols = volumes[-169:-1]
                avg_vol = (sum(prev_vols) / len(prev_vols)) if prev_vols else None
                volume_ratio = (volumes[-1] / avg_vol) if (avg_vol and avg_vol > 0) else 0.0
            else:
                volume_ratio = 0.0
        else:
            def yf_metrics_sync(t: str) -> tuple[Optional[float], Optional[float], Optional[float], float, float]:
                hist = yf.Ticker(t).history(period="10d", interval="1h", progress=False)
                if hist is None or hist.empty:
                    return None, None, None, 0.0, 0.0
                hist = hist.dropna(subset=["Close", "Volume"])
                closes = [float(x) for x in hist["Close"].tolist()]
                vols = [float(x) for x in hist["Volume"].tolist()]
                rsi_local = compute_rsi(closes, 14) if len(closes) > 14 else None
                if len(closes) >= 169:
                    close_now = closes[-1]
                    close_7d = closes[-169]
                    trend7_local = ((close_now - close_7d) / close_7d) * 100.0 if close_7d else 0.0
                    prev_vols = vols[-169:-1]
                    avg_vol = (sum(prev_vols) / len(prev_vols)) if prev_vols else None
                    vol_ratio_local = (vols[-1] / avg_vol) if (avg_vol and avg_vol > 0) else 0.0
                else:
                    trend7_local = 0.0
                    vol_ratio_local = 0.0
                last_local, pct_local, _ = _yf_last_and_pct_sync(t)
                return last_local, pct_local, rsi_local, vol_ratio_local, trend7_local

            last, pct24, rsi, volume_ratio, trend7 = await asyncio.to_thread(yf_metrics_sync, ticker_for_yf)

        # Safe string metrics for GPT
        last_val = f"{last:.2f}" if last is not None else "N/A"
        pct_val = f"{pct24:+.2f}%" if pct24 is not None else "N/A"
        rsi_val = f"{rsi:.1f}" if rsi is not None else "N/A"
        vol_val = f"{volume_ratio:.2f}x" if volume_ratio is not None else "N/A"
        trend_val = f"{trend7:+.2f}%" if trend7 is not None else "N/A"

        user_metrics = (
            f"current_price={last_val}, 24h_change={pct_val}, "
            f"RSI(14)={rsi_val} ({rsi_state_label(rsi)}), "
            f"volume_ratio={vol_val}, "
            f"7d_trend={trend_val}, Fear&Greed={fng_val} ({fng_label})"
        )

        # Positioning check heuristic
        crowding = "Medium"
        crowding_reason = "Positioning looks balanced."
        trend7_num = trend7 if trend7 is not None else 0.0
        if rsi is not None and rsi > 70:
            crowding = "High"
            crowding_reason = "RSI above 70 suggests crowded longs and unwind risk."
        elif rsi is not None and rsi < 30:
            crowding = "High"
            crowding_reason = "RSI below 30 suggests crowded shorts and squeeze risk."
        if trend7_num > 15:
            crowding = "Extreme"
            crowding_reason = "Asset is up >15% in 7d, so positioning may be crowded."
        elif trend7_num < -15:
            crowding = "High"
            crowding_reason = "Asset is down >15% in 7d, so oversold positioning risk is elevated."

        extra_instruction = (
            f"You must respond in EXACTLY this format (fill values; no extra sections):\n"
            f"📊 ANALYSIS — {display_name}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Price: {last_val}  |  24h: {pct_val}\n"
            f"📈 RSI: {rsi_val} — {rsi_state_label(rsi)}\n"
            f"🔊 Volume: {vol_val} avg\n\n"
            f"🧠 WHAT THE MARKET IS SAYING\n"
            f"{{What price action tells us beyond numbers. Look for divergences.}}\n\n"
            f"📖 THE NARRATIVE\n"
            f"{{What story explains this move. Connect to macro — Fed, geopolitics, dollar, inflation. Who is buying/selling and why RIGHT NOW.}}\n\n"
            f"⚡ THE EDGE\n"
            f"{{What most people are missing about this setup}}\n\n"
            f"❓ WHY IS THIS HAPPENING\n"
            f"{{Connect price action to macro context and recent news. If no clear reason: 'No clear catalyst identified — watch for follow-through volume before drawing conclusions.'}}\n\n"
            f"🎯 LEVELS\n"
            f"Support: $X  |  Resistance: $X\n\n"
            f"🎯 POSITIONING CHECK\n"
            f"Crowding risk: {crowding}\n"
            f"{crowding_reason}\n\n"
            f"📐 TRADE SETUP\n"
            f"Type: {{LONG / SHORT / AVOID}}\n"
            f"Timeframe: {{1-2 weeks / 2-4 weeks}}\n"
            f"Conviction: {{Low / Medium / High}}\n\n"
            f"Entry zone: ${{X}} - ${{X}}\n"
            f"Target: ${{X}} (+X%)\n"
            f"Stop loss: ${{X}} (-X%)\n"
            f"Risk/Reward: 1:{{X}}\n"
            f"Only include TRADE SETUP if R:R >= 1:2; otherwise say AVOID and explain why.\n"
            f"Position size: $50-100 max (1-2% of portfolio)\n"
            f"Confirms thesis: {{specific level or event}}\n"
            f"Kills thesis: {{specific level or event}}\n\n"
            f"💼 FOR YOUR BUDGET\n"
            f"{{1–2 sentences: specific to $1k–$10k beginner}}\n"
            f"Suggested size if entering: $50–$100\n\n"
            f"⚠️ Not financial advice.\n"
            f"(You may exceed the usual 5-sentence limit to complete the required template.)\n"
            f"Do not use curly braces anywhere in the output."
        )

        reply, _, _ = await call_openai_for_text(
            application=context.application,
            state=state,
            user_id=update.effective_user.id,
            user_text=f"Symbol: {display_name}. Metrics: {user_metrics}.",
            extra_user_instruction=extra_instruction,
        )
        if reply:
            await update.message.reply_text(escape_for_html(reply), parse_mode="HTML", disable_web_page_preview=True)
        else:
            await update.message.reply_text(
                "📊 ANALYSIS unavailable right now; required market data is unavailable. Please try again shortly.",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    except Exception as exc:
        log_err(f"/analyse error: {exc}")
        await update.message.reply_text("AI unavailable right now, try again in a minute", parse_mode="HTML")
    finally:
        if created_temp and httpx_client is not None:
            await httpx_client.aclose()


async def cmd_scenario(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /scenario {situation}", parse_mode="HTML")
        return
    situation = " ".join(context.args).strip()
    state = context.application.bot_data.get("state")
    reply, _, _ = await call_openai_for_text(
        application=context.application,
        state=state,
        user_id=update.effective_user.id,
        user_text=situation,
        extra_user_instruction="Think through a what-if with live data. Explain what would likely happen to each of the tracked assets and why, in plain English. You may use more than 5 sentences if needed for clarity.",
    )
    if reply:
        await update.message.reply_text(escape_for_html(reply), parse_mode="HTML", disable_web_page_preview=True)


async def cmd_learn_gpt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /learn {topic}", parse_mode="HTML")
        return
    topic = " ".join(context.args).strip()
    state = context.application.bot_data.get("state")
    reply, _, _ = await call_openai_for_text(
        application=context.application,
        state=state,
        user_id=update.effective_user.id,
        user_text=topic,
        extra_user_instruction="Explain this trading concept simply for a beginner. Max 3–5 sentences. End with one practical example using BTC or ETH.",
    )
    if reply:
        await update.message.reply_text(escape_for_html(reply), parse_mode="HTML", disable_web_page_preview=True)


async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    state = context.application.bot_data.get("state")
    try:
        # Build regime snapshot from available live data.
        live = await fetch_live_market_data(context.application, state)
        # Pull key percent values quickly from parsed live string.
        def _extract_pct(name: str) -> Optional[float]:
            m = re.search(rf"{re.escape(name)}[^\\(]*\\(([+-]?[0-9]+(?:\\.[0-9]+)?)%\\)", live)
            if not m:
                return None
            try:
                return float(m.group(1))
            except Exception:
                return None

        spy_pct = _extract_pct("SPY")
        vix_pct = _extract_pct("VIX")
        btc_pct = _extract_pct("BTC")
        gold_pct = _extract_pct("Gold")
        regime, narrative, divergence, edge = compute_regime(spy_pct, vix_pct, btc_pct, gold_pct)
        regime_block = (
            f"🌐 REGIME SNAPSHOT\n"
            f"Regime: {regime}\n"
            f"Narrative: {narrative}\n"
            f"Divergence: {divergence}\n"
            f"Edge: {edge}"
        )

        reply, _, _ = await call_openai_for_text(
            application=context.application,
            state=state,
            user_id=update.effective_user.id,
            user_text="Review the market right now for all tracked assets.",
            extra_user_instruction=(
                "Give a 1-paragraph market summary (plain English): what looks interesting, what to avoid today, and overall mood. "
                "End with one specific asset to watch today and why. "
                "If any market data is missing, explicitly say 'unavailable' and continue."
            ),
        )
        body = reply if reply else "Market review is partially unavailable right now. Key fields are unavailable."
        full = f"{body}\n\n{regime_block}\n\n⚠️ Not financial advice. Do your own research."
        await update.message.reply_text(escape_for_html(full), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:
        log_err(f"/review error: {exc}")
        await update.message.reply_text(
            "Market review partially unavailable right now; some data is unavailable, but bot is still running.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def cmd_levels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Deterministic proxy levels: blend 24h and 7d highs/lows.
    state = context.application.bot_data.get("state")
    if state is None or not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /levels {symbol}", parse_mode="HTML")
        return
    sym = " ".join(context.args).strip().upper()
    httpx_client: Optional[httpx.AsyncClient] = context.application.bot_data.get("httpx_client")
    if httpx_client is None:
        httpx_client = httpx.AsyncClient()

    crypto_pair = CRYPTO_KEYS_BY_INPUT.get(sym)
    if crypto_pair:
        try:
            # 24h and 7d 15m candles
            kl_24 = await fetch_klines(httpx_client, crypto_pair, "15m", 96)
            kl_7d = await fetch_klines(httpx_client, crypto_pair, "15m", 672)
            low_24 = min(float(k[3]) for k in kl_24 if len(k) > 3)
            high_24 = max(float(k[2]) for k in kl_24 if len(k) > 2)
            low_7 = min(float(k[3]) for k in kl_7d if len(k) > 3)
            high_7 = max(float(k[2]) for k in kl_7d if len(k) > 2)
            support = (low_24 + low_7) / 2.0
            resistance = (high_24 + high_7) / 2.0
            name = CRYPTO_TRACKED.get(crypto_pair, crypto_pair)
            await update.message.reply_text(
                f"🎯 Levels — {name}\nSupport: ${support:,.0f} | Resistance: ${resistance:,.0f}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log_err(f"/levels error (crypto): {exc}")
            await update.message.reply_text("⚠️ Unable to calculate levels right now.", parse_mode="HTML")
        return

    # Stocks/commodities via yfinance
    yf_ticker = STOCK_KEYS_BY_INPUT.get(sym) or COMMOD_KEYS_BY_INPUT.get(sym)
    if not yf_ticker:
        await update.message.reply_text("Unknown symbol. Try BTC, ETH, SOL, BNB, AVAX, SPY, QQQ, Gold, Oil.", parse_mode="HTML")
        return

    def yf_levels_sync(t: str) -> tuple[Optional[float], Optional[float]]:
        hist = yf.Ticker(t).history(period="8d", interval="1h", progress=False)
        if hist is None or hist.empty:
            return None, None
        hist = hist.dropna(subset=["High", "Low"])
        end = hist.index.max()
        start_24 = end - timedelta(hours=24)
        start_7 = end - timedelta(days=7)
        hist_24 = hist[hist.index >= start_24]
        hist_7 = hist[hist.index >= start_7]
        if hist_24.empty or hist_7.empty:
            return None, None
        low_24 = float(hist_24["Low"].min())
        high_24 = float(hist_24["High"].max())
        low_7 = float(hist_7["Low"].min())
        high_7 = float(hist_7["High"].max())
        support = (low_24 + low_7) / 2.0
        resistance = (high_24 + high_7) / 2.0
        return support, resistance

    try:
        support, resistance = await asyncio.to_thread(yf_levels_sync, yf_ticker)
        if support is None or resistance is None:
            await update.message.reply_text("⚠️ Unable to calculate levels right now.", parse_mode="HTML")
            return
        # Display name normalization
        name = "Gold" if yf_ticker == "GLD" else ("Oil" if yf_ticker == "USO" else yf_ticker)
        await update.message.reply_text(
            f"🎯 Levels — {name}\nSupport: ${support:,.0f} | Resistance: ${resistance:,.0f}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        log_err(f"/levels error (yf): {exc}")
        await update.message.reply_text("⚠️ Unable to calculate levels right now.", parse_mode="HTML")


async def cmd_status_live(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    state = context.application.bot_data.get("state")
    live = await fetch_live_market_data(context.application, state)
    # Build a clean status block from direct live fetch string.
    # This is intentionally on-demand so it never depends on cache.
    text = f"📡 LIVE STATUS\n{live}"
    await update.message.reply_text(escape_for_html(text), parse_mode="HTML", disable_web_page_preview=True)


async def prefetch_initial_prices(app: Application, bot_impl_module: Any, state: Any) -> None:
    """
    Prime snapshots synchronously at startup so background caches
    are populated before first command reads.
    """
    httpx_client: Optional[httpx.AsyncClient] = app.bot_data.get("httpx_client")
    created_temp = False
    if httpx_client is None:
        httpx_client = httpx.AsyncClient()
        created_temp = True
    try:
        crypto_data = await fetch_live_binance_24h(httpx_client, list(CRYPTO_TRACKED.keys()))
        if hasattr(state, "crypto_snapshots"):
            for pair, payload in crypto_data.items():
                snap = state.crypto_snapshots.get(pair) if isinstance(state.crypto_snapshots, dict) else None
                if snap is None:
                    snap = bot_impl_module.MarketSnapshot()
                snap.price = payload.get("last")
                snap.move_pct = payload.get("pct")
                snap.confidence = snap.confidence if hasattr(snap, "confidence") else None
                state.crypto_snapshots[pair] = snap

        yf_tickers = ["SPY", "QQQ", "UUP", "GLD", "USO", "GC=F", "CL=F", "SI=F", "^VIX"]
        yf_data = await fetch_live_yf_prices(yf_tickers)
        if hasattr(state, "market_snapshots"):
            for ticker, payload in yf_data.items():
                snap = state.market_snapshots.get(ticker) if isinstance(state.market_snapshots, dict) else None
                if snap is None:
                    snap = bot_impl_module.MarketSnapshot()
                snap.price = payload.get("last")
                snap.move_pct = payload.get("pct")
                state.market_snapshots[ticker] = snap
    except Exception as exc:
        log_err(f"startup prefetch error: {exc}")
    finally:
        if created_temp and httpx_client is not None:
            await httpx_client.aclose()


async def get_symbol_price(application: Application, symbol: str) -> Optional[float]:
    sym = symbol.upper().strip()
    httpx_client: Optional[httpx.AsyncClient] = application.bot_data.get("httpx_client")
    created_temp = False
    if httpx_client is None:
        httpx_client = httpx.AsyncClient()
        created_temp = True
    try:
        # crypto by pair or asset
        if sym in CRYPTO_KEYS_BY_INPUT:
            pair = CRYPTO_KEYS_BY_INPUT[sym]
            data = await fetch_live_binance_24h(httpx_client, [pair])
            px = data.get(pair, {}).get("last")
            if px is not None:
                return float(px)
        # yfinance direct
        yf_ticker = STOCK_KEYS_BY_INPUT.get(sym) or COMMOD_KEYS_BY_INPUT.get(sym) or sym
        last, _, _ = await asyncio.to_thread(_yf_last_and_pct_sync, yf_ticker)
        if last is not None:
            return float(last)
        # coingecko fallback for coin symbols
        _, px, _ = await resolve_coin_price(httpx_client, sym)
        if px is not None:
            return float(px)
        return None
    except Exception:
        return None
    finally:
        if created_temp and httpx_client is not None:
            await httpx_client.aclose()


async def cmd_addsetup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if len(context.args) != 5:
        await update.message.reply_text("Usage: /addsetup {symbol} {direction} {entry} {target} {stop}", parse_mode="HTML")
        return
    symbol, direction, entry_s, target_s, stop_s = context.args
    try:
        entry = float(entry_s)
        target = float(target_s)
        stop = float(stop_s)
    except ValueError:
        await update.message.reply_text("Invalid numbers. Example: /addsetup GOLD LONG 4500 4650 4420", parse_mode="HTML")
        return

    ai_manager: AiManager = context.application.bot_data["ai_manager"]
    setup = {
        "symbol": symbol.upper(),
        "direction": direction.upper(),
        "entry": entry,
        "target": target,
        "stop": stop,
        "created_at": now_utc().isoformat(),
    }
    arr = ai_manager.memory.get("open_setups")
    if not isinstance(arr, list):
        arr = []
    arr.append(setup)
    ai_manager.memory["open_setups"] = arr[-30:]
    ai_manager.persist()
    await update.message.reply_text("✅ Setup added.", parse_mode="HTML")


async def cmd_addposition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /addposition {symbol} {amount} {buy_price}", parse_mode="HTML")
        return
    symbol, amount_s, buy_s = context.args
    try:
        amount = float(amount_s)
        buy_price = float(buy_s)
    except ValueError:
        await update.message.reply_text("Invalid numbers.", parse_mode="HTML")
        return
    ai_manager: AiManager = context.application.bot_data["ai_manager"]
    portfolio = ai_manager.portfolio
    positions = portfolio.get("positions", [])
    if not isinstance(positions, list):
        positions = []
    positions.append({"symbol": symbol.upper(), "amount": amount, "buy_price": buy_price})
    portfolio["positions"] = positions
    # reduce cash from budget by invested amount
    invested = amount * buy_price
    portfolio["cash_remaining"] = float(portfolio.get("cash_remaining", 0.0)) - invested
    ai_manager.persist()
    await update.message.reply_text("✅ Position added.", parse_mode="HTML")


async def cmd_removeposition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /removeposition {symbol}", parse_mode="HTML")
        return
    symbol = context.args[0].upper()
    ai_manager: AiManager = context.application.bot_data["ai_manager"]
    portfolio = ai_manager.portfolio
    positions = portfolio.get("positions", [])
    if not isinstance(positions, list):
        positions = []
    remaining = [p for p in positions if str(p.get("symbol", "")).upper() != symbol]
    removed = len(positions) - len(remaining)
    portfolio["positions"] = remaining
    ai_manager.persist()
    await update.message.reply_text(f"Removed {removed} position(s) for {symbol}.", parse_mode="HTML")


async def cmd_setbudget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /setbudget {amount}", parse_mode="HTML")
        return
    try:
        amount = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid amount.", parse_mode="HTML")
        return
    ai_manager: AiManager = context.application.bot_data["ai_manager"]
    ai_manager.portfolio["total_budget"] = amount
    ai_manager.portfolio["cash_remaining"] = amount
    ai_manager.persist()
    await update.message.reply_text(f"✅ Budget set to ${amount:,.2f}.", parse_mode="HTML")


async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    ai_manager: AiManager = context.application.bot_data["ai_manager"]
    portfolio = ai_manager.portfolio
    positions = portfolio.get("positions", []) if isinstance(portfolio.get("positions"), list) else []
    price_map: dict[str, float] = {}
    for p in positions:
        sym = str(p.get("symbol", "")).upper()
        px = await get_symbol_price(context.application, sym)
        if px is not None:
            price_map[sym] = px
    text = portfolio_snapshot_text(portfolio, price_map)
    await update.message.reply_text(escape_for_html(text), parse_mode="HTML", disable_web_page_preview=True)


async def startup_online_message_task(app: Application) -> None:
    await asyncio.sleep(30)
    now_sgt = now_utc().astimezone(timezone(timedelta(hours=8)))
    text = (
        "🤖 BOT ONLINE\n"
        f"{now_sgt.strftime('%Y-%m-%d %H:%M:%S')} SGT\n"
        "All modules running:\n"
        "✅ Price monitor\n"
        "✅ News monitor\n"
        "✅ DEX monitor\n"
        "✅ Sentiment monitor\n"
        "✅ AI chat ready\n\n"
        "Next daily briefing: 8:00 AM SGT\n"
        "Type /status to see current prices."
    )
    try:
        await app.bot.send_message(chat_id=os.getenv("TELEGRAM_CHAT_ID", ""), text=text, parse_mode="HTML")
    except Exception as exc:
        log_err(f"startup message failed: {exc}")


async def portfolio_alerts_task(app: Application) -> None:
    while True:
        await asyncio.sleep(1800)
        ai_manager: AiManager = app.bot_data.get("ai_manager")
        if not ai_manager:
            continue
        positions = ai_manager.portfolio.get("positions", [])
        if not isinstance(positions, list):
            continue
        today = now_utc().astimezone(timezone(timedelta(hours=8))).date()
        for p in positions:
            sym = str(p.get("symbol", "")).upper()
            key = f"{sym}:{today.isoformat()}"
            if key in ai_manager.position_alert_sent:
                continue
            try:
                buy = float(p.get("buy_price", 0.0))
                cur = await get_symbol_price(app, sym)
                if cur is None or buy <= 0:
                    continue
                pct = ((cur - buy) / buy) * 100.0
                if pct <= -15:
                    await app.bot.send_message(chat_id=os.getenv("TELEGRAM_CHAT_ID", ""), text=f"⚠️ Portfolio alert: {sym} is down {pct:.1f}% from entry.")
                    ai_manager.position_alert_sent[key] = today
                elif pct >= 20:
                    await app.bot.send_message(chat_id=os.getenv("TELEGRAM_CHAT_ID", ""), text=f"✅ Take-profit alert: {sym} is up {pct:.1f}% from entry.")
                    ai_manager.position_alert_sent[key] = today
            except Exception as exc:
                log_err(f"portfolio alert error {sym}: {exc}")


async def _build_digest(app: Application, state: Any, is_morning: bool) -> str:
    """Build the full digest text for morning (8 AM) or evening (8 PM) SGT."""
    ai_manager: Optional[AiManager] = app.bot_data.get("ai_manager")
    live = await fetch_live_market_data(app, state)

    def _extract_pct(name: str) -> Optional[float]:
        m = re.search(rf"{re.escape(name)}[^\(]*\(([+-]?[0-9]+(?:\.[0-9]+)?)%\)", live)
        return float(m.group(1)) if m else None

    regime, narrative, divergence, edge = compute_regime(
        _extract_pct("SPY"), _extract_pct("VIX"), _extract_pct("BTC"), _extract_pct("Gold")
    )

    session_label = "☀️ MORNING BRIEF" if is_morning else "🌙 EVENING BRIEF"
    sgt_now = now_utc().astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M SGT")

    lines = [
        f"{session_label} — {sgt_now}",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "🌐 MARKET REGIME",
        f"Regime: {regime}",
        f"Story: {narrative}",
        f"Divergence: {divergence}",
        f"Edge: {edge}",
        "",
        "📊 LIVE SNAPSHOT",
        live,
        "",
        "📋 OPEN SETUPS",
    ]

    setups = (ai_manager.memory.get("open_setups", []) if ai_manager else []) or []
    if not isinstance(setups, list) or not setups:
        lines.append("No open setups.")
    else:
        for i, s in enumerate(setups, 1):
            sym = str(s.get("symbol", "N/A")).upper()
            direction = str(s.get("direction", "N/A")).upper()
            entry = float(s.get("entry", 0.0))
            target_px = float(s.get("target", 0.0))
            stop_px = float(s.get("stop", 0.0))
            current = await get_symbol_price(app, sym)
            if current is None:
                current = entry
            met = "✅ Met" if ((direction == "LONG" and current <= entry) or (direction == "SHORT" and current >= entry)) else "⏳ Pending"
            pnl = ((current - entry) / entry * 100.0) if entry else 0.0
            status = "IN PLAY"
            if direction == "LONG" and current >= target_px:
                status = "🎯 TARGET HIT"
            elif direction == "LONG" and current <= stop_px:
                status = "🛑 STOPPED OUT"
            elif direction == "SHORT" and current <= target_px:
                status = "🎯 TARGET HIT"
            elif direction == "SHORT" and current >= stop_px:
                status = "🛑 STOPPED OUT"
            lines.extend([
                f"SETUP-{i} — {sym} {direction}",
                f"Entry: ${entry:,.2f} {met}",
                f"Now: ${current:,.2f} | Target: ${target_px:,.2f} | Stop: ${stop_px:,.2f}",
                f"P&L if entered: {pnl:+.2f}% | Status: {status}",
            ])

    # GPT-powered entry + sizing section
    if ai_manager:
        ok, _ = ai_manager.can_call()
        if ok and ai_manager.client:
            ai_manager.mark_called()
            session_ctx = "market open — focus on entry opportunities" if is_morning else "market close — focus on review and overnight watch"
            gpt_prompt = (
                f"You are briefing a beginner crypto trader. Session: {session_ctx}.\n"
                f"Current market data: {live}\n"
                f"Regime: {regime}. {narrative}\n\n"
                "Provide EXACTLY this structure (fill in values, no extra text, no curly braces):\n\n"
                "🎯 TOP OPPORTUNITY TODAY\n"
                "Asset: [name]  |  Direction: [LONG/SHORT/WAIT]\n"
                "Entry zone: $X–$X\n"
                "Target: $X (+X%)  |  Stop: $X (-X%)\n"
                "R:R: 1:X\n"
                "Position size: $50–$100\n"
                "Why now: [1 sentence — macro reason + technical confirmation needed]\n\n"
                "📉 RISK TO WATCH\n"
                "[1 sentence — the biggest macro/technical risk that could wreck this setup]\n\n"
                "💡 LEARNING MOMENT\n"
                "[1 sentence — one trading mechanic this setup teaches, e.g. 'volume confirmation', 'support flip']\n\n"
                "Only recommend if R:R >= 1:2. If no clean setup exists, say 'No clean setup — stay in cash until X confirms.'\n"
                "⚠️ Not financial advice."
            )
            try:
                resp = await asyncio.to_thread(
                    ai_manager.client.chat.completions.create,
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": gpt_prompt}],
                    max_tokens=400,
                )
                gpt_section = (resp.choices[0].message.content or "").strip()
                if gpt_section:
                    lines.append("")
                    lines.append(gpt_section)
            except Exception as exc:
                log_err(f"digest gpt error: {exc}")

    return "\n".join(lines)


async def daily_open_setups_task(app: Application) -> None:
    """Fires at 8 AM and 8 PM SGT every day with a full AI-powered digest."""
    tz = timezone(timedelta(hours=8))
    digest_hours = [8, 20]  # 8 AM and 8 PM SGT

    while True:
        now_sgt = now_utc().astimezone(tz)
        # Find next digest time
        next_target = None
        for h in sorted(digest_hours):
            candidate = now_sgt.replace(hour=h, minute=0, second=0, microsecond=0)
            if candidate > now_sgt:
                next_target = candidate
                break
        if next_target is None:
            # All today's digests passed — schedule for first tomorrow
            next_target = now_sgt.replace(hour=digest_hours[0], minute=0, second=0, microsecond=0) + timedelta(days=1)

        await asyncio.sleep(max(1, int((next_target - now_sgt).total_seconds())))

        ai_manager: Optional[AiManager] = app.bot_data.get("ai_manager")
        state = app.bot_data.get("state")
        if not ai_manager:
            continue

        is_morning = (next_target.hour == 8)
        try:
            digest_text = await _build_digest(app, state, is_morning=is_morning)
            await app.bot.send_message(
                chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
                text=digest_text,
                parse_mode="HTML",
            )
        except Exception as exc:
            log_err(f"digest send failed: {exc}")


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all active config values (overrides + env vars)."""
    from optimizer_agent import get_all_active_config, _load_overrides
    if not update.message:
        return
    active = get_all_active_config()
    overrides = _load_overrides()
    active_keys = set(overrides.get("active", {}).keys())
    lines = ["⚙️ ACTIVE CONFIG", "━━━━━━━━━━━━━━━━━━━━━━━"]
    for k, v in active.items():
        tag = " 🔧 (override)" if k in active_keys else ""
        lines.append(f"{k} = {v}{tag}")
    pending = overrides.get("pending", [])
    if pending:
        lines.append("")
        lines.append(f"⏳ {len(pending)} pending recommendation(s) — use /approve N or /reject N")
    lines.append("")
    lines.append("Use /resetconfig PARAM to revert any override to default.")
    await update.message.reply_text("\n".join(lines))


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/approve N — apply the Nth pending optimizer recommendation."""
    from optimizer_agent import approve_recommendation, get_pending_recommendations
    if not update.message:
        return
    if not context.args:
        pending = get_pending_recommendations()
        if not pending:
            await update.message.reply_text("No pending recommendations. Run /config to check.")
            return
        lines = ["⏳ PENDING RECOMMENDATIONS"]
        for i, r in enumerate(pending, 1):
            lines.append(f"[{i}] {r['parameter']}: {r['current_value']} → {r['suggested_value']}")
            lines.append(f"     {r['reason']}")
        lines.append("\nUse /approve N to apply or /reject N to dismiss.")
        await update.message.reply_text("\n".join(lines))
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /approve 1")
        return
    rec = approve_recommendation(idx)
    if rec is None:
        await update.message.reply_text(f"No recommendation #{idx}. Use /approve to see the list.")
        return
    await update.message.reply_text(
        f"✅ Applied: {rec['parameter']} = {rec['suggested_value']}\n"
        f"Reason: {rec['reason']}\n"
        f"Takes effect immediately — no restart needed."
    )


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reject N — dismiss the Nth pending optimizer recommendation."""
    from optimizer_agent import reject_recommendation
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /reject 1")
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /reject 1")
        return
    rec = reject_recommendation(idx)
    if rec is None:
        await update.message.reply_text(f"No recommendation #{idx}.")
        return
    await update.message.reply_text(
        f"❌ Rejected: {rec['parameter']} stays at {rec['current_value']}."
    )


async def cmd_resetconfig(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/resetconfig PARAM — revert an override back to env var / default."""
    from optimizer_agent import reset_override, get_all_active_config
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /resetconfig RSI_HIGH")
        return
    param = context.args[0].strip().upper()
    removed = reset_override(param)
    if removed:
        new_val = get_all_active_config().get(param, "unknown")
        await update.message.reply_text(
            f"↩️ {param} override removed. Now using: {new_val} (from env/default)."
        )
    else:
        await update.message.reply_text(f"No active override for {param}.")


def run_ai_bot() -> None:
    import bot_impl

    try:
        config = bot_impl.Config.from_env()
    except Exception as exc:
        log_err(f"config error: {exc}")
        raise SystemExit(1)

    state = bot_impl.BotState(config=config)
    app = bot_impl.build_app(state)

    # Remove bot_impl handlers we replace locally.
    for group, handlers in list(app.handlers.items()):
        for handler in list(handlers):
            cb = getattr(handler, "callback", None)
            if cb in {bot_impl.cmd_learn, bot_impl.cmd_levels, bot_impl.cmd_status}:
                app.remove_handler(handler, group=group)

    # AI manager stored in app.bot_data for handlers.
    ai_key = os.getenv("OPENAI_API_KEY", "").strip()
    app.bot_data["ai_manager"] = AiManager(ai_key)

    # Skills layer
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("status", cmd_status_live))
    app.add_handler(CommandHandler("analyse", cmd_analyse))
    app.add_handler(CommandHandler("scenario", cmd_scenario))
    app.add_handler(CommandHandler("learn", cmd_learn_gpt))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("levels", cmd_levels))
    app.add_handler(CommandHandler("addsetup", cmd_addsetup))
    app.add_handler(CommandHandler("addposition", cmd_addposition))
    app.add_handler(CommandHandler("removeposition", cmd_removeposition))
    app.add_handler(CommandHandler("setbudget", cmd_setbudget))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    # Optimizer config commands
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("resetconfig", cmd_resetconfig))

    # Free chat: any non-command text goes to GPT.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_chat))

    # Keep existing alert modules/tasks + start AI helper tasks.
    async def post_init_with_extras(a: Application) -> None:
        # Prime prices first so cache-backed modules/commands are ready.
        await prefetch_initial_prices(a, bot_impl, state)
        await bot_impl.on_startup(a)
        from optimizer_agent import weekly_optimizer_task
        extra_tasks = [
            asyncio.create_task(startup_online_message_task(a), name="startup_online_message_task"),
            asyncio.create_task(portfolio_alerts_task(a), name="portfolio_alerts_task"),
            asyncio.create_task(daily_open_setups_task(a), name="daily_open_setups_task"),
            asyncio.create_task(weekly_optimizer_task(a), name="weekly_optimizer_task"),
        ]
        a.bot_data["ai_extra_tasks"] = extra_tasks

    async def post_shutdown_with_extras(a: Application) -> None:
        tasks = a.bot_data.get("ai_extra_tasks", [])
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        ai_manager: Optional[AiManager] = a.bot_data.get("ai_manager")
        if ai_manager:
            ai_manager.persist()
        await bot_impl.on_shutdown(a)

    app.post_init = post_init_with_extras
    app.post_shutdown = post_shutdown_with_extras
    app.run_polling(close_loop=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run_ai_bot()
