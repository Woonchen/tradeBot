"""後端啟動時把資料庫升級到最新結構。

改了 app/models.py 之後，產生新的 migration 並檢查內容再 commit：
    uv run alembic revision --autogenerate -m "說明這次改了什麼"
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

# 導入 Alembic 前，由 Base.metadata.create_all 建立的結構
BASELINE = "0001"


def upgrade(connection: Connection) -> None:
    """在 AsyncConnection.run_sync 裡呼叫；整個升級在同一個交易裡，失敗就全部還原。"""
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.attributes["connection"] = connection
    tables = set(inspect(connection).get_table_names())
    if "alembic_version" not in tables and "signals" in tables:
        # 導入 Alembic 之前就存在的資料庫：結構就是 baseline，先標記版本再往上升級
        command.stamp(cfg, BASELINE)
    command.upgrade(cfg, "head")
