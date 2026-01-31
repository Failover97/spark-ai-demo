import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

try:
    from spoon_ai.tools.base import BaseTool
except Exception:  # pragma: no cover - optional dependency
    BaseTool = None
try:
    import httpx
except Exception:  # pragma: no cover - optional dependency
    httpx = None


def _tool_schema(properties: Dict[str, Any], required: Optional[list] = None) -> dict:
    # 生成工具参数的 JSON Schema（给 Agent 看，决定怎么调用工具）
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _iso_now() -> str:
    # 统一返回 UTC 时间（字符串）
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _score(symbol: str, salt: str, low: float, high: float) -> float:
    # 伪随机数：同一个 symbol + salt 会得到稳定的数值（用于 mock 数据）
    base = hashlib.md5(f"{symbol}:{salt}".encode("utf-8")).hexdigest()
    num = int(base[:8], 16) / 0xFFFFFFFF
    return round(low + (high - low) * num, 4)


def _get_env(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value if value else default


COINGECKO_BASE_URL = _get_env("COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "").strip()
COINGECKO_API_KEY_HEADER = _get_env("COINGECKO_API_KEY_HEADER", "x-cg-demo-api-key")
ALTERNATIVE_FNG_URL = _get_env("ALTERNATIVE_FNG_URL", "https://api.alternative.me/fng/")
RETTIWT_API_KEY = os.getenv("RETTIWT_API_KEY", "").strip()
RETTIWT_MAX_TWEETS = int(_get_env("RETTIWT_MAX_TWEETS", "60"))
RETTIWT_KOL_ACCOUNTS = [
    name.strip().lstrip("@")
    for name in os.getenv("RETTIWT_KOL_ACCOUNTS", "").split(",")
    if name.strip()
]
BINANCE_BASE_URL = _get_env("BINANCE_BASE_URL", "https://api.binance.com")
BINANCE_FUTURES_BASE_URL = _get_env("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com")
DEFILLAMA_BASE_URL = _get_env("DEFILLAMA_BASE_URL", "https://api.llama.fi")


_COIN_LIST_CACHE: Dict[str, Any] = {"ts": 0.0, "data": []}
_COIN_LIST_TTL = 24 * 60 * 60


def _cg_headers() -> Dict[str, str]:
    # CoinGecko Pro 或 Demo 的 API Key 头（没有就不加）
    if COINGECKO_API_KEY:
        return {COINGECKO_API_KEY_HEADER: COINGECKO_API_KEY}
    return {}


async def _cg_get(path: str, params: Optional[dict] = None) -> dict:
    # 统一的 CoinGecko GET 请求
    if httpx is None:
        raise RuntimeError("httpx is not installed. Install deps and retry.")
    url = f"{COINGECKO_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, params=params or {}, headers=_cg_headers())
        response.raise_for_status()
        return response.json()


async def _http_get_json(url: str, params: Optional[dict] = None) -> dict:
    if httpx is None:
        raise RuntimeError("httpx is not installed. Install deps and retry.")
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, params=params or {})
        response.raise_for_status()
        return response.json()


async def _binance_get(path: str, params: Optional[dict] = None) -> Any:
    if httpx is None:
        raise RuntimeError("httpx is not installed. Install deps and retry.")
    url = f"{BINANCE_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, params=params or {})
        response.raise_for_status()
        return response.json()


async def _binance_futures_get(path: str, params: Optional[dict] = None) -> Any:
    if httpx is None:
        raise RuntimeError("httpx is not installed. Install deps and retry.")
    url = f"{BINANCE_FUTURES_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, params=params or {})
        response.raise_for_status()
        return response.json()


async def _defillama_get(path: str, params: Optional[dict] = None) -> Any:
    if httpx is None:
        raise RuntimeError("httpx is not installed. Install deps and retry.")
    url = f"{DEFILLAMA_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, params=params or {})
        response.raise_for_status()
        return response.json()


 


async def _resolve_coin_id(symbol: str) -> str:
    # 把 symbol 映射成 CoinGecko 的 coin_id
    symbol = symbol.lower()
    known = {
        "btc": "bitcoin",
        "eth": "ethereum",
        "usdt": "tether",
        "usdc": "usd-coin",
        "bnb": "binancecoin",
        "sol": "solana",
        "xrp": "ripple",
        "ada": "cardano",
        "doge": "dogecoin",
        "dot": "polkadot",
        "avax": "avalanche-2",
        "link": "chainlink",
        "arb": "arbitrum",
        "op": "optimism",
    }
    if symbol in known:
        return known[symbol]

    now = time.time()
    if now - float(_COIN_LIST_CACHE["ts"]) > _COIN_LIST_TTL:
        data = await _cg_get("/coins/list")
        _COIN_LIST_CACHE["data"] = data or []
        _COIN_LIST_CACHE["ts"] = now

    for coin in _COIN_LIST_CACHE["data"]:
        if coin.get("symbol", "").lower() == symbol:
            return coin.get("id", "")
    raise ValueError(f"Unknown symbol: {symbol}")


def _series_from_prices(prices: List[List[float]]) -> List[float]:
    # CoinGecko 返回 [timestamp, price] 列表
    return [float(p[1]) for p in prices if isinstance(p, list) and len(p) >= 2]


def _high_low(values: List[float]) -> Tuple[Optional[float], Optional[float]]:
    if not values:
        return None, None
    return max(values), min(values)


def _sma(values: List[float], period: int) -> Optional[float]:
    # 简单移动平均线
    if len(values) < period:
        return None
    return round(sum(values[-period:]) / period, 6)


def _ema(values: List[float], period: int) -> Optional[List[float]]:
    # 指数移动平均线
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_values = [values[0]]
    for value in values[1:]:
        ema_values.append((value * k) + (ema_values[-1] * (1 - k)))
    return ema_values


def _rsi(values: List[float], period: int = 14) -> Optional[float]:
    # RSI 指标
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = values[-i] - values[-i - 1]
        if delta >= 0:
            gains.append(delta)
        else:
            losses.append(abs(delta))
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 4)


