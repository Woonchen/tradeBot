"""市場情緒資料：抓取、存進資料庫、提供給策略與儀表板。

- 恐懼貪婪指數（Alternative.me）：第一次抓 2018 年起的全部歷史，之後每天更新
- 資金費率（Binance 永續合約）：某個幣第一次用到時補齊 2019-09 起的全部歷史，之後只抓新的
- 散戶多空比、大戶多空比、未平倉量、主動買賣比：Binance 只保留 30 天，每小時把最近 30 天
  寫進資料庫（重複的會覆蓋），日積月累成自己的歷史；後端停機一陣子，只要還在 30 天內也會補回來
"""

import logging
import time

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..exchange.base import normalize_ticker
from ..models import FearGreed, FundingRate, MarketMetric
from ..strategies.external import EXTERNAL_KINDS

log = logging.getLogger(__name__)

FNG_URL = "https://api.alternative.me/fng/"
FAPI = "https://fapi.binance.com"
FUNDING_START_MS = 1567296000000  # 2019-09-01，Binance USDT 永續合約開始的時間
DAY = 86400

# metric → (Binance 端點, 取哪個欄位)
METRICS = {
    "long_short": ("globalLongShortAccountRatio", "longShortRatio"),
    "top_long_short": ("topLongShortPositionRatio", "longShortRatio"),
    "open_interest": ("openInterestHist", "sumOpenInterestValue"),
    "taker_ratio": ("takerlongshortRatio", "buySellRatio"),
}


async def upsert(s: AsyncSession, model, rows: list[dict]) -> None:
    """寫入；主鍵已存在就更新數值（PostgreSQL 與 SQLite 都支援 ON CONFLICT）。"""
    if not rows:
        return
    if s.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    keys = [c.name for c in model.__table__.primary_key.columns]
    for i in range(0, len(rows), 500):
        stmt = insert(model).values(rows[i:i + 500])
        values = {c: stmt.excluded[c] for c in rows[0] if c not in keys}
        await s.execute(stmt.on_conflict_do_update(index_elements=keys, set_=values))


async def tracked(s: AsyncSession) -> list[str]:
    """要記錄的幣：允許交易清單、啟用中策略用到的幣，再加 BTC、ETH。"""
    from ..engine.risk import load_bot_settings
    from ..models import StrategyConfig

    bot = await load_bot_settings(s)
    syms = list(bot.symbols or [])
    syms += [c.symbol for c in (await s.scalars(select(StrategyConfig).where(StrategyConfig.enabled.is_(True)))).all()]
    return sorted({normalize_ticker(x) for x in syms} | {"BTCUSDT", "ETHUSDT"})


async def tracked_tickers(sf: async_sessionmaker) -> list[str]:
    async with sf() as s:
        return await tracked(s)


