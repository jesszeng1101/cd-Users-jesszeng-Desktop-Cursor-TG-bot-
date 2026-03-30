import asyncio
import hashlib
import json
import logging
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import feedparser
import httpx
import yfinance as yf
import websockets
from app_constants import (
    DEFAULT_CRITICAL_ALERTS_PER_DAY,
    DEFAULT_EMERGENCY_MOVE_PCT,
    DEFAULT_SUGGESTED_SIZE_TEXT,
    KLINES_15M_7D,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_PERIOD,
    SECONDS_1_HOUR,
    SECONDS_1_MIN,
    SECONDS_5_MIN,
    SECONDS_6_HOURS,
    SECONDS_15_MIN,
    VOLUME_SPIKE_MULTIPLIER,
)
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


UTC = timezone.utc
SGT = timezone(timedelta(hours=8), name="SGT")
ET = ZoneInfo("America/New_York")


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def log_err(msg: str) -> None:
    print(f"[{now_utc().isoformat()}] {msg}", file=sys.stderr, flush=True)


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def format_usd(x: float) -> str:
    if x is None or not isinstance(x, (int, float)):
        return "N/A"
    return f"${x:,.0f}"


def time_ago(dt: datetime) -> str:
    if not isinstance(dt, datetime):
        return "unknown"
    delta = now_utc() - dt.astimezone(UTC)
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def is_silent_hours_sgt(now_sgt: datetime) -> bool:
    h = now_sgt.hour
    # 11PM–7AM SGT
    return h >= 23 or h < 7


def market_is_open_et(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    t = now_et.time()
    # 9:30AM–4:00PM ET
    return t >= datetime.strptime("09:30", "%H:%M").time() and t <= datetime.strptime("16:00", "%H:%M").time()


def next_market_open_et(now_et: datetime) -> datetime:
    # Find next weekday morning 09:30 ET.
    days_ahead = 0
    while True:
        candidate = (now_et + timedelta(days=days_ahead)).replace(hour=9, minute=30, second=0, microsecond=0)
        if candidate.weekday() < 5:
            if candidate > now_et:
                return candidate
        days_ahead += 1


def compute_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
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


class TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, datetime] = {}

    def contains(self, key: str) -> bool:
        exp = self._store.get(key)
        if exp is None:
            return False
        if exp < now_utc():
            self._store.pop(key, None)
            return False
        return True

    def add(self, key: str, ttl_seconds: int) -> None:
        self._store[key] = now_utc() + timedelta(seconds=ttl_seconds)

    def cleanup(self) -> None:
        current = now_utc()
        expired = [k for k, v in self._store.items() if v < current]
        for k in expired:
            self._store.pop(k, None)


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

BINANCE_WS_PAIRS = ["BTCUSDT", "ETHUSDT", "AVAXUSDT"]
EXTERNAL_CRYPTO_PAIRS = ["MNTUSDT"]
CRYPTO_PAIRS = BINANCE_WS_PAIRS + EXTERNAL_CRYPTO_PAIRS
CRYPTO_DISPLAY = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "AVAXUSDT": "AVAX",
    "MNTUSDT": "MNT",
}

# yfinance tickers
EQUITY_INDICES = {
    "SPY": "S&P 500",
    "QQQ": "Nasdaq",
    "GLD": "Gold (ETF)",
    "USO": "Oil (ETF)",
    "UUP": "DXY",
}

COMMODITIES = {
    "GC=F": "Gold futures",
    "CL=F": "Crude Oil",
    "SI=F": "Silver",
}


def build_direction_from_keywords(matched: set[str]) -> Optional[str]:
    # Used for daily briefing (bullish/bearish). For confidence scoring, we only need "relevant in last 2h".
    bullish = {"ETF", "listing", "partnership", "BlackRock"}
    bearish = {"SEC", "hack", "exploit", "delisting", "liquidation", "sanctions", "ban", "Fed", "CPI"}
    lowered = {m.lower() for m in matched}
    bullish_hit = any(k.lower() in lowered for k in bullish)
    bearish_hit = any(k.lower() in lowered for k in bearish)
    if bullish_hit and not bearish_hit:
        return "BULLISH"
    if bearish_hit and not bullish_hit:
        return "BEARISH"
    if bullish_hit and bearish_hit:
        return "MIXED"
    return None


def detect_affected_assets(title: str) -> list[str]:
    t = title.lower()
    affected: list[str] = []
    mapping = {
        "btc": "BTC",
        "bitcoin": "BTC",
        "eth": "ETH",
        "ethereum": "ETH",
        "sol": "SOL",
        "solana": "SOL",
        "bnb": "BNB",
        "avalanche": "AVAX",
        "avax": "AVAX",
        "gold": "GLD",
        "xaut": "GLD",
    }
    for key, name in mapping.items():
        if key in t and name not in affected:
            affected.append(name)
    return affected


def classify_fear_greed(value: int) -> str:
    if value <= 20:
        return "EXTREME FEAR"
    if value <= 45:
        return "FEAR"
    if value <= 55:
        return "NEUTRAL"
    if value <= 80:
        return "GREED"
    return "EXTREME GREED"


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    price_move_pct: float
    alert_pause_hours: int
    fear_low: int
    greed_high: int

    price_emergency_pct: float = DEFAULT_EMERGENCY_MOVE_PCT
    news_dedup_ttl_seconds: int = SECONDS_6_HOURS
    critical_max_per_day: int = DEFAULT_CRITICAL_ALERTS_PER_DAY

    @staticmethod
    def from_env() -> "Config":
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
        return Config(
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            price_move_pct=float(os.getenv("PRICE_MOVE_PCT", "2.0")),
            alert_pause_hours=2,
            fear_low=int(os.getenv("FEAR_LOW", "25")),
            greed_high=int(os.getenv("GREED_HIGH", "75")),
        )


@dataclass
class NewsEvent:
    ts: datetime
    url: str
    title: str
    matched_keywords: set[str]
    direction: Optional[str]  # BULLISH/BEARISH/MIXED
    affected_assets: list[str]


@dataclass
class MarketSnapshot:
    price: Optional[float] = None
    move_pct: Optional[float] = None  # move over latest timeframe
    rsi: Optional[float] = None
    volume_ratio: Optional[float] = None
    confidence: Optional[int] = None
    rsi_state: str = "Neutral"


@dataclass
class CriticalSlot:
    message_id: int
    asset_key: str
    name: str
    score: int
    move_strength: float
    combined_rank: float
    last_update_ts: datetime


