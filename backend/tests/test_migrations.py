from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app import migrate
from app.models import Base


def head() -> str:
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", "app/migrations")
    return ScriptDirectory.from_config(cfg).get_current_head()


def current(conn) -> str | None:
    return MigrationContext.configure(conn).get_current_revision()


def diff(conn) -> list:
    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    return compare_metadata(ctx, Base.metadata)


async def test_migrations_match_models(tmp_path):
    """從空資料庫跑完所有 migration，結構要跟 models.py 完全一樣。

    改了 models.py 卻忘了產生 migration，這個測試會失敗。
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(migrate.upgrade)
    async with engine.connect() as conn:
        assert await conn.run_sync(current) == head()
        assert await conn.run_sync(diff) == []
    await engine.dispose()


def build_pre_alembic_db(conn) -> None:
    """做出「導入 Alembic 前」的資料庫：baseline（0001）的結構、沒有 alembic_version 表。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", "app/migrations")
    cfg.attributes["connection"] = conn
    command.upgrade(cfg, migrate.BASELINE)
    conn.execute(text("DROP TABLE alembic_version"))


async def test_existing_create_all_database_is_stamped_and_kept(tmp_path):
    """導入 Alembic 前用 create_all 建的資料庫：標記成 baseline 再升級到最新，資料不動。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(build_pre_alembic_db)
        await conn.execute(text(
            "INSERT INTO activity_logs (ts, level, category, message) VALUES ('2026-01-01', 'info', 'system', 'keep me')"
        ))
    async with engine.begin() as conn:
        await conn.run_sync(migrate.upgrade)
    async with engine.begin() as conn:  # 再啟動一次：已經是最新版，什麼都不做
        await conn.run_sync(migrate.upgrade)
    async with engine.connect() as conn:
        assert await conn.run_sync(current) == head()
        assert (await conn.execute(text("SELECT message FROM activity_logs"))).scalar_one() == "keep me"
        assert await conn.run_sync(diff) == []  # 升級後的結構跟 models.py 一致
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        assert "alembic_version" in tables
    await engine.dispose()


async def test_0002_backfills_enabled_at_for_enabled_strategies(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(build_pre_alembic_db)
        await conn.execute(text(
            "INSERT INTO strategy_configs (id, name, strategy, symbol, market, interval, params, enabled, created_at) VALUES "
            "(1, 'on', 'x', 'BTCUSDT', 'futures', '15m', '{}', 1, '2026-01-01 00:00:00'),"
            "(2, 'off', 'x', 'ETHUSDT', 'futures', '15m', '{}', 0, '2026-01-02 00:00:00')"
        ))
    async with engine.begin() as conn:
        await conn.run_sync(migrate.upgrade)
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, enabled_at FROM strategy_configs ORDER BY id"))).all()
    assert [(i, str(v)[:10] if v else None) for i, v in rows] == [(1, "2026-01-01"), (2, None)]
    await engine.dispose()
