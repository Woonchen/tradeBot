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


async def test_existing_create_all_database_is_stamped_and_kept(tmp_path):
    """導入 Alembic 前用 create_all 建的資料庫：標記成 baseline 再升級，資料不動。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        assert "alembic_version" in tables
    await engine.dispose()
