# app/core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding='utf-8',
        extra="ignore"
    )

    APP_NAME: str = "notion-2api"
    APP_VERSION: str = "4.0.0" # 最终稳定版
    DESCRIPTION: str = "一个将 Notion AI 转换为兼容 OpenAI 格式 API 的高性能代理。"

    API_MASTER_KEY: Optional[str] = None

    # --- Notion 凭证 ---
    NOTION_COOKIE: Optional[str] = None
    NOTION_SPACE_ID: Optional[str] = None
    NOTION_USER_ID: Optional[str] = None
    NOTION_USER_NAME: Optional[str] = None
    NOTION_USER_EMAIL: Optional[str] = None
    NOTION_PASSWORD: Optional[str] = None  # Notion 帐号密码，用于自动刷新 token
    NOTION_BLOCK_ID: Optional[str] = None
    NOTION_CLIENT_VERSION: Optional[str] = "23.13.20260217.0001"

    API_REQUEST_TIMEOUT: int = 180
    NGINX_PORT: int = 8088
    SESSION_KEEPALIVE_INTERVAL: int = 300  # 会话保活间隔（秒），默认 5 分钟

    # 更新所有已知的模型列表（匹配 Notion AI 当前支持的模型）
    DEFAULT_MODEL: str = "claude-sonnet-4.5"
    
    KNOWN_MODELS: List[str] = [
        "claude-sonnet-4.5",
        "claude-opus-4.6",
        "gemini-3-pro",
        "gpt-5.2"
    ]
    
    # 模型名称到 Notion 后端模型 ID 的映射
    MODEL_MAP: dict = {
        "claude-sonnet-4.5": "anthropic-sonnet-alt",
        "claude-opus-4.6": "avocado-froyo-medium",
        "gemini-3-pro": "gateau-roule",
        "gpt-5.2": "oatmeal-cookie"
    }

settings = Settings()