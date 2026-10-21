from fastapi import APIRouter, Depends, HTTPException, Query

from ..engine.executor import Executor
from ..exchange.base import UnknownSymbol
from .deps import get_executor, require_user

router = APIRouter(prefix="/api", tags=["market"], dependencies=[Depends(require_user)])

INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"}


@router.get("/klines")
async def klines(
    symbol: str,
    market: str = "spot",
    interval: str = "1h",
    limit: int = Query(500, le=1500),
    executor: Executor = Depends(get_executor),
):
    if interval not in INTERVALS:
        raise HTTPException(422, f"interval 必須是 {sorted(INTERVALS)} 之一")
    adapter = executor.adapters.get(market)
    if adapter is None:
        raise HTTPException(422, "market 必須是 spot 或 futures")
    try:
        sym = await adapter.resolve_symbol(symbol)
    except UnknownSymbol:
        raise HTTPException(404, f"找不到交易對 {symbol}")
    rows = await adapter.fetch_ohlcv(sym, interval, limit)
    return {
        "symbol": sym,
        "candles": [
            {"time": r[0] // 1000, "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
            for r in rows
        ],
    }
