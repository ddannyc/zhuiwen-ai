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

    # --- CORS ---
    # 精确白名单（CSV）。生产设为真实前端域名。
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # 开发期私网放行正则：localhost / 127.0.0.1 / 私有网段（192.168.x.x、10.x.x.x、
    # 172.16–31.x.x）的 vite 端口（5173 dev / 4173 preview）。换设备/IP 调试无需逐个手填。
    # 生产应清空（CORS_ORIGIN_REGEX=），只用精确白名单。
    cors_origin_regex: str = (
        r"https?://(localhost|127\.0\.0\.1|"
        r"(?:192\.168|10\.\d+|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d+){1,2}"
        r"):(?:5173|4173)"
    )

    # --- LLM 网关 ---
    # chat 走 LiteLLM SDK 进程内调用（gateway.py），默认打 DashScope 兼容端点。
    chat_model: str = "qwen-plus"
    dashscope_api_key: str = ""  # 阿里百炼 Key（DashScope 兼容端点）
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # ↑ 本地无 key 调试：把它指向 mock（http://localhost:4000），api_key 随意。

    # embeddings 仍走 OpenAI 兼容端点（knowledge_base 域用，本期未启用）
    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str = "change-me"

    # --- Temporal (sourcing 采集长流程) ---
    temporal_host: str = "localhost:7233"
    sourcing_task_queue: str = "sourcing"
    # Temporal 不可达时 sourcing 走降级：直接写 pending job 行，chat 路径不阻塞、
    # 采集插件仍可经 /sourcing/jobs/poll 取任务。生产应保证 Temporal 可用以获得
    # durable/重试/断点恢复。连接探活超时（秒），避免 chat 请求被长时间阻塞。
    temporal_connect_timeout: float = 3.0

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
