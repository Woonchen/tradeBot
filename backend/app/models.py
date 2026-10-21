from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    """SQLite 取回的是 naive datetime；所有時間一律以 UTC 儲存，輸出時補上時區。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class Base(DeclarativeBase):
    def to_dict(self) -> dict:
        out = {}
        for col in self.__table__.columns:
            v = getattr(self, col.key)
            out[col.key] = iso(v) if isinstance(v, datetime) else v
        return out


class Signal(Base):
    """TradingView（或手動操作）送進來的每一個訊號。"""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(20), default="tradingview")  # tradingview / manual
    strategy: Mapped[str] = mapped_column(String(64), default="")
    ticker: Mapped[str] = mapped_column(String(40))  # 原始 ticker，例如 BTCUSDT.P
    symbol: Mapped[str | None] = mapped_column(String(40))  # 解析後的 ccxt symbol
    market: Mapped[str] = mapped_column(String(10))  # spot / futures
    action: Mapped[str] = mapped_column(String(10))  # buy/sell/long/short/close
    target: Mapped[str] = mapped_column(String(10))  # 目標部位 long/short/flat
    price: Mapped[float | None] = mapped_column(Float)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="accepted", index=True)
    reject_reason: Mapped[str | None] = mapped_column(Text)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64))
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    market: Mapped[str] = mapped_column(String(10))
    side: Mapped[str] = mapped_column(String(4))  # buy / sell
    type: Mapped[str] = mapped_column(String(16), default="market")
    qty: Mapped[float] = mapped_column(Float)
    filled_qty: Mapped[float] = mapped_column(Float, default=0)
    avg_price: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(12), default="new", index=True)  # new/open/filled/canceled/rejected
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    intent: Mapped[str] = mapped_column(String(12))  # open_long / close_long / open_short / close_short
    leverage: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Trade(Base):
    """實際成交紀錄，K 線上的買賣點就是從這張表畫出來的。"""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 交易所自行平倉（原生停損、強平、手動在交易所操作）時沒有對應的 order
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    exchange_trade_id: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    market: Mapped[str] = mapped_column(String(10), index=True)
    side: Mapped[str] = mapped_column(String(4))
    # open_long / close_long / open_short / close_short
    intent: Mapped[str] = mapped_column(String(12))
    price: Mapped[float] = mapped_column(Float)
    qty: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0)
    fee_asset: Mapped[str] = mapped_column(String(12), default="USDT")
    fee_usdt: Mapped[float] = mapped_column(Float, default=0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Position(Base):
    """機器人自己的部位帳本；closed_at 為空代表仍持倉。"""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    market: Mapped[str] = mapped_column(String(10))
    side: Mapped[str] = mapped_column(String(5))  # long / short
    qty: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    mark_price: Mapped[float | None] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0)
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    liquidation_price: Mapped[float | None] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float)
    take_profit_price: Mapped[float | None] = mapped_column(Float)
    # True = 停損停利單掛在交易所；False = 由 position_sync 軟體監控
    native_protection: Mapped[bool] = mapped_column(Boolean, default=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    market: Mapped[str] = mapped_column(String(10))
    asset: Mapped[str] = mapped_column(String(16))
    free: Mapped[float] = mapped_column(Float)
    locked: Mapped[float] = mapped_column(Float)
    usdt_value: Mapped[float] = mapped_column(Float)


class EquitySnapshot(Base):
    """錢包總權益曲線（現貨資產市值 + 合約保證金餘額含未實現損益）。"""

    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    spot_usdt: Mapped[float] = mapped_column(Float)
    futures_usdt: Mapped[float] = mapped_column(Float)
    total_usdt: Mapped[float] = mapped_column(Float)


class ActivityLog(Base):
    """機器人的操作軌跡。"""

    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")  # info / warn / error
    category: Mapped[str] = mapped_column(String(12))  # signal / order / trade / risk / system
    message: Mapped[str] = mapped_column(Text)
    signal_id: Mapped[int | None] = mapped_column(Integer, index=True)
    order_id: Mapped[int | None] = mapped_column(Integer)


class BotSettings(Base):
    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 允許交易的 ticker（例如 ["BTCUSDT","ETHUSDT"]）；空 = 全部允許
    symbols: Mapped[list] = mapped_column(JSON, default=list)
    # fixed：每筆固定 size_value USDT 保證金；percent：可用 USDT 的 size_value %
    size_mode: Mapped[str] = mapped_column(String(8), default="fixed")
    size_value: Mapped[float] = mapped_column(Float, default=100)
    leverage: Mapped[int] = mapped_column(Integer, default=3)
    margin_mode: Mapped[str] = mapped_column(String(8), default="isolated")  # isolated / cross
    max_open_positions: Mapped[int] = mapped_column(Integer, default=5)
    max_daily_loss_usdt: Mapped[float] = mapped_column(Float, default=200)
    # 開倉後自動掛的保護單（%，0 = 不掛），僅合約
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=0)
    take_profit_pct: Mapped[float] = mapped_column(Float, default=0)


class StrategyConfig(Base):
    """一個正在（或可以）自動執行的策略：哪個策略、哪個交易對、哪個週期、什麼參數。"""

    __tablename__ = "strategy_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    strategy: Mapped[str] = mapped_column(String(32))  # REGISTRY 的 key
    symbol: Mapped[str] = mapped_column(String(40))  # 例如 BTCUSDT
    market: Mapped[str] = mapped_column(String(10))
    interval: Mapped[str] = mapped_column(String(4))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 執行狀態：最後一根處理過的 K 棒、當時策略的判斷
    last_target: Mapped[str | None] = mapped_column(String(5))
    last_bar_time: Mapped[int | None] = mapped_column(Integer)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
