"""Alembic 執行環境。

- 連線網址來自 app.config（.env 的 DATABASE_URL），資料表結構以 app.models 為準
- 後端啟動時由 app/migrate.py 帶著現成的連線呼叫；開發時用 `uv run alembic ...` 會自己建立連線
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.models import Base

config = context.config
if config.config_file_name is not None:  # 只有指令列會帶 alembic.ini；後端啟動時沿用 app 的 logging
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite 不支援大部分 ALTER TABLE，改用「建新表、搬資料」的方式
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(settings.database_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
        await connection.commit()
    await engine.dispose()


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`：只輸出 SQL，不連資料庫。"""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
elif (connection := config.attributes.get("connection")) is not None:
    do_run_migrations(connection)
else:
    asyncio.run(run_async_migrations())