def _macd(values: List[float]) -> Dict[str, Optional[float]]:
    # MACD 指标
    if len(values) < 26:
        return {"macd": None, "signal": None, "hist": None}
    ema_fast = _ema(values, 12)
    ema_slow = _ema(values, 26)
    if not ema_fast or not ema_slow:
        return {"macd": None, "signal": None, "hist": None}
    macd_line = [f - s for f, s in zip(ema_fast[-len(ema_slow):], ema_slow)]
    signal_line = _ema(macd_line, 9)
    if not signal_line:
        return {"macd": None, "signal": None, "hist": None}
    macd = macd_line[-1]
    signal = signal_line[-1]
    hist = macd - signal
    return {"macd": round(macd, 6), "signal": round(signal, 6), "hist": round(hist, 6)}


def _max_drawdown(values: List[float]) -> Optional[float]:
    # 最大回撤（负数表示回撤幅度）
    if not values:
        return None
    peak = values[0]
    max_dd = 0.0
    for v in values[1:]:
        if v > peak:
            peak = v
        dd = (v - peak) / peak
        if dd < max_dd:
            max_dd = dd
    return round(max_dd, 6)


def _avg_drawdown(values: List[float]) -> Optional[float]:
    # 年均回撤（这里用一段时间内回撤序列的平均值）
    if not values:
        return None
    peak = values[0]
    drawdowns = []
    for v in values[1:]:
        if v > peak:
            peak = v
        drawdowns.append((v - peak) / peak)
    if not drawdowns:
        return 0.0
    return round(sum(drawdowns) / len(drawdowns), 6)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _tokenize(text: str) -> List[str]:
    cleaned = re.sub(r"[^0-9a-zA-Z#@_]+", " ", text.lower())
    return [t for t in cleaned.split() if t]


def _sentiment_score(text: str) -> float:
    positives = {
        "bull", "bullish", "pump", "moon", "up", "win", "profit", "positive",
        "strong", "breakout", "buy", "support", "growth", "great",
    }
    negatives = {
        "bear", "bearish", "dump", "down", "loss", "negative", "weak",
        "breakdown", "sell", "risk", "fear", "crash", "rug", "scam",
    }
    tokens = _tokenize(text)
    if not tokens:
        return 0.0
    score = 0
    for token in tokens:
        if token in positives:
            score += 1
        if token in negatives:
            score -= 1
    return max(-1.0, min(1.0, score / max(1, len(tokens) / 5)))


def _extract_tweet_fields(tweet: Dict[str, Any]) -> Dict[str, Any]:
    text = (
        tweet.get("fullText")
        or tweet.get("text")
        or tweet.get("content")
        or ""
    )
    user = tweet.get("user") or tweet.get("author") or tweet.get("tweetBy") or {}
    user_name = (
        tweet.get("userName")
        or user.get("userName")
        or user.get("username")
        or user.get("screenName")
        or ""
    )
    followers = (
        user.get("followersCount")
        or user.get("followers")
        or tweet.get("followersCount")
        or 0
    )
    created_at = tweet.get("createdAt") or tweet.get("created_at")
    return {
        "text": _normalize_text(text),
        "user_name": str(user_name).lstrip("@"),
        "followers": int(followers) if str(followers).isdigit() else 0,
        "created_at": created_at,
    }