@dataclass
class BotState:
    config: Config
    paused_until: Optional[datetime] = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    dedup_news: TTLCache = field(default_factory=TTLCache)
    alert_dedup: TTLCache = field(default_factory=TTLCache)
    candle_alert_dedup: TTLCache = field(default_factory=TTLCache)
    recent_news: list[NewsEvent] = field(default_factory=list)  # prune by age

    fng_value: Optional[int] = None
    fng_label: Optional[str] = None

    crypto_snapshots: dict[str, MarketSnapshot] = field(default_factory=dict)  # pair -> snapshot
    market_snapshots: dict[str, MarketSnapshot] = field(default_factory=dict)  # yfinance ticker -> snapshot

    # Crypto volume RSI caches
    crypto_volumes_15m: dict[str, list[float]] = field(default_factory=dict)  # 7d window
    crypto_avg_vol_15m: dict[str, Optional[float]] = field(default_factory=dict)
    crypto_rsi_hour_index: dict[str, Optional[int]] = field(default_factory=dict)
    crypto_rsi_value_cache: dict[str, Optional[float]] = field(default_factory=dict)

    # Daily briefing & critical limits (SGT date)
    daily_briefing_sent_for: Optional[datetime.date] = None
    critical_slots: list[CriticalSlot] = field(default_factory=list)
    critical_slots_for: Optional[datetime.date] = None

    def is_paused(self) -> bool:
        return self.paused_until is not None and now_utc() < self.paused_until

    def reset_daily_if_needed(self, now_sgt: datetime) -> None:
        if self.critical_slots_for != now_sgt.date():
            self.critical_slots_for = now_sgt.date()
            self.critical_slots = []


def already_seen(state: BotState, url: str, ttl_seconds: int) -> bool:
    if state.dedup_news.contains(url):
        return True
    state.dedup_news.add(url, ttl_seconds=ttl_seconds)
    return False


