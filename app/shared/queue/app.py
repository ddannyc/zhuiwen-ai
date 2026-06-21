"""procrastinate App 实例。

连接器用 PsycopgConnector（psycopg3 async）——procrastinate 无 asyncpg/async-SQLAlchemy
连接器，故与 app 业务的 asyncpg 会话不同源；不做单事务原子 defer，靠业务表 post_status
outbox + cron 兜底投递（见 docs/sourcing-client-migration.md ADR-001）。

conninfo 由 database_url 去掉 +asyncpg 驱动后缀得到（psycopg3 用 libpq DSN）。
procrastinate 自有表经迁移注入（见迁移 0004_procrastinate）。
"""
import procrastinate

from app.core.config import get_settings

_settings = get_settings()


def _conninfo() -> str:
    # postgresql+asyncpg://app:app@host:5433/xborder → postgresql://app:app@host:5433/xborder
    return _settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


queue_app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=_conninfo()),
)