class Sentiment:
    def __init__(self, sf: async_sessionmaker, http: httpx.AsyncClient | None = None):
        self.sf = sf
        self.http = http or httpx.AsyncClient(timeout=20, headers={"User-Agent": "tradebot"})
        self._checked: dict[str, float] = {}  # 同一份資料多久最多向外抓一次

    async def close(self):
        await self.http.aclose()

    async def _get(self, url: str, params: dict | None = None):
        r = await self.http.get(url, params=params)
        r.raise_for_status()
        return r.json()

    def _due(self, key: str, every: float) -> bool:
        if time.time() - self._checked.get(key, 0) < every:
            return False
        self._checked[key] = time.time()
        return True

    # ------------------------------------------------------------ 恐懼貪婪
    async def refresh_fear_greed(self) -> None:
        data = (await self._get(FNG_URL, {"limit": 0, "format": "json"}))["data"]
        rows = [{"ts": int(d["timestamp"]), "value": int(d["value"]), "classification": d["value_classification"]} for d in data]
        async with self.sf() as s:
            await upsert(s, FearGreed, rows)
            await s.commit()

    async def fear_greed_series(self) -> list[tuple[int, float]]:
        async with self.sf() as s:
            latest = await s.scalar(select(func.max(FearGreed.ts)))
        if (latest is None or latest < time.time() - 1.5 * DAY) and self._due("fng", 600):
            try:
                await self.refresh_fear_greed()
            except Exception as e:  # 抓不到就用資料庫裡已有的
                log.warning("fear & greed refresh failed: %r", e)
        async with self.sf() as s:
            return [(r.ts, float(r.value)) for r in (await s.execute(select(FearGreed.ts, FearGreed.value).order_by(FearGreed.ts))).all()]

    # ------------------------------------------------------------ 資金費率
    async def refresh_funding(self, ticker: str) -> None:
        async with self.sf() as s:
            last = await s.scalar(select(func.max(FundingRate.ts)).where(FundingRate.symbol == ticker))
        start = (last + 1) * 1000 if last else FUNDING_START_MS
        while True:
            data = await self._get(f"{FAPI}/fapi/v1/fundingRate", {"symbol": ticker, "startTime": start, "limit": 1000})
            if not data:
                break
            rows = [{"symbol": ticker, "ts": int(d["fundingTime"]) // 1000, "rate": float(d["fundingRate"])} for d in data]
            async with self.sf() as s:
                await upsert(s, FundingRate, rows)
                await s.commit()
            if len(data) < 1000:
                break
            start = int(data[-1]["fundingTime"]) + 1

    async def funding_series(self, symbol: str) -> list[tuple[int, float]]:
        """(結算時間, 每 8 小時的費率 %)，例如 0.01 = 0.01%。"""
        ticker = normalize_ticker(symbol)
        async with self.sf() as s:
            latest = await s.scalar(select(func.max(FundingRate.ts)).where(FundingRate.symbol == ticker))
        if (latest is None or latest < time.time() - 9 * 3600) and self._due(f"funding:{ticker}", 300):
            try:
                await self.refresh_funding(ticker)
            except Exception as e:
                log.warning("funding refresh failed for %s: %r", ticker, e)
        async with self.sf() as s:
            q = select(FundingRate.ts, FundingRate.rate).where(FundingRate.symbol == ticker).order_by(FundingRate.ts)
            return [(r.ts, r.rate * 100) for r in (await s.execute(q)).all()]

    # ------------------------------------------------------------ 30 天的合約數據
    async def record_metrics(self, tickers: list[str]) -> None:
        for ticker in tickers:
            rows = []
            for metric, (path, field) in METRICS.items():
                try:
                    data = await self._get(f"{FAPI}/futures/data/{path}", {"symbol": ticker, "period": "1d", "limit": 30})
                except httpx.HTTPError as e:  # 只有現貨、沒有合約的幣會失敗：略過
                    log.info("metric %s for %s unavailable: %r", metric, ticker, e)
                    continue
                rows += [{"symbol": ticker, "metric": metric, "ts": int(d["timestamp"]) // 1000, "value": float(d[field])} for d in data]
            async with self.sf() as s:
                await upsert(s, MarketMetric, rows)
                await s.commit()

    async def refresh_all(self, tickers: list[str]) -> None:
        """背景排程：每小時跑一次。"""
        await self.fear_greed_series()
        for t in tickers:
            await self.funding_series(t)
        await self.record_metrics(tickers)

    # ------------------------------------------------------------ 給策略用
    async def attach(self, strategy, symbol: str) -> None:
        """策略用到外部資料時，載入到 strategy.ext（計算時再對齊到每根 K 棒）。"""
        needs = strategy.external_needs() if hasattr(strategy, "external_needs") else set()
        ext: dict[str, list[tuple[int, float]]] = {}
        if "fear_greed" in needs:
            ext["fear_greed"] = await self.fear_greed_series()
        if "funding_rate" in needs:
            ext["funding_rate"] = await self.funding_series(symbol)
        strategy.ext = ext


assert set(EXTERNAL_KINDS) == {"fear_greed", "funding_rate"}  # 新增種類時，attach 也要跟著處理