async def send_message_html(app: Application, state: BotState, text: str) -> None:
    if state.config.telegram_chat_id is None or state.is_paused():
        return None
    # Deduplicate identical alert texts within a short TTL window to prevent repeats.
    # This addresses repeated WS ticks / accidental re-triggers.
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        dedup_key = f"alert:{digest}"
        if state.alert_dedup.contains(dedup_key):
            log_err(f"alert_dedup: suppressed duplicate {dedup_key[-8:]}")
            return None
        # Use a conservative TTL: enough to suppress duplicates, but short enough
        # that meaningful new events can still alert.
        state.alert_dedup.add(dedup_key, ttl_seconds=SECONDS_15_MIN)
    except Exception:
        pass
    try:
        msg = await app.bot.send_message(
            chat_id=state.config.telegram_chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return msg.message_id
    except Exception as exc:
        log_err(f"telegram send failed: {exc}")
        return None


async def edit_message_html(app: Application, state: BotState, message_id: int, text: str) -> None:
    if state.is_paused():
        return
    try:
        await app.bot.edit_message_text(
            chat_id=state.config.telegram_chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        log_err(f"telegram edit failed: {exc}")


def rsi_state_label(rsi: Optional[float]) -> str:
    if rsi is None:
        return "Neutral"
    if rsi < RSI_OVERSOLD:
        return "Oversold"
    if rsi > RSI_OVERBOUGHT:
        return "Overbought"
    return "Neutral"


def compute_confidence(
    state: BotState,
    move_pct: Optional[float],
    rsi: Optional[float],
    volume_ratio: Optional[float],
    now: datetime,
) -> int:
    if move_pct is None or rsi is None or volume_ratio is None:
        # Still return something, but score won't reach 3 without metrics.
        return 0

    score = 0
    if abs(move_pct) >= state.config.price_move_pct:
        score += 1
    if rsi < RSI_OVERSOLD or rsi > RSI_OVERBOUGHT:
        score += 1
    if volume_ratio >= VOLUME_SPIKE_MULTIPLIER:
        score += 1

    # Sentiment confirms direction
    if state.fng_value is not None:
        bullish = state.fng_value <= state.config.fear_low
        bearish = state.fng_value >= state.config.greed_high
        if move_pct > 0 and bullish:
            score += 1
        elif move_pct < 0 and bearish:
            score += 1

    # News confirmation (relevant in last 2 hours)
    cutoff = now - timedelta(hours=2)
    if any(ev.ts >= cutoff for ev in state.recent_news):
        score += 1

    return score


def score_rank_tuple(score: int, move_strength: float) -> tuple:
    # Higher score wins; tie-breaker uses move size.
    return (score, move_strength)


def suggest_action(
    asset_name: str,
    move_pct: float,
    rsi: Optional[float],
    sentiment_bullish: Optional[bool],
    sentiment_bearish: Optional[bool],
) -> tuple[str, str]:
    # Returns (action, reason)
    rsi_label = rsi_state_label(rsi)
    bullish = sentiment_bullish is True
    bearish = sentiment_bearish is True

    if rsi_label == "Oversold" and move_pct < 0:
        if bullish:
            return (
                "CONSIDER ENTRY",
                "With fear supportive, oversold dips can bounce—plan a small entry and wait for confirmation.",
            )
        return (
            "WATCH",
            "RSI looks stretched, but sentiment isn’t clearly supportive—wait for a small bounce first.",
        )

    if rsi_label == "Overbought" and move_pct > 0:
        if bearish:
            return (
                "CONSIDER EXIT",
                "If sentiment is risk-off, overheated price often snaps back, so consider exiting or reducing risk.",
            )
        return (
            "AVOID",
            "Overbought moves often retrace fast, so avoid chasing and wait for a pullback.",
        )

    # Default: watch/consider depending on direction and RSI extremes
    if move_pct < 0 and rsi_label == "Neutral":
        return (
            "AVOID",
            "Downside is present, but RSI isn’t extreme, so don’t rush into trades in choppy conditions.",
        )
    if move_pct > 0 and rsi_label == "Neutral":
        return (
            "WATCH",
            "Momentum looks positive, but it’s not extreme, so wait for confirmation instead of guessing.",
        )

    return (
        "WATCH",
        "This looks meaningful, but it isn’t fully extreme—use it as a checklist, not a guarantee.",
    )


def build_critical_alert_text(
    asset_name: str,
    price: float,
    move_pct: float,
    timeframe: str,
    rsi: Optional[float],
    volume_ratio: float,
    confidence: int,
    whats_happening: str,
    action: str,
    action_reason: str,
    suggested_size_line: Optional[str],
    also_watch: str,
) -> str:
    rsi_label = rsi_state_label(rsi)
    rsi_val = f"{rsi:.1f}" if rsi is not None else "N/A"
    move_sign = "+" if move_pct >= 0 else ""
    suggested_size = suggested_size_line if suggested_size_line else ""

    return (
        "⚡ CRITICAL ALERT\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Asset: {escape_html(asset_name)}\n"
        f"📍 Price: {format_usd(price)}  |  Move: {move_sign}{move_pct:.1f}% in {escape_html(timeframe)}\n"
        f"📊 RSI: {escape_html(rsi_val)} — {escape_html(rsi_label)}\n"
        f"🔊 Volume: {volume_ratio:.1f}× average\n"
        f"🎯 Confidence: {confidence}/5\n"
        "\n"
        "🧠 WHAT'S HAPPENING\n"
        f"{escape_html(whats_happening)}\n"
        "\n"
        "💼 SUGGESTED ACTION\n"
        f"{escape_html(action)}\n"
        f"→ {escape_html(action_reason)}\n"
        + (
            f"→ If entering: suggested position size = {escape_html(suggested_size)}\n"
            if suggested_size
            else ""
        )
        + f"🪙 ALSO WATCH: {escape_html(also_watch)}\n"
        "\n"
        "📌 Not financial advice. Do your own research."
    )


def choose_crypto_related_assets(asset_key: str) -> str:
    # Simple beginner-friendly correlations.
    if asset_key == "BTCUSDT":
        return "ETH, SOL, BNB"
    if asset_key == "ETHUSDT":
        return "BTC, SOL, AVAX"
    if asset_key == "SOLUSDT":
        return "BTC, ETH, AVAX"
    if asset_key == "BNBUSDT":
        return "BTC, ETH, AVAX"
    if asset_key == "AVAXUSDT":
        return "BTC, ETH, SOL"
    return "BTC, ETH, SOL"


def choose_market_related_assets(asset_display: str) -> str:
    # Beginner cross-check: keep it simple.
    if "Gold" in asset_display and "ETF" in asset_display:
        return "Oil (USO), DXY (UUP), S&P 500 (SPY)"
    if "Oil" in asset_display:
        return "Gold (GLD), DXY (UUP), Nasdaq (QQQ)"
    if asset_display == "DXY":
        return "Gold (GLD), Oil (USO), Crypto (BTC/ETH)"
    if asset_display in {"Gold futures", "Crude Oil", "Silver"}:
        return "Gold (GLD), Oil (USO), DXY (UUP)"
    return "S&P 500 (SPY), Nasdaq (QQQ), DXY (UUP)"


async def fetch_klines_15m(client: httpx.AsyncClient, symbol: str, limit: int) -> list[list[Any]]:
    params = {"symbol": symbol, "interval": "15m", "limit": limit}
    resp = await client.get("https://api.binance.com/api/v3/klines", params=params, timeout=25.0)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


async def fetch_klines_1h(client: httpx.AsyncClient, symbol: str, limit: int) -> list[list[Any]]:
    params = {"symbol": symbol, "interval": "1h", "limit": limit}
    resp = await client.get("https://api.binance.com/api/v3/klines", params=params, timeout=25.0)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


async def fetch_bybit_klines(symbol: str, interval: str, limit: int = 200) -> list[list[Any]]:
    """
    Bybit kline response list item:
    [startTime, open, high, low, close, volume, turnover]
    Returns ascending-by-time lists.
    """
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "spot", "symbol": symbol, "interval": interval, "limit": limit}
    async with httpx.AsyncClient() as c:
        r = await c.get(url, params=params, timeout=20.0)
        r.raise_for_status()
        payload = r.json()
    raw = (payload.get("result") or {}).get("list") or []
    # Bybit returns reverse chronological order; normalize ascending.
    rows = list(reversed(raw))
    return rows


async def prime_crypto_15m_volume_avgs(state: BotState, client: httpx.AsyncClient) -> None:
    # Uses 7 days of 15m candles to compute average volume baseline.
    # 7 days * 24h * 4 candles/hour = ~672
    for pair in CRYPTO_PAIRS:
        try:
            klines = await fetch_klines_15m(client, pair, limit=KLINES_15M_7D)
            volumes = []
            for k in klines:
                try:
                    volumes.append(float(k[5]))
                except Exception:
                    pass
            if volumes:
                state.crypto_volumes_15m[pair] = volumes[-KLINES_15M_7D:]
                state.crypto_avg_vol_15m[pair] = sum(state.crypto_volumes_15m[pair]) / len(state.crypto_volumes_15m[pair])
            else:
                state.crypto_avg_vol_15m[pair] = None
        except Exception as exc:
            log_err(f"prime volume error {pair}: {exc}")


async def update_crypto_rsi_if_needed(state: BotState, client: httpx.AsyncClient, pair: str, hour_index: int) -> None:
    last_index = state.crypto_rsi_hour_index.get(pair)
    if last_index == hour_index and state.crypto_rsi_value_cache.get(pair) is not None:
        return
    klines_1h = await fetch_klines_1h(client, pair, limit=200)
    closes = [float(k[4]) for k in klines_1h if len(k) > 4]
    rsi = compute_rsi(closes, RSI_PERIOD)
    state.crypto_rsi_hour_index[pair] = hour_index
    state.crypto_rsi_value_cache[pair] = rsi


async def crypto_price_monitor(app: Application, state: BotState, client: httpx.AsyncClient) -> None:
    log = logging.getLogger(__name__)
    while not state.stop_event.is_set():
        try:
            await prime_crypto_15m_volume_avgs(state, client)
            streams = "/".join([f"{p.lower()}@kline_15m" for p in BINANCE_WS_PAIRS])
            ws_url = f"wss://stream.binance.com:9443/stream?streams={streams}"
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                # Minimal startup log once connected.
                log.info("price_monitor connected to Binance WS")
                async for raw in ws:
                    if state.stop_event.is_set():
                        break
                    try:
                        payload = json.loads(raw)
                        k = payload.get("data", {}).get("k", {})
                        pair = k.get("s")
                        if pair not in BINANCE_WS_PAIRS:
                            continue

                        is_closed = bool(k.get("x"))
                        if not is_closed:
                            continue

                        open_p = float(k.get("o", 0.0))
                        close_p = float(k.get("c", 0.0))
                        volume = float(k.get("v", 0.0))
                        if open_p <= 0:
                            continue

                        move_pct = ((close_p - open_p) / open_p) * 100.0
                        # RSI from 1h close (cached by hour index)
                        close_time_ms = int(k.get("T") or 0)
                        hour_index = close_time_ms // 3600000 if close_time_ms else int(now_utc().timestamp() // 3600)
                        await update_crypto_rsi_if_needed(state, client, pair, hour_index)
                        rsi = state.crypto_rsi_value_cache.get(pair)

                        # Use the pre-current candle average for volume-ratio.
                        avg_vol = state.crypto_avg_vol_15m.get(pair)
                        volume_ratio = (volume / avg_vol) if (avg_vol and avg_vol > 0) else 0.0

                        # Update baseline window with the latest bar (for next ticks).
                        if pair in state.crypto_volumes_15m and state.crypto_volumes_15m[pair]:
                            state.crypto_volumes_15m[pair].append(volume)
                            state.crypto_volumes_15m[pair] = state.crypto_volumes_15m[pair][-KLINES_15M_7D:]
                            avg_vol_next = sum(state.crypto_volumes_15m[pair]) / len(state.crypto_volumes_15m[pair])
                            state.crypto_avg_vol_15m[pair] = avg_vol_next

                        snapshot = state.crypto_snapshots.get(pair) or MarketSnapshot()
                        snapshot.price = close_p
                        snapshot.move_pct = move_pct
                        snapshot.rsi = rsi
                        snapshot.volume_ratio = volume_ratio
                        snapshot.rsi_state = rsi_state_label(rsi)

                        now = now_utc()
                        confidence = compute_confidence(state, move_pct, rsi, volume_ratio, now)
                        snapshot.confidence = confidence
                        state.crypto_snapshots[pair] = snapshot

                        now_sgt = now.astimezone(SGT)
                        state.reset_daily_if_needed(now_sgt)

                        # Silent hours suppression
                        silent = is_silent_hours_sgt(now_sgt)
                        emergency = abs(move_pct) >= state.config.price_emergency_pct
                        if silent and not emergency:
                            continue
                        if state.is_paused():
                            continue

                        # Emergency alerts should still reach the “score>=3” threshold.
                        if emergency and confidence < 3:
                            confidence = 3

                        if confidence < 3:
                            continue

                        candle_close_ms = int(k.get("T") or 0)
                        candle_key = f"candle:{pair}:{candle_close_ms}"
                        if candle_close_ms and state.candle_alert_dedup.contains(candle_key):
                            continue
                        if candle_close_ms:
                            state.candle_alert_dedup.add(candle_key, ttl_seconds=SECONDS_1_HOUR)

                        asset_name = CRYPTO_DISPLAY[pair]
                        sentiment_bullish = state.fng_value is not None and state.fng_value <= state.config.fear_low
                        sentiment_bearish = state.fng_value is not None and state.fng_value >= state.config.greed_high

                        also_watch = choose_crypto_related_assets(pair)
                        timeframe = "15m"
                        rsi_label = rsi_state_label(rsi)

                        whats_happening = (
                            f"{asset_name} moved {move_pct:+.1f}% in the last {timeframe} while volume was {volume_ratio:.1f}× normal. "
                            f"RSI is {rsi_label.lower()}, which often lines up with momentum extremes (not random noise)."
                        )
                        whats_happening = whats_happening[:380]

                        action, reason = suggest_action(
                            asset_name=asset_name,
                            move_pct=move_pct,
                            rsi=rsi,
                            sentiment_bullish=sentiment_bullish,
                            sentiment_bearish=sentiment_bearish,
                        )

                        suggested_size_line = None
                        if action == "CONSIDER ENTRY":
                            suggested_size_line = DEFAULT_SUGGESTED_SIZE_TEXT

                        text = build_critical_alert_text(
                            asset_name=asset_name,
                            price=close_p,
                            move_pct=move_pct,
                            timeframe=timeframe,
                            rsi=rsi,
                            volume_ratio=volume_ratio,
                            confidence=confidence,
                            whats_happening=whats_happening,
                            action=action,
                            action_reason=reason,
                            suggested_size_line=suggested_size_line,
                            also_watch=also_watch,
                        )

                        # Daily max 3 critical alerts, with top-N replacement via editing.
                        rsi_ext = 0.0
                        if rsi is not None:
                            if rsi < RSI_OVERSOLD:
                                rsi_ext = RSI_OVERSOLD - rsi
                            elif rsi > RSI_OVERBOUGHT:
                                rsi_ext = rsi - RSI_OVERBOUGHT
                        combined_rank = abs(move_pct) + rsi_ext + float(volume_ratio)

                        if len(state.critical_slots) < state.config.critical_max_per_day:
                            message_id = await send_message_html(app, state, text)
                            if message_id is None:
                                continue
                            state.critical_slots.append(
                                CriticalSlot(
                                    message_id=message_id,
                                    asset_key=pair,
                                    name=asset_name,
                                    score=confidence,
                                    move_strength=abs(move_pct),
                                    combined_rank=combined_rank,
                                    last_update_ts=now,
                                )
                            )
                        else:
                            lowest = min(state.critical_slots, key=lambda s: (s.score, s.combined_rank))
                            if (confidence, combined_rank) > (lowest.score, lowest.combined_rank):
                                await edit_message_html(app, state, lowest.message_id, text)
                                lowest.asset_key = pair
                                lowest.name = asset_name
                                lowest.score = confidence
                                lowest.move_strength = abs(move_pct)
                                lowest.combined_rank = combined_rank
                                lowest.last_update_ts = now

                    except Exception as exc:
                        log_err(f"crypto WS message error: {exc}")
        except Exception as exc:
            log_err(f"crypto monitor error: {exc}")
            await asyncio.sleep(SECONDS_1_MIN)


async def external_crypto_monitor(app: Application, state: BotState) -> None:
    """
    Monitor non-Binance spot pairs (currently MNTUSDT) using Bybit candles.
    """
    while not state.stop_event.is_set():
        try:
            for pair in EXTERNAL_CRYPTO_PAIRS:
                # 15m candles for move + volume ratio
                kl_15 = await fetch_bybit_klines(pair, "15", limit=200)
                if len(kl_15) < 30:
                    continue
                last = kl_15[-1]
                start_ms = int(last[0])
                open_p = float(last[1])
                close_p = float(last[4])
                vol = float(last[5])
                if open_p <= 0:
                    continue

                move_pct = ((close_p - open_p) / open_p) * 100.0
                vols = [float(r[5]) for r in kl_15[:-1]]
                avg_vol = (sum(vols) / len(vols)) if vols else 0.0
                volume_ratio = (vol / avg_vol) if avg_vol > 0 else 0.0

                # 1h candles for RSI
                kl_60 = await fetch_bybit_klines(pair, "60", limit=200)
                closes_1h = [float(r[4]) for r in kl_60]
                rsi = compute_rsi(closes_1h, RSI_PERIOD)

                snap = state.crypto_snapshots.get(pair) or MarketSnapshot()
                snap.price = close_p
                snap.move_pct = move_pct
                snap.rsi = rsi
                snap.volume_ratio = volume_ratio
                snap.rsi_state = rsi_state_label(rsi)

                now = now_utc()
                confidence = compute_confidence(state, move_pct, rsi, volume_ratio, now)
                snap.confidence = confidence
                state.crypto_snapshots[pair] = snap

                if confidence < 3 or state.is_paused():
                    continue

                candle_key = f"candle:{pair}:{start_ms}"
                if state.candle_alert_dedup.contains(candle_key):
                    continue
                state.candle_alert_dedup.add(candle_key, ttl_seconds=SECONDS_1_HOUR)

                asset_name = CRYPTO_DISPLAY.get(pair, pair)
                also_watch = "BTC, ETH, AVAX"
                rsi_label = rsi_state_label(rsi)
                whats_happening = (
                    f"{asset_name} moved {move_pct:+.1f}% in the last 15m while volume was {volume_ratio:.1f}× normal. "
                    f"RSI is {rsi_label.lower()}, which often lines up with momentum extremes."
                )[:380]
                action, reason = suggest_action(
                    asset_name=asset_name,
                    move_pct=move_pct,
                    rsi=rsi,
                    sentiment_bullish=state.fng_value is not None and state.fng_value <= state.config.fear_low,
                    sentiment_bearish=state.fng_value is not None and state.fng_value >= state.config.greed_high,
                )
                suggested_size_line = DEFAULT_SUGGESTED_SIZE_TEXT if action == "CONSIDER ENTRY" else None
                text = build_critical_alert_text(
                    asset_name=asset_name,
                    price=close_p,
                    move_pct=move_pct,
                    timeframe="15m",
                    rsi=rsi,
                    volume_ratio=volume_ratio,
                    confidence=confidence,
                    whats_happening=whats_happening,
                    action=action,
                    action_reason=reason,
                    suggested_size_line=suggested_size_line,
                    also_watch=also_watch,
                )
                await send_message_html(app, state, text)
        except Exception as exc:
            log_err(f"external crypto monitor error: {exc}")
        await asyncio.sleep(SECONDS_1_MIN)


def rsi_state_for_extremes(rsi: Optional[float]) -> str:
    return rsi_state_label(rsi)


async def fetch_yfinance_snapshot(ticker: str) -> MarketSnapshot:
    # yfinance is synchronous; caller should run it in a thread.
    df = yf.Ticker(ticker).history(period="10d", interval="15m", auto_adjust=False)
    if df is None or df.empty:
        return MarketSnapshot()
    # Ensure columns exist
    if "Open" not in df.columns or "Close" not in df.columns or "Volume" not in df.columns:
        return MarketSnapshot()

    df = df.dropna(subset=["Open", "Close", "Volume"])
    if len(df) < 25:
        return MarketSnapshot()

    last = df.iloc[-1]
    open_p = float(last["Open"])
    close_p = float(last["Close"])
    volume = float(last["Volume"])

    move_pct = ((close_p - open_p) / open_p) * 100.0 if open_p else 0.0

    # RSI on closes (15m bars)
    closes = [float(x) for x in df["Close"].tolist()]
    rsi = compute_rsi(closes, 14)

    # Volume ratio vs average of previous bars (exclude current)
    volumes = [float(x) for x in df["Volume"].tolist()]
    prev = volumes[:-1]
    avg = sum(prev[-180:]) / len(prev[-180:]) if prev else 0.0  # ~7ish days of 15m bars when available
    volume_ratio = (volume / avg) if avg and avg > 0 else 0.0

    snapshot = MarketSnapshot(
        price=close_p,
        move_pct=move_pct,
        rsi=rsi,
        volume_ratio=volume_ratio,
        rsi_state=rsi_state_for_extremes(rsi),
    )
    return snapshot


async def stocks_monitor(app: Application, state: BotState, client: httpx.AsyncClient) -> None:
    # client unused but kept for consistent signature
    while not state.stop_event.is_set():
        try:
            now_et = now_utc().astimezone(ET)
            if not market_is_open_et(now_et):
                await asyncio.sleep(60)
                continue

            tickers = list(EQUITY_INDICES.keys()) + list(COMMODITIES.keys())
            # Fetch in parallel threads
            results = await asyncio.gather(*[asyncio.to_thread(fetch_yfinance_snapshot, t) for t in tickers], return_exceptions=True)
            now = now_utc()

            # Update snapshots + possibly fire critical alerts (same scoring rules)
            for ticker, res in zip(tickers, results):
                if isinstance(res, Exception):
                    continue
                snapshot: MarketSnapshot = res
                snapshot.rsi_state = rsi_state_for_extremes(snapshot.rsi)
                confidence = compute_confidence(state, snapshot.move_pct, snapshot.rsi, snapshot.volume_ratio, now)
                snapshot.confidence = confidence
                state.market_snapshots[ticker] = snapshot

                # Critical alert sending and daily limit (same behavior)
                if snapshot.confidence is None or snapshot.confidence < 3:
                    continue
                now_sgt = now.astimezone(SGT)
                state.reset_daily_if_needed(now_sgt)

                silent = is_silent_hours_sgt(now_sgt)
                emergency = False  # emergency concept is only required for crypto in your spec
                if silent and not emergency:
                    continue
                if state.is_paused():
                    continue

                if snapshot.price is None or snapshot.move_pct is None or snapshot.rsi is None or snapshot.volume_ratio is None:
                    continue

                if ticker in EQUITY_INDICES:
                    asset_name = EQUITY_INDICES[ticker]
                else:
                    asset_name = COMMODITIES.get(ticker, ticker)

                sentiment_bullish = state.fng_value is not None and state.fng_value <= state.config.fear_low
                sentiment_bearish = state.fng_value is not None and state.fng_value >= state.config.greed_high

                move_pct = snapshot.move_pct
                rsi = snapshot.rsi
                volume_ratio = snapshot.volume_ratio
                timeframe = "15m"
                rsi_label = rsi_state_label(rsi)

                also_watch = choose_market_related_assets(asset_name)

                whats_happening = (
                    f"{asset_name} moved {move_pct:+.1f}% in the last {timeframe} on heavy volume ({volume_ratio:.1f}× normal). "
                    f"RSI is {rsi_label.lower()}, which often points to an overextended move."
                )[:380]

                action, reason = suggest_action(
                    asset_name=asset_name,
                    move_pct=move_pct,
                    rsi=rsi,
                    sentiment_bullish=sentiment_bullish,
                    sentiment_bearish=sentiment_bearish,
                )

                suggested_size_line = None
                if action == "CONSIDER ENTRY":
                    suggested_size_line = DEFAULT_SUGGESTED_SIZE_TEXT

                text = build_critical_alert_text(
                    asset_name=asset_name,
                    price=float(snapshot.price),
                    move_pct=move_pct,
                    timeframe=timeframe,
                    rsi=rsi,
                    volume_ratio=volume_ratio,
                    confidence=int(snapshot.confidence),
                    whats_happening=whats_happening,
                    action=action,
                    action_reason=reason,
                    suggested_size_line=suggested_size_line,
                    also_watch=also_watch,
                )

                rsi_ext = 0.0
                if rsi is not None:
                    if rsi < RSI_OVERSOLD:
                        rsi_ext = RSI_OVERSOLD - rsi
                    elif rsi > RSI_OVERBOUGHT:
                        rsi_ext = rsi - RSI_OVERBOUGHT
                combined_rank = abs(move_pct) + rsi_ext + float(volume_ratio)

                if len(state.critical_slots) < state.config.critical_max_per_day:
                    message_id = await send_message_html(app, state, text)
                    if message_id is None:
                        continue
                    state.critical_slots.append(
                        CriticalSlot(
                            message_id=message_id,
                            asset_key=ticker,
                            name=asset_name,
                            score=int(snapshot.confidence),
                            move_strength=abs(move_pct),
                            combined_rank=combined_rank,
                            last_update_ts=now,
                        )
                    )
                else:
                    lowest = min(state.critical_slots, key=lambda s: (s.score, s.combined_rank))
                    if (int(snapshot.confidence), combined_rank) > (lowest.score, lowest.combined_rank):
                        await edit_message_html(app, state, lowest.message_id, text)
                        lowest.asset_key = ticker
                        lowest.name = asset_name
                        lowest.score = int(snapshot.confidence)
                        lowest.move_strength = abs(move_pct)
                        lowest.combined_rank = combined_rank
                        lowest.last_update_ts = now

            await asyncio.sleep(SECONDS_15_MIN)
        except Exception as exc:
            log_err(f"stocks monitor error: {exc}")
            await asyncio.sleep(SECONDS_1_MIN)


async def news_monitor(app: Application, state: BotState, client: httpx.AsyncClient) -> None:
    # This monitor does NOT send standalone news spam; it feeds confidence + daily briefing.
    while not state.stop_event.is_set():
        try:
            rss_urls = [
                "https://www.coindesk.com/arc/outboundfeeds/rss/",
                "https://cointelegraph.com/rss",
                "https://feeds.feedburner.com/CoinDesk",
            ]
            for feed_url in rss_urls:
                resp = await client.get(feed_url, timeout=25.0, follow_redirects=True)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
                for entry in parsed.entries:
                    title = (entry.get("title") or "").strip()
                    url = (entry.get("link") or "").strip()
                    if not title or not url:
                        continue

                    lower = title.lower()
                    matched = {k for k in HIGH_IMPACT_KEYWORDS if k.lower() in lower}
                    if not matched:
                        continue

                    if already_seen(state, url, ttl_seconds=state.config.news_dedup_ttl_seconds):
                        continue

                    # Timestamp
                    ts = now_utc()
                    try:
                        published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                        if published_parsed:
                            ts = datetime(
                                published_parsed.tm_year,
                                published_parsed.tm_mon,
                                published_parsed.tm_mday,
                                published_parsed.tm_hour,
                                published_parsed.tm_min,
                                published_parsed.tm_sec,
                                tzinfo=UTC,
                            )
                    except Exception:
                        ts = now_utc()

                    direction = build_direction_from_keywords(matched)
                    affected = detect_affected_assets(title)
                    if not affected:
                        affected = ["BTC", "ETH", "SOL"]

                    ev = NewsEvent(
                        ts=ts,
                        url=url,
                        title=title,
                        matched_keywords=matched,
                        direction=direction,
                        affected_assets=affected,
                    )
                    state.recent_news.append(ev)

            # prune older than ~24h
            cutoff = now_utc() - timedelta(hours=24)
            state.recent_news = [ev for ev in state.recent_news if ev.ts >= cutoff]
            state.dedup_news.cleanup()
        except Exception as exc:
            log_err(f"news monitor error: {exc}")
            await asyncio.sleep(SECONDS_1_MIN)
            continue
        await asyncio.sleep(SECONDS_5_MIN)


async def sentiment_monitor(app: Application, state: BotState, client: httpx.AsyncClient) -> None:
    while not state.stop_event.is_set():
        try:
            data = await get_json(client, "https://api.alternative.me/fng/", params={"limit": 8})
            values = data.get("data", []) or []
            if values:
                latest = int(values[0].get("value", 0))
                state.fng_value = latest
                state.fng_label = classify_fear_greed(latest)
        except Exception as exc:
            log_err(f"sentiment monitor error: {exc}")
            await asyncio.sleep(SECONDS_1_MIN)
            continue
        await asyncio.sleep(SECONDS_1_HOUR)


async def get_json(client: httpx.AsyncClient, url: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    resp = await client.get(url, params=params, timeout=25.0)
    resp.raise_for_status()
    return resp.json()


def learning_tip_for_weekday(weekday: int) -> str:
    tips = [
        "Consistency beats big wins. Aim for 1–2% gain per trade, not 20%.",
        "RSI above 72 = overbought. Selling into strength is a skill.",
        "When DXY rises, crypto and gold often fall. Watch the dollar.",
        "Support held 3 times = strong floor. Break below it = danger.",
        "Friday afternoons see low volume. Avoid entering new positions.",
        "Review your week. What signal worked? What was noise?",
        "Plan your week. Set your levels before the market opens Monday.",
    ]
    return tips[weekday % 7]


def sentiment_meaning(value: Optional[int]) -> str:
    if value is None:
        return "Sentiment data is loading—use patience until you see a clear signal."
    if value <= 20:
        return "Extreme fear often signals fear is high, so wait for confirmation before acting."
    if value <= 45:
        return "Fear is elevated, so look for dip-buys only when price stabilizes."
    if value <= 55:
        return "Sentiment is neutral, so make decisions with levels and volume rather than guessing."
    if value <= 80:
        return "Greed is high, so protect downside and avoid chasing strength."
    return "Extreme greed raises risk, so consider taking profits or waiting for a pullback."


async def daily_briefing_task(app: Application, state: BotState, client: httpx.AsyncClient) -> None:
    while not state.stop_event.is_set():
        try:
            now_sgt = now_utc().astimezone(SGT)
            # Sleep until next 8:00 AM SGT
            target = now_sgt.replace(hour=8, minute=0, second=0, microsecond=0)
            if now_sgt >= target:
                target = target + timedelta(days=1)
            await asyncio.sleep(max(0, (target - now_sgt).total_seconds()))

            now_sgt = now_utc().astimezone(SGT)
            if state.daily_briefing_sent_for == now_sgt.date():
                continue
            state.daily_briefing_sent_for = now_sgt.date()

            # Reset daily critical slots (also keeps briefing state consistent)
            state.reset_daily_if_needed(now_sgt)

            date_str = now_sgt.strftime("%Y-%m-%d")
            fng_value = state.fng_value if state.fng_value is not None else 0
            fng_label = state.fng_label if state.fng_label is not None else "N/A"

            # Crypto lines from latest snapshot
            # Expect state.crypto_snapshots to be continuously updated by WS; if missing, show N/A.
            crypto_lines = []
            for pair in CRYPTO_PAIRS:
                snap = state.crypto_snapshots.get(pair)
                name = CRYPTO_DISPLAY[pair]
                if snap and snap.price is not None and snap.move_pct is not None:
                    move_pct = float(snap.move_pct)
                    price = float(snap.price)
                    mood = "😄" if move_pct >= 0.5 else ("😟" if move_pct <= -0.5 else "😐")
                    sign = "+" if move_pct >= 0 else ""
                    crypto_lines.append(f"  {name:<3} ${price:,.0f}  {sign}{move_pct:.1f}%  {mood}")
                else:
                    crypto_lines.append(f"  {name:<3} N/A  N/A  😐")

            # Markets section from yfinance latest snapshots (15m move)
            def market_move(ticker: str) -> tuple[str, str]:
                snap = state.market_snapshots.get(ticker)
                if snap and snap.price is not None and snap.move_pct is not None:
                    sign = "+" if snap.move_pct >= 0 else ""
                    return f"${float(snap.price):,.2f}", f"{sign}{snap.move_pct:.1f}%"
                return "N/A", "N/A"

            spx_price, spx_move = market_move("SPY")
            qq_price, qq_move = market_move("QQQ")
            gld_price, gld_move = market_move("GLD")
            uso_price, uso_move = market_move("USO")
            uup_price, uup_move = market_move("UUP")

            markets_line = (
                f"  S&P 500  {spx_move}  |  Nasdaq {qq_move}\n"
                f"  Gold     {gld_move}  |  Oil    {uso_move}\n"
                f"  DXY      {uup_move}"
            )

            # Overnight news: last 12h with >=2 keyword matches
            overnight_start = now_sgt - timedelta(hours=12)
            overnight_cutoff_utc = overnight_start.astimezone(UTC)
            candidates = [ev for ev in state.recent_news if ev.ts >= overnight_cutoff_utc]
            candidates = [ev for ev in candidates if len(ev.matched_keywords) >= 2]
            # Sort by recency
            candidates.sort(key=lambda ev: ev.ts, reverse=True)
            top_two = candidates[:2]

            overnight_news_lines: list[str] = []
            for ev in top_two:
                dir_label = ev.direction if ev.direction in {"BULLISH", "BEARISH"} else "BEARISH"
                affects = ", ".join(ev.affected_assets[:4])
                headline = escape_html(ev.title)
                overnight_news_lines.append(f"  • {headline} → {dir_label} — affects {escape_html(affects)}")
            overnight_news_block = "\n".join(overnight_news_lines) if overnight_news_lines else ""

            # Today's focus: pick highest confidence asset available (crypto + markets)
            def best_asset() -> tuple[str, int, str]:
                best_key = ""
                best_score = -1
                best_name = ""
                # crypto
                for pair in CRYPTO_PAIRS:
                    snap = state.crypto_snapshots.get(pair)
                    if snap and snap.confidence is not None:
                        sc = int(snap.confidence)
                        if sc > best_score:
                            best_score = sc
                            best_key = pair
                            best_name = CRYPTO_DISPLAY[pair]
                # markets
                for ticker, snap in state.market_snapshots.items():
                    if snap and snap.confidence is not None:
                        sc = int(snap.confidence)
                        if ticker in EQUITY_INDICES:
                            name = EQUITY_INDICES[ticker]
                        else:
                            name = COMMODITIES.get(ticker, ticker)
                        if sc > best_score:
                            best_score = sc
                            best_key = ticker
                            best_name = name
                return best_key, best_score, best_name

            _, best_score, best_name = best_asset()
            # Focus explanation
            focus_line = (
                f"  {best_name} stands out with the highest current confidence. "
                "It matches multiple beginner-friendly signals (move size, RSI stretch, and volume/sentiment/news)."
            )
            confidence_line = f"Confidence: {best_score}/5" if best_score >= 0 else "Confidence: N/A"

            weekday = now_sgt.weekday()  # Mon=0
            tip = learning_tip_for_weekday(weekday)

            daily_text = (
                f"🌅 GOOD MORNING — {escape_html(date_str)}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n"
                "\n"
                "📊 CRYPTO\n"
                + "\n".join(crypto_lines)
                + "\n"
                "📈 MARKETS\n"
                + f"  S&P 500  {spx_move}  |  Nasdaq {qq_move}\n"
                + f"  Gold     {gld_move}  |  Oil    {uso_move}\n"
                + f"  DXY      {uup_move}\n"
                "\n"
                "😨 SENTIMENT\n"
                + f"Fear &amp; Greed: {fng_value} — {escape_html(fng_label)}\n"
                + f"{escape_html(sentiment_meaning(fng_value))}\n"
                "\n"
                "🗞 OVERNIGHT NEWS\n"
                + (overnight_news_block + "\n" if overnight_news_block else "")
                + "\n"
                "🎯 TODAY'S FOCUS\n"
                + focus_line
                + "\n"
                + f"{confidence_line}\n"
                "\n"
                "💡 LEARNING TIP\n"
                + f"{escape_html(tip)}\n"
                "\n"
                "⚠️ Max risk per trade: 1–2% of portfolio (~$20–200)\n"
                "📌 Not financial advice. Always do your own research."
            )

            digest_key = f"daily_briefing:{now_sgt.date().isoformat()}"
            if state.alert_dedup.contains(digest_key):
                continue
            state.alert_dedup.add(digest_key, ttl_seconds=SECONDS_24_HOURS)

            # Send daily briefing even if paused; spec implies routine.
            try:
                await app.bot.send_message(
                    chat_id=state.config.telegram_chat_id,
                    text=daily_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                log_err(f"telegram daily briefing send failed: {exc}")
        except Exception as exc:
            log_err(f"daily briefing error: {exc}")
            await asyncio.sleep(SECONDS_1_MIN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    # Crypto
    lines = ["📡 STATUS"]
    for pair in CRYPTO_PAIRS:
        snap = state.crypto_snapshots.get(pair)
        name = CRYPTO_DISPLAY[pair]
        if snap and snap.price is not None:
            price = format_usd(float(snap.price))
            lines.append(f"{name}: {price}")
        else:
            lines.append(f"{name}: N/A")

    lines.append("")
    lines.append("📈 MARKETS (last poll)")
    for ticker, display in EQUITY_INDICES.items():
        snap = state.market_snapshots.get(ticker)
        if snap and snap.price is not None:
            lines.append(f"{display}: ${float(snap.price):,.2f}")
        else:
            lines.append(f"{display}: N/A")

    lines.append("")
    lines.append("🧱 COMMODITIES (last poll)")
    for ticker, display in COMMODITIES.items():
        snap = state.market_snapshots.get(ticker)
        if snap and snap.price is not None:
            lines.append(f"{display}: ${float(snap.price):,.2f}")
        else:
            lines.append(f"{display}: N/A")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    state.paused_until = now_utc() + timedelta(hours=state.config.alert_pause_hours)
    await update.message.reply_text(
        f"⏸ Alerts paused for {state.config.alert_pause_hours} hours.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    state.paused_until = None
    await update.message.reply_text(
        "▶️ Alerts resumed.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_levels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    client: httpx.AsyncClient = context.application.bot_data["httpx_client"]

    async def support_resistance(symbol: str) -> tuple[str, str]:
        klines = await fetch_klines_15m(client, symbol, limit=96)  # last 24h
        closes = [float(k[4]) for k in klines if len(k) > 4]
        if not closes:
            return "N/A", "N/A"
        low = min(closes)
        high = max(closes)
        return f"${low:,.0f}", f"${high:,.0f}"

    try:
        btc_s, btc_r = await support_resistance("BTCUSDT")
        eth_s, eth_r = await support_resistance("ETHUSDT")
        sol_s, sol_r = await support_resistance("SOLUSDT")
        text = (
            f"🎯 Today’s levels (last 24h, 15m)\n"
            f"BTC: Support {btc_s} | Resistance {btc_r}\n"
            f"ETH: Support {eth_s} | Resistance {eth_r}\n"
            f"SOL: Support {sol_s} | Resistance {sol_r}"
        )
    except Exception as exc:
        log_err(f"/levels error: {exc}")
        text = "⚠️ Unable to fetch levels right now."
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    lines = ["🎯 CONFIDENCE SCORES"]
    for pair in CRYPTO_PAIRS:
        snap = state.crypto_snapshots.get(pair)
        name = CRYPTO_DISPLAY[pair]
        score = snap.confidence if snap and snap.confidence is not None else None
        lines.append(f"{name}: {score}/5" if score is not None else f"{name}: N/A")

    lines.append("")
    lines.append("📈 MARKETS")
    for ticker, display in EQUITY_INDICES.items():
        snap = state.market_snapshots.get(ticker)
        score = snap.confidence if snap and snap.confidence is not None else None
        lines.append(f"{display}: {score}/5" if score is not None else f"{display}: N/A")

    lines.append("")
    lines.append("🧱 COMMODITIES")
    for ticker, display in COMMODITIES.items():
        snap = state.market_snapshots.get(ticker)
        score = snap.confidence if snap and snap.confidence is not None else None
        lines.append(f"{display}: {score}/5" if score is not None else f"{display}: N/A")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def cmd_learn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    concepts = [
        "Use RSI as a ‘stretch meter’: when RSI is very low/high, price may be overextended.",
        "Volume helps you trust a move: big moves with big volume are more meaningful than quiet moves.",
        "Risk first: beginners should size trades small (like 1–2% of capital) so mistakes don't hurt too much.",
        "Don't chase: wait for the move to finish and then decide with levels (support/resistance).",
        "Confidence scores are checklists: they don’t guarantee outcomes—use them to decide what to watch.",
        "Use a plan: entry, exit, and ‘I’m wrong’ level should be decided before you buy.",
    ]
    tip = random.choice(concepts)
    await update.message.reply_text(f"📚 Beginner tip\n{tip}", parse_mode="HTML", disable_web_page_preview=True)


def build_app(state: BotState) -> Application:
    app = Application.builder().token(state.config.telegram_bot_token).build()
    app.bot_data["state"] = state
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("learn", cmd_learn))
    app.add_handler(CommandHandler("score", cmd_score))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("levels", cmd_levels))
    return app


async def on_startup(app: Application) -> None:
    state: BotState = app.bot_data["state"]
    client = httpx.AsyncClient()
    app.bot_data["httpx_client"] = client

    tasks = [
        asyncio.create_task(news_monitor(app, state, client), name="news_monitor"),
        asyncio.create_task(sentiment_monitor(app, state, client), name="sentiment_monitor"),
        asyncio.create_task(crypto_price_monitor(app, state, client), name="crypto_price_monitor"),
        asyncio.create_task(external_crypto_monitor(app, state), name="external_crypto_monitor"),
        asyncio.create_task(stocks_monitor(app, state, client), name="stocks_monitor"),
        asyncio.create_task(daily_briefing_task(app, state, client), name="daily_briefing_task"),
    ]
    app.bot_data["tasks"] = tasks


async def on_shutdown(app: Application) -> None:
    state: BotState = app.bot_data["state"]
    state.stop_event.set()
    tasks = app.bot_data.get("tasks", [])
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    client: Optional[httpx.AsyncClient] = app.bot_data.get("httpx_client")
    if client:
        await client.aclose()


async def _noop(_: Any = None) -> None:
    return


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    log = logging.getLogger(__name__)

    try:
        config = Config.from_env()
    except Exception as exc:
        log_err(f"config error: {exc}")
        raise SystemExit(1)

    state = BotState(config=config)
    app = build_app(state)
    app.post_init = on_startup
    app.post_shutdown = on_shutdown
    app.run_polling(close_loop=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