def _group_trend(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        created_at = entry.get("created_at")
        if created_at:
            try:
                if isinstance(created_at, (int, float)):
                    dt = datetime.fromtimestamp(float(created_at) / 1000, tz=timezone.utc)
                else:
                    dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                key = dt.date().isoformat()
            except (ValueError, TypeError):
                key = "unknown"
        else:
            key = "unknown"
        bucket = buckets.setdefault(key, {"date": key, "tweet_count": 0, "score": 0.0})
        bucket["tweet_count"] += 1
        bucket["score"] += entry.get("sentiment", 0.0)
    trend = []
    for key, bucket in sorted(buckets.items()):
        count = bucket["tweet_count"] or 1
        trend.append(
            {
                "date": bucket["date"],
                "tweet_count": bucket["tweet_count"],
                "avg_sentiment": round(bucket["score"] / count, 4),
            }
        )
    return trend


def _build_word_cloud(entries: List[Dict[str, Any]], limit: int = 30) -> List[Dict[str, Any]]:
    stopwords = {
        "the", "and", "for", "with", "this", "that", "from", "have", "has",
        "just", "you", "your", "are", "was", "were", "will", "been", "but",
        "about", "what", "when", "where", "into", "its", "it's", "not", "now",
        "http", "https", "tco", "co", "com",
    }
    counts: Dict[str, int] = {}
    for entry in entries:
        for token in _tokenize(entry.get("text", "")):
            if token in stopwords or token.startswith("@"):
                continue
            counts[token] = counts.get(token, 0) + 1
    top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [{"word": word, "count": count} for word, count in top]


def _pct_returns(prices: List[float]) -> List[float]:
    returns = []
    for i in range(1, len(prices)):
        prev = prices[i - 1]
        if prev == 0:
            continue
        returns.append((prices[i] - prev) / prev)
    return returns


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _std(values: List[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    mean = _mean(values) or 0.0
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return var**0.5


def _annualization_factor(interval: str) -> Optional[float]:
    interval_map = {
        "1m": 365 * 24 * 60,
        "3m": 365 * 24 * 20,
        "5m": 365 * 24 * 12,
        "15m": 365 * 24 * 4,
        "30m": 365 * 24 * 2,
        "1h": 365 * 24,
        "2h": 365 * 12,
        "4h": 365 * 6,
        "6h": 365 * 4,
        "8h": 365 * 3,
        "12h": 365 * 2,
        "1d": 365,
        "3d": 365 / 3,
        "1w": 52,
    }
    return interval_map.get(interval)


def _risk_level(volatility_pct: Optional[float]) -> str:
    if volatility_pct is None:
        return "unknown"
    if volatility_pct < 1.5:
        return "low"
    if volatility_pct < 4.0:
        return "medium"
    return "high"


def _bollinger(values: List[float], period: int = 20, std_mult: float = 2.0) -> Dict[str, Optional[float]]:
    if len(values) < period:
        return {"upper": None, "middle": None, "lower": None}
    middle = _sma(values, period)
    window = values[-period:]
    std = _std(window)
    if middle is None or std is None:
        return {"upper": None, "middle": None, "lower": None}
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    return {"upper": round(upper, 6), "middle": round(middle, 6), "lower": round(lower, 6)}


def _support_resistance_from_prices(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"support": None, "resistance": None}
    return {"support": min(values), "resistance": max(values)}


def _depth_levels(orderbook: Dict[str, Any], side: str = "bids", top_n: int = 5) -> List[Dict[str, float]]:
    levels = []
    rows = orderbook.get(side, []) if isinstance(orderbook, dict) else []
    for row in rows[:top_n]:
        try:
            price = float(row[0])
            qty = float(row[1])
        except Exception:
            continue
        levels.append({"price": price, "qty": qty})
    return levels


def _trade_flow(trades: List[Dict[str, Any]], min_notional: float) -> Dict[str, Any]:
    large = []
    buy_notional = 0.0
    sell_notional = 0.0
    for trade in trades:
        try:
            price = float(trade.get("price"))
            qty = float(trade.get("qty"))
        except Exception:
            continue
        notional = price * qty
        if notional < min_notional:
            continue
        is_buyer_maker = trade.get("isBuyerMaker")
        side = "sell" if is_buyer_maker else "buy"
        if side == "buy":
            buy_notional += notional
        else:
            sell_notional += notional
        large.append(
            {
                "price": price,
                "qty": qty,
                "notional": round(notional, 4),
                "side": side,
                "time": trade.get("time"),
            }
        )
    return {
        "large_trades": large,
        "buy_notional": round(buy_notional, 4),
        "sell_notional": round(sell_notional, 4),
        "net_notional": round(buy_notional - sell_notional, 4),
    }


def _pick_protocol(protocols: List[Dict[str, Any]], symbol: str) -> Optional[Dict[str, Any]]:
    symbol_lower = symbol.lower()
    for proto in protocols:
        if str(proto.get("symbol", "")).lower() == symbol_lower:
            return proto
    for proto in protocols:
        if str(proto.get("name", "")).lower() == symbol_lower:
            return proto
    for proto in protocols:
        slug = str(proto.get("slug", "")).lower()
        if slug == symbol_lower or slug.replace("-", "") == symbol_lower.replace("-", ""):
            return proto
    return None


def _pick_chain(chains: List[Dict[str, Any]], symbol: str) -> Optional[Dict[str, Any]]:
    symbol_lower = symbol.lower()
    for chain in chains:
        if str(chain.get("tokenSymbol", "")).lower() == symbol_lower:
            return chain
    for chain in chains:
        if str(chain.get("name", "")).lower() == symbol_lower:
            return chain
    for chain in chains:
        if str(chain.get("gecko_id", "")).lower() == symbol_lower:
            return chain
    return None


def _tvl_series(history: Any, limit: int = 90) -> List[Dict[str, Any]]:
    if not isinstance(history, list):
        return []
    trimmed = history[-limit:] if limit else history
    series = []
    for item in trimmed:
        if not isinstance(item, dict):
            continue
        series.append({"date": item.get("date"), "tvl": item.get("totalLiquidityUSD")})
    return series


def _chain_tvl_series(history: Any, limit: int = 90) -> List[Dict[str, Any]]:
    if not isinstance(history, list):
        return []
    trimmed = history[-limit:] if limit else history
    series = []
    for item in trimmed:
        if not isinstance(item, dict):
            continue
        tvl_value = item.get("tvl")
        if tvl_value is None:
            tvl_value = item.get("totalLiquidityUSD")
        series.append({"date": item.get("date"), "tvl": tvl_value})
    return series


async def _run_rettiwt_search(
    filter_obj: Dict[str, Any], count: int, cursor: Optional[str] = None
) -> Dict[str, Any]:
    if not RETTIWT_API_KEY:
        return {"ok": False, "error": "RETTIWT_API_KEY is not set."}

    script = {"filter": filter_obj, "count": count, "cursor": cursor}
    node_code = f"""
const {{ Rettiwt }} = require('rettiwt-api');
const input = {json.dumps(script)};
const rettiwt = new Rettiwt({{ apiKey: process.env.RETTIWT_API_KEY }});
const run = async () => {{
  const data = input.cursor
    ? await rettiwt.tweet.search(input.filter, input.count, input.cursor)
    : await rettiwt.tweet.search(input.filter, input.count);
  const payload = data && data.toJSON ? data.toJSON() : data;
  console.log(JSON.stringify(payload));
}};
run().catch((err) => {{
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
}});
"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "node",
            "-e",
            node_code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "RETTIWT_API_KEY": RETTIWT_API_KEY},
        )
    except FileNotFoundError:
        return {"ok": False, "error": "node is not available on PATH."}

    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (stderr or stdout).decode("utf-8", errors="ignore").strip(),
        }
    try:
        return {"ok": True, "data": json.loads(stdout.decode("utf-8"))}
    except json.JSONDecodeError:
        return {"ok": False, "error": "Failed to parse Rettiwt output as JSON."}


if BaseTool:
    class MarketDataTool(BaseTool):
        """市场基础数据 + 价格走势 + 技术指标 + 回撤"""

        name: str = "get_market_data"
        description: str = "获取加密货币的市场数据（基于 CoinGecko）。"
        parameters: dict = _tool_schema(
            {
                "symbol": {
                    "type": "string",
                    "description": "Token symbol，比如 BTC, ETH.",
                },
            },
            required=["symbol"],
        )

        async def execute(self, symbol: str) -> Dict[str, Any]:
            # 返回市场基础 + 走势 + 技术指标 + 回撤数据（CoinGecko）
            if httpx is None:
                raise RuntimeError("httpx is not installed. Install deps and retry.")

            symbol = symbol.upper()
            coin_id = await _resolve_coin_id(symbol)

            basic = await _cg_get(
                "/simple/price",
                params={
                    "ids": coin_id,
                    "vs_currencies": "usd",
                    "include_market_cap": "true",
                    "include_24hr_change": "true",
                    "include_24hr_vol": "true",
                    "include_last_updated_at": "true",
                },
            )
            basic_data = basic.get(coin_id, {})

            def _chart(days: int) -> dict:
                return {"vs_currency": "usd", "days": str(days), "interval": "daily"}

            chart_7 = await _cg_get(f"/coins/{coin_id}/market_chart", params=_chart(7))
            chart_30 = await _cg_get(
                f"/coins/{coin_id}/market_chart", params=_chart(30)
            )
            chart_90 = await _cg_get(
                f"/coins/{coin_id}/market_chart", params=_chart(90)
            )

            prices_7 = _series_from_prices(chart_7.get("prices", []))
            prices_30 = _series_from_prices(chart_30.get("prices", []))
            prices_90 = _series_from_prices(chart_90.get("prices", []))

            high_7, low_7 = _high_low(prices_7)
            high_30, low_30 = _high_low(prices_30)
            high_90, low_90 = _high_low(prices_90)

            coin_detail = await _cg_get(
                f"/coins/{coin_id}",
                params={
                    "localization": "false",
                    "tickers": "false",
                    "market_data": "true",
                    "community_data": "false",
                    "developer_data": "false",
                    "sparkline": "false",
                },
            )
            market_data = coin_detail.get("market_data", {})
            ath = market_data.get("ath", {}).get("usd")
            ath_date = market_data.get("ath_date", {}).get("usd")
            current_price = market_data.get("current_price", {}).get("usd")
            ath_distance = None
            if ath and current_price:
                ath_distance = round((current_price - ath) / ath, 6)

            rsi_14 = _rsi(prices_90, 14)
            macd = _macd(prices_90)
            ma_20 = _sma(prices_90, 20)
            ma_50 = _sma(prices_90, 50)

            chart_365 = await _cg_get(
                f"/coins/{coin_id}/market_chart", params=_chart(365)
            )
            prices_365 = _series_from_prices(chart_365.get("prices", []))
            max_dd = _max_drawdown(prices_365)
            avg_dd = _avg_drawdown(prices_365)

            return {
                "symbol": symbol,
                "coin_id": coin_id,
                "as_of": _iso_now(),
                "source": "coingecko",
                "basic_market_data": {
                    "price_usd": basic_data.get("usd"),
                    "market_cap_usd": basic_data.get("usd_market_cap"),
                    "change_24h_pct": basic_data.get("usd_24h_change"),
                    "volume_24h_usd": basic_data.get("usd_24h_vol"),
                    "last_updated_at": basic_data.get("last_updated_at"),
                },
                "price_trend": {
                    "history_7d": prices_7,
                    "history_30d": prices_30,
                    "history_90d": prices_90,
                    "high_low": {
                        "7d": {"high": high_7, "low": low_7},
                        "30d": {"high": high_30, "low": low_30},
                        "90d": {"high": high_90, "low": low_90},
                    },
                    "ath": {
                        "ath_price_usd": ath,
                        "ath_date": ath_date,
                        "distance_to_ath": ath_distance,
                    },
                },
                "technical_indicators": {
                    "rsi_14": rsi_14,
                    "macd": macd,
                    "ma_20": ma_20,
                    "ma_50": ma_50,
                },
                "drawdown": {
                    "max_drawdown_1y": max_dd,
                    "avg_drawdown_1y": avg_dd,
                },
            }



    class SocialSentimentTool(BaseTool):
        """Market fear & greed + Twitter sentiment analysis (Rettiwt-API)."""

        name: str = "get_social_sentiment"
        description: str = "Get market fear/greed and Twitter sentiment for a symbol."
        parameters: dict = _tool_schema(
            {
                "symbol": {
                    "type": "string",
                    "description": "Token symbol, e.g., BTC, ETH.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional custom Twitter query (native syntax or keywords).",
                },
                "max_tweets": {
                    "type": "number",
                    "description": "Max tweets to fetch (default from env).",
                },
                "from_users": {
                    "type": "array",
                    "description": "Optional list of usernames (without @).",
                    "items": {"type": "string"},
                },
                "mentions": {
                    "type": "array",
                    "description": "Optional list of mentioned usernames (without @).",
                    "items": {"type": "string"},
                },
                "hashtags": {
                    "type": "array",
                    "description": "Optional list of hashtags (without #).",
                    "items": {"type": "string"},
                },
                "exclude_words": {
                    "type": "array",
                    "description": "Optional list of excluded keywords.",
                    "items": {"type": "string"},
                },
                "only_original": {
                    "type": "boolean",
                    "description": "Only original tweets (exclude retweets).",
                },
                "only_replies": {
                    "type": "boolean",
                    "description": "Only replies.",
                },
                "top": {
                    "type": "boolean",
                    "description": "Top tweets (popular).",
                },
            },
            required=["symbol"],
        )

        async def execute(
            self,
            symbol: str,
            query: Optional[str] = None,
            max_tweets: Optional[int] = None,
            from_users: Optional[List[str]] = None,
            mentions: Optional[List[str]] = None,
            hashtags: Optional[List[str]] = None,
            exclude_words: Optional[List[str]] = None,
            only_original: Optional[bool] = None,
            only_replies: Optional[bool] = None,
            top: Optional[bool] = None,
        ) -> Dict[str, Any]:
            if httpx is None:
                raise RuntimeError("httpx is not installed. Install deps and retry.")

            symbol = symbol.upper()
            max_tweets = int(max_tweets or RETTIWT_MAX_TWEETS)
            query = query or f"{symbol}"
            include_words = [token for token in _tokenize(query) if token]
            if not include_words:
                include_words = [symbol.lower()]
            if hashtags is None:
                hashtags = re.findall(r"#([A-Za-z0-9_]+)", query or "")

            fng = await _http_get_json(
                ALTERNATIVE_FNG_URL, params={"limit": 1, "format": "json"}
            )
            fng_entry = (fng.get("data") or [{}])[0]

            twitter_result = await _run_rettiwt_search(
                {
                    "includeWords": include_words,
                    "excludeWords": exclude_words or [],
                    "hashtags": hashtags or [],
                    "fromUsers": from_users or [],
                    "mentions": mentions or [],
                    "onlyOriginal": bool(only_original) if only_original is not None else False,
                    "onlyReplies": bool(only_replies) if only_replies is not None else False,
                    "top": bool(top) if top is not None else False,
                },
                max_tweets,
            )

            if not twitter_result.get("ok"):
                twitter_analysis = {
                    "mock": True,
                    "query": query,
                    "error": twitter_result.get("error"),
                    "tweet_count": 0,
                    "sentiment_trend": [],
                    "word_cloud": [],
                    "influencers": [],
                    "kol_viewpoints": [],
                }
            else:
                raw_data = twitter_result.get("data", {})
                tweets = (
                    raw_data.get("list")
                    if isinstance(raw_data, dict)
                    else None
                )
                if tweets is None:
                    tweets = raw_data.get("data") if isinstance(raw_data, dict) else None
                if tweets is None:
                    tweets = raw_data.get("tweets") if isinstance(raw_data, dict) else None
                if tweets is None:
                    tweets = raw_data
                if isinstance(tweets, dict):
                    tweets = list(tweets.values())
                parsed = []
                for tweet in tweets or []:
                    fields = _extract_tweet_fields(tweet if isinstance(tweet, dict) else {})
                    if not fields["text"]:
                        continue
                    fields["sentiment"] = _sentiment_score(fields["text"])
                    parsed.append(fields)

                trend = _group_trend(parsed)
                word_cloud = _build_word_cloud(parsed)

                influencers = {}
                for entry in parsed:
                    if not entry["user_name"]:
                        continue
                    bucket = influencers.setdefault(
                        entry["user_name"],
                        {"username": entry["user_name"], "followers": entry["followers"], "tweet_count": 0, "score": 0.0},
                    )
                    bucket["followers"] = max(bucket["followers"], entry["followers"])
                    bucket["tweet_count"] += 1
                    bucket["score"] += entry["sentiment"]
                influencer_list = sorted(
                    influencers.values(),
                    key=lambda item: (item["followers"], item["tweet_count"]),
                    reverse=True,
                )[:10]
                for item in influencer_list:
                    count = item["tweet_count"] or 1
                    item["avg_sentiment"] = round(item["score"] / count, 4)
                    item.pop("score", None)

                kol_viewpoints = []
                if RETTIWT_KOL_ACCOUNTS:
                    kol_filter = {
                        "fromUsers": RETTIWT_KOL_ACCOUNTS,
                        "includeWords": include_words,
                        "hashtags": hashtags or [],
                    }
                    kol_result = await _run_rettiwt_search(kol_filter, max_tweets)
                    if kol_result.get("ok"):
                        kol_raw = kol_result.get("data", {})
                        kol_tweets = (
                            kol_raw.get("list")
                            if isinstance(kol_raw, dict)
                            else None
                        )
                        if kol_tweets is None:
                            kol_tweets = kol_raw.get("data") if isinstance(kol_raw, dict) else None
                        if kol_tweets is None:
                            kol_tweets = kol_raw.get("tweets") if isinstance(kol_raw, dict) else None
                        if kol_tweets is None:
                            kol_tweets = kol_raw
                        kol_parsed = []
                        for tweet in kol_tweets or []:
                            fields = _extract_tweet_fields(tweet if isinstance(tweet, dict) else {})
                            if not fields["text"]:
                                continue
                            fields["sentiment"] = _sentiment_score(fields["text"])
                            kol_parsed.append(fields)
                        by_kol: Dict[str, List[Dict[str, Any]]] = {}
                        for entry in kol_parsed:
                            by_kol.setdefault(entry["user_name"], []).append(entry)
                        for name, entries in by_kol.items():
                            avg_sent = round(
                                sum(e["sentiment"] for e in entries) / max(1, len(entries)), 4
                            )
                            kol_viewpoints.append(
                                {
                                    "username": name,
                                    "avg_sentiment": avg_sent,
                                    "sample_tweets": [e["text"] for e in entries[:3]],
                                    "hot_words": _build_word_cloud(entries, limit=10),
                                }
                            )
                twitter_analysis = {
                    "mock": False,
                    "query": query,
                    "tweet_count": len(parsed),
                    "sentiment_trend": trend,
                    "word_cloud": word_cloud,
                    "influencers": influencer_list,
                    "kol_viewpoints": kol_viewpoints,
                }

            return {
                "symbol": symbol,
                "as_of": _iso_now(),
                "market_fear_greed": {
                    "value": fng_entry.get("value"),
                    "classification": fng_entry.get("value_classification"),
                    "timestamp": fng_entry.get("timestamp"),
                    "time_until_update": fng_entry.get("time_until_update"),
                    "source": "alternative.me",
                },
                "twitter": twitter_analysis,
            }


    class EcosystemTool(BaseTool):
        """Chain/protocol ecosystem overview from DefiLlama."""

        name: str = "get_ecosystem_overview"
        description: str = "Get chain/protocol TVL, rank, and 90d trend from DefiLlama."
        parameters: dict = _tool_schema(
            {
                "mode": {
                    "type": "string",
                    "description": "chain or protocol",
                },
                "symbol": {
                    "type": "string",
                    "description": "Chain name or protocol symbol/name/slug.",
                },
                "days": {
                    "type": "number",
                    "description": "History window in days (default 90).",
                },
            },
            required=["symbol"],
        )

        async def execute(
            self,
            symbol: str,
            mode: str = "protocol",
            days: int = 90,
        ) -> Dict[str, Any]:
            if httpx is None:
                raise RuntimeError("httpx is not installed. Install deps and retry.")

            symbol = symbol.strip()
            mode = (mode or "protocol").lower().strip()
            days = max(7, min(int(days), 365))

            if mode == "chain":
                chains = await _defillama_get("/v2/chains")
                chain = _pick_chain(chains or [], symbol)
                if not chain:
                    raise ValueError(f"Unknown chain: {symbol}")

                sorted_chains = sorted(
                    chains or [], key=lambda item: item.get("tvl", 0) or 0, reverse=True
                )
                rank = next(
                    (
                        idx + 1
                        for idx, item in enumerate(sorted_chains)
                        if item.get("name") == chain.get("name")
                    ),
                    None,
                )

                protocols = await _defillama_get("/protocols")
                protocol_count = 0
                for proto in protocols or []:
                    if symbol.lower() in [c.lower() for c in proto.get("chains", [])]:
                        protocol_count += 1

                history = []
                history_error = None
                try:
                    chain_name = quote(str(chain.get("name") or ""), safe="")
                    history_raw = await _defillama_get(
                        f"/v2/historicalChainTvl/{chain_name}"
                    )
                    history = _chain_tvl_series(history_raw, limit=days)
                except Exception as exc:
                    history_error = str(exc)

                return {
                    "mode": "chain",
                    "symbol": symbol,
                    "as_of": _iso_now(),
                    "source": "defillama",
                    "chain": {
                        "name": chain.get("name"),
                        "token_symbol": chain.get("tokenSymbol"),
                        "gecko_id": chain.get("gecko_id"),
                        "tvl_usd": chain.get("tvl"),
                        "market_rank": rank,
                    },
                    "protocol_count": protocol_count,
                    "tvl_history": history,
                    "history_note": history_error,
                }

            protocols = await _defillama_get("/protocols")
            protocol = _pick_protocol(protocols or [], symbol)
            if not protocol:
                raise ValueError(f"Unknown protocol: {symbol}")

            sorted_protocols = sorted(
                protocols or [], key=lambda item: item.get("tvl", 0) or 0, reverse=True
            )
            rank = next(
                (
                    idx + 1
                    for idx, item in enumerate(sorted_protocols)
                    if item.get("name") == protocol.get("name")
                ),
                None,
            )

            slug = protocol.get("slug") or protocol.get("name")
            protocol_slug = quote(str(slug or ""), safe="")
            detail = await _defillama_get(f"/protocol/{protocol_slug}")

            history = []
            if isinstance(detail, dict):
                candidate = detail.get("tvl")
                if isinstance(candidate, list):
                    history = _chain_tvl_series(candidate, limit=days)
                elif isinstance(detail.get("tvlHistory"), list):
                    history = _chain_tvl_series(detail.get("tvlHistory"), limit=days)

            return {
                "mode": "protocol",
                "symbol": symbol,
                "as_of": _iso_now(),
                "source": "defillama",
                "protocol": {
                    "name": protocol.get("name"),
                    "symbol": protocol.get("symbol"),
                    "category": protocol.get("category"),
                    "chains": protocol.get("chains", []),
                    "tvl_usd": protocol.get("tvl"),
                    "market_rank": rank,
                    "change_1d": protocol.get("change_1d"),
                    "change_7d": protocol.get("change_7d"),
                    "change_1m": protocol.get("change_1m"),
                },
                "protocol_count": len(protocols or []),
                "tvl_history": history,
            }


    class TargetAnalysisTool(BaseTool):
        """PnL, targets, R:R, scaling plan, rebalance suggestion."""

        name: str = "get_target_analysis"
        description: str = "Compute PnL, target prices, R:R, and exit plan."
        parameters: dict = _tool_schema(
            {
                "symbol": {"type": "string", "description": "Token symbol, e.g., BTC, ETH."},
                "quantity": {"type": "number", "description": "Position size in units."},
                "avg_cost": {"type": "number", "description": "Average entry price (USD)."},
                "risk_preference": {"type": "string", "description": "low / medium / high"},
                "stop_loss_pct": {"type": "number", "description": "Stop loss percent (e.g., 0.08 for 8%)."},
                "rr_ratio": {"type": "number", "description": "Risk:Reward base multiple (e.g., 1 or 2.5)."},
            },
            required=["symbol", "quantity", "avg_cost"],
        )

        async def execute(
            self,
            symbol: str,
            quantity: float,
            avg_cost: float,
            risk_preference: str = "medium",
            stop_loss_pct: float = 0.08,
            rr_ratio: float = 1.0,
        ) -> Dict[str, Any]:
            if httpx is None:
                raise RuntimeError("httpx is not installed. Install deps and retry.")

            symbol = symbol.upper()
            quantity = float(quantity)
            avg_cost = float(avg_cost)
            stop_loss_pct = max(0.001, float(stop_loss_pct))
            rr_ratio = max(0.5, float(rr_ratio))

            market = await MarketDataTool().execute(symbol)
            current_price = market.get("basic_market_data", {}).get("price_usd")
            if current_price is None:
                raise RuntimeError("Current price unavailable.")

            pnl_amount = (current_price - avg_cost) * quantity
            pnl_pct = (current_price - avg_cost) / avg_cost if avg_cost else None

            support = None
            resistance = None
            try:
                tech = await TechnicalAnalysisTool().execute(symbol)
                levels = (tech.get("support_resistance") or {}).get("combined") or {}
                support = levels.get("support")
                resistance = levels.get("resistance")
            except Exception:
                price_trend = market.get("price_trend", {})
                high_low = price_trend.get("high_low", {})
                highs = [
                    (high_low.get("7d") or {}).get("high"),
                    (high_low.get("30d") or {}).get("high"),
                    (high_low.get("90d") or {}).get("high"),
                ]
                lows = [
                    (high_low.get("7d") or {}).get("low"),
                    (high_low.get("30d") or {}).get("low"),
                    (high_low.get("90d") or {}).get("low"),
                ]
                resistance = max([v for v in highs if v is not None], default=None)
                support = min([v for v in lows if v is not None], default=None)

            vol_data = await VolatilityTool().execute(symbol, interval="5m", limit=200)
            vol = vol_data.get("volatility", {})
            risk_level = vol.get("risk_level")

            stop_loss = avg_cost * (1 - stop_loss_pct)
            risk_distance = max(avg_cost - stop_loss, 0.0)

            pref = (risk_preference or "medium").lower()
            if pref == "low":
                rr_multipliers = [1.0, 1.6, 2.2]
                take_profit_ratios = [0.40, 0.40, 0.20]
            elif pref == "high":
                rr_multipliers = [1.0, 2.5, 4.0]
                take_profit_ratios = [0.20, 0.30, 0.50]
            else:
                rr_multipliers = [1.0, 2.0, 3.0]
                take_profit_ratios = [0.30, 0.40, 0.30]

            target1 = avg_cost + risk_distance * (rr_ratio * rr_multipliers[0])
            target2 = avg_cost + risk_distance * (rr_ratio * rr_multipliers[1])
            target3 = avg_cost + risk_distance * (rr_ratio * rr_multipliers[2])

            if resistance:
                def _adjust(target: float) -> float:
                    if resistance and abs(target - resistance) / resistance < 0.02:
                        return resistance * 0.99
                    return target
                target1 = _adjust(target1)
                target2 = _adjust(target2)
                target3 = _adjust(target3)

            rr = None
            if risk_distance > 0:
                rr = (target1 - avg_cost) / risk_distance

            take_profit = [
                {"target": target1, "ratio": take_profit_ratios[0]},
                {"target": target2, "ratio": take_profit_ratios[1]},
                {"target": target3, "ratio": take_profit_ratios[2]},
            ]

            action = "hold"
            reasons = []
            if pnl_pct is not None:
                if pnl_pct < -0.10 and risk_level == "high":
                    action = "reduce"
                    reasons.append("loss > 10% with high volatility")
                elif pnl_pct > 0.20 and risk_level in ("medium", "high"):
                    action = "reduce"
                    reasons.append("gain > 20% with elevated volatility")

            if support and current_price and support > 0:
                if (current_price - support) / support < 0.02 and action == "hold":
                    action = "add"
                    reasons.append("price near support")

            if resistance and current_price and resistance > 0:
                if (resistance - current_price) / resistance < 0.02 and action == "hold":
                    action = "reduce"
                    reasons.append("price near resistance")

            return {
                "symbol": symbol,
                "as_of": _iso_now(),
                "price": {
                    "current": current_price,
                    "avg_cost": avg_cost,
                    "quantity": quantity,
                },
                "pnl": {
                    "amount": round(pnl_amount, 4),
                    "pct": round(pnl_pct, 6) if pnl_pct is not None else None,
                },
                "risk_levels": {
                    "support": support,
                    "resistance": resistance,
                    "stop_loss": stop_loss,
                },
                "targets": {
                    "target_1": target1,
                    "target_2": target2,
                    "target_3": target3,
                    "rr": round(rr, 4) if rr is not None else None,
                },
                "exit_plan": {
                    "take_profit": take_profit,
                },
                "rebalance": {
                    "action": action,
                    "reasons": reasons,
                    "market_risk": risk_level,
                },
            }


    class VolatilityTool(BaseTool):
        """Max drawdown (CoinGecko) + volatility/sharpe/beta (Binance)."""

        name: str = "get_volatility_metrics"
        description: str = "Compute max drawdown, volatility, Sharpe, and beta vs BTC."
        parameters: dict = _tool_schema(
            {
                "symbol": {"type": "string", "description": "Token symbol, e.g., BTC, ETH."},
                "interval": {"type": "string", "description": "Binance kline interval, e.g., 1m, 5m, 1h."},
                "limit": {"type": "number", "description": "Kline limit (max 1000)."},
                "long_days": {"type": "number", "description": "CoinGecko long-term days for drawdown."},
                "risk_free_rate": {"type": "number", "description": "Annual risk-free rate (e.g., 0.03)."},
                "include_klines": {"type": "boolean", "description": "Include kline data for advanced panel."},
            },
            required=["symbol"],
        )

        async def execute(
            self,
            symbol: str,
            interval: str = "1m",
            limit: int = 500,
            long_days: int = 365,
            risk_free_rate: Optional[float] = None,
            include_klines: bool = False,
        ) -> Dict[str, Any]:
            if httpx is None:
                raise RuntimeError("httpx is not installed. Install deps and retry.")

            symbol = symbol.upper()
            limit = max(10, min(int(limit), 1000))
            interval = interval or "1m"

            binance_symbol = symbol if symbol.endswith("USDT") else f"{symbol}USDT"
            btc_symbol = "BTCUSDT"

            klines = await _binance_get(
                "/api/v3/klines",
                params={"symbol": binance_symbol, "interval": interval, "limit": limit},
            )
            btc_klines = await _binance_get(
                "/api/v3/klines",
                params={"symbol": btc_symbol, "interval": interval, "limit": limit},
            )

            prices = [float(item[4]) for item in klines if isinstance(item, list) and len(item) > 4]
            btc_prices = [float(item[4]) for item in btc_klines if isinstance(item, list) and len(item) > 4]

            returns = _pct_returns(prices)
            btc_returns = _pct_returns(btc_prices)
            length = min(len(returns), len(btc_returns))
            returns = returns[-length:] if length else returns
            btc_returns = btc_returns[-length:] if length else btc_returns

            vol = _std(returns)
            ann_factor = _annualization_factor(interval) or 0.0
            ann_vol = vol * (ann_factor ** 0.5) if vol is not None and ann_factor else None

            rf = float(risk_free_rate) if risk_free_rate is not None else 0.0
            sharpe = None
            if vol is not None and vol > 0 and ann_factor:
                rf_per_period = rf / ann_factor
                mean_ret = _mean(returns) or 0.0
                sharpe = ((mean_ret - rf_per_period) / vol) * (ann_factor ** 0.5)

            beta = None
            if returns and btc_returns and len(returns) > 1:
                mean_a = _mean(returns) or 0.0
                mean_b = _mean(btc_returns) or 0.0
                cov = sum(
                    (a - mean_a) * (b - mean_b) for a, b in zip(returns, btc_returns)
                ) / (len(returns) - 1)
                var_b = _std(btc_returns)
                if var_b is not None:
                    var_b = var_b**2
                beta = cov / var_b if var_b else None

            coin_id = await _resolve_coin_id(symbol)
            chart_long = await _cg_get(
                f"/coins/{coin_id}/market_chart", params={"vs_currency": "usd", "days": str(long_days)}
            )
            long_prices = _series_from_prices(chart_long.get("prices", []))
            max_dd = _max_drawdown(long_prices)

            payload = {
                "symbol": symbol,
                "as_of": _iso_now(),
                "source": {"binance": BINANCE_BASE_URL, "coingecko": COINGECKO_BASE_URL},
                "max_drawdown": {
                    "period_days": long_days,
                    "max_drawdown": max_dd,
                    "interpretation": "Worst historical peak-to-trough loss over long window.",
                },
                "volatility": {
                    "interval": interval,
                    "sample_size": len(returns),
                    "volatility_pct": round(vol * 100, 4) if vol is not None else None,
                    "annualized_volatility_pct": round(ann_vol * 100, 4) if ann_vol is not None else None,
                    "risk_level": _risk_level((vol * 100) if vol is not None else None),
                },
                "sharpe_ratio": {
                    "risk_free_rate": rf,
                    "value": round(sharpe, 4) if sharpe is not None else None,
                    "note": "Annualized Sharpe using interval returns.",
                },
                "beta_vs_btc": {
                    "benchmark": btc_symbol,
                    "value": round(beta, 4) if beta is not None else None,
                },
            }

            if include_klines:
                compact = []
                for row in klines:
                    if not isinstance(row, list) or len(row) < 6:
                        continue
                    compact.append(
                        {
                            "open_time": row[0],
                            "open": float(row[1]),
                            "high": float(row[2]),
                            "low": float(row[3]),
                            "close": float(row[4]),
                            "volume": float(row[5]),
                        }
                    )
                payload["klines"] = {
                    "interval": interval,
                    "symbol": binance_symbol,
                    "items": compact,
                }

            return payload


    class TechnicalAnalysisTool(BaseTool):
        """Trend, momentum, volatility, and key levels (Binance)."""

        name: str = "get_technical_analysis"
        description: str = "Compute MA/EMA, RSI, MACD, Bollinger, support/resistance, and flow."
        parameters: dict = _tool_schema(
            {
                "symbol": {"type": "string", "description": "Token symbol, e.g., BTC, ETH."},
                "interval": {"type": "string", "description": "Kline interval, e.g., 1m, 5m, 1h."},
                "limit": {"type": "number", "description": "Kline limit (max 1000)."},
                "depth_limit": {"type": "number", "description": "Order book depth limit."},
                "trade_limit": {"type": "number", "description": "Recent trades limit."},
                "big_trade_usd": {"type": "number", "description": "Large trade notional threshold in USD."},
            },
            required=["symbol"],
        )

        async def execute(
            self,
            symbol: str,
            interval: str = "1h",
            limit: int = 200,
            depth_limit: int = 100,
            trade_limit: int = 200,
            big_trade_usd: float = 100000.0,
        ) -> Dict[str, Any]:
            if httpx is None:
                raise RuntimeError("httpx is not installed. Install deps and retry.")

            symbol = symbol.upper()
            limit = max(50, min(int(limit), 1000))
            depth_limit = max(5, min(int(depth_limit), 1000))
            trade_limit = max(10, min(int(trade_limit), 1000))
            big_trade_usd = max(0.0, float(big_trade_usd))

            binance_symbol = symbol if symbol.endswith("USDT") else f"{symbol}USDT"

            klines = await _binance_get(
                "/api/v3/klines",
                params={"symbol": binance_symbol, "interval": interval, "limit": limit},
            )
            closes = [float(item[4]) for item in klines if isinstance(item, list) and len(item) > 4]
            current_price = closes[-1] if closes else None

            ma_20 = _sma(closes, 20)
            ma_50 = _sma(closes, 50)
            ema_20 = _ema(closes, 20)
            ema_50 = _ema(closes, 50)
            ema_20_val = round(ema_20[-1], 6) if ema_20 else None
            ema_50_val = round(ema_50[-1], 6) if ema_50 else None

            trend = None
            if ma_20 is not None and ma_50 is not None:
                if ma_20 > ma_50:
                    trend = "up"
                elif ma_20 < ma_50:
                    trend = "down"
                else:
                    trend = "flat"

            rsi_14 = _rsi(closes, 14)
            rsi_state = None
            if rsi_14 is not None:
                if rsi_14 >= 70:
                    rsi_state = "overbought"
                elif rsi_14 <= 30:
                    rsi_state = "oversold"
                else:
                    rsi_state = "neutral"

            macd = _macd(closes)
            macd_state = None
            if macd.get("macd") is not None and macd.get("signal") is not None:
                macd_state = "bullish" if macd["macd"] > macd["signal"] else "bearish"

            boll = _bollinger(closes, 20, 2.0)
            boll_state = None
            boll_pos = None
            if current_price is not None and boll["upper"] and boll["lower"]:
                if current_price > boll["upper"]:
                    boll_state = "overheated"
                elif current_price < boll["lower"]:
                    boll_state = "oversold"
                else:
                    boll_state = "normal"
                denom = boll["upper"] - boll["lower"]
                if denom:
                    boll_pos = round((current_price - boll["lower"]) / denom, 4)

            price_levels = _support_resistance_from_prices(closes[-90:] if len(closes) >= 90 else closes)

            orderbook = await _binance_get(
                "/api/v3/depth",
                params={"symbol": binance_symbol, "limit": depth_limit},
            )
            bids = _depth_levels(orderbook, "bids", 5)
            asks = _depth_levels(orderbook, "asks", 5)
            best_bid = bids[0]["price"] if bids else None
            best_ask = asks[0]["price"] if asks else None

            trades = await _binance_get(
                "/api/v3/trades",
                params={"symbol": binance_symbol, "limit": trade_limit},
            )
            flow = _trade_flow(trades, big_trade_usd)

            funding_rate = None
            try:
                fr = await _binance_futures_get(
                    "/fapi/v1/fundingRate",
                    params={"symbol": binance_symbol, "limit": 1},
                )
                if isinstance(fr, list) and fr:
                    funding_rate = fr[-1]
            except Exception:
                funding_rate = None

            combined_support = best_bid if best_bid is not None else price_levels.get("support")
            combined_resistance = best_ask if best_ask is not None else price_levels.get("resistance")

            return {
                "symbol": symbol,
                "as_of": _iso_now(),
                "source": {
                    "binance": BINANCE_BASE_URL,
                    "binance_futures": BINANCE_FUTURES_BASE_URL,
                },
                "trend": {
                    "ma_20": ma_20,
                    "ma_50": ma_50,
                    "ema_20": ema_20_val,
                    "ema_50": ema_50_val,
                    "direction": trend,
                },
                "momentum": {
                    "rsi_14": rsi_14,
                    "state": rsi_state,
                },
                "trend_confirmation": {
                    "macd": macd,
                    "state": macd_state,
                },
                "volatility": {
                    "bollinger": boll,
                    "state": boll_state,
                    "position": boll_pos,
                },
                "support_resistance": {
                    "from_price": price_levels,
                    "from_orderbook": {"bids": bids, "asks": asks},
                    "combined": {"support": combined_support, "resistance": combined_resistance},
                },
                "orderbook_depth": {
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "bids": bids,
                    "asks": asks,
                },
                "large_trade_flow": flow,
                "funding_rate": funding_rate,
            }


else:
    MarketDataTool = None
    EcosystemTool = None
    SocialSentimentTool = None
    TargetAnalysisTool = None
    VolatilityTool = None
    TechnicalAnalysisTool = None
