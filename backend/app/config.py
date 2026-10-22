from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    binance_api_key: str = ""
    binance_api_secret: str = ""
    testnet: bool = True
    execution_mode: Literal["paper", "binance"] = "paper"
    paper_start_usdt: float = 10_000

    webhook_secret: str = "change-me"
    # 逗號分隔；留空 = 不檢查。TradingView 官方發送 IP 見 README
    webhook_ip_allowlist: str = ""

    database_url: str = "sqlite+aiosqlite:///./tradebot.db"

    dashboard_user: str = "admin"
    dashboard_password: str = "change-me"
    jwt_secret: str = "change-me-too"
    jwt_ttl_hours: int = 24

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    balance_sync_seconds: int = 60
    position_sync_seconds: int = 15
    order_sync_seconds: int = 20

    def insecure_secrets(self) -> list[str]:
        """還是預設值或空白的密鑰。空白的 webhook secret 會讓任何人送空字串就通過驗證。"""
        defaults = Settings.model_fields
        return [
            name.upper()
            for name in ("webhook_secret", "dashboard_password", "jwt_secret")
            if not getattr(self, name).strip() or getattr(self, name) == defaults[name].default
        ]


settings = Settings()
