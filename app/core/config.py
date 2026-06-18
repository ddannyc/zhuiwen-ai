"""全局配置。所有环境变量在这里集中读取，其他模块不直接读 os.environ。"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- 基础 ---
    app_env: str = "dev"
    debug: bool = True

    # --- Postgres (业务库 + pgvector) ---
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/xborder"
    # 用于执行迁移 / RLS 策略的超级权限连接（与业务连接分开）
    database_admin_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/xborder"

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Auth (JWT) ---
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"

    # --- LLM 网关 (LiteLLM) ---
    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str = "change-me"

    # --- 规则知识库 (rules_kb) ---
    # 本期最小实现读这个 jsonl 种子语料；后续换 Postgres+pgvector 时弃用。
    rules_kb_path: str = "data/rules_kb/ozon_rules.jsonl"

    # --- 可观测 (Langfuse) ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
