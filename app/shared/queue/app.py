"""procrastinate App 实例。

连接器用 PsycopgConnector（psycopg3 async）——procrastinate 无 asyncpg/async-SQLAlchemy
连接器，故与 app 业务的 asyncpg 会话不同源；不做单事务原子 defer，靠业务表 post_status
outbox + cron 兜底投递（见 docs/sourcing-client-migration.md ADR-001）。

conninfo 由 database_url 去掉 +asyncpg 驱动后缀得到（psycopg3 用 libpq DSN）。
procrastinate 自有表经迁移注入（见迁移 0004_procrastinate）。
"""
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

import procrastinate

from app.core.config import get_settings

_settings = get_settings()


def _conninfo() -> str:
    # postgresql+asyncpg://app:app@host:5433/xborder → postgresql://app:app@host:5433/xborder
    return _settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


queue_app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=_conninfo()),
)

# 进程级「app 是否已开」标记：API/worker 用 lifespan_open 在启动时开一次 queue_app，
# 之后 defer 复用同一连接池，不必每请求开/关（review #2）。
_app_open = False


@asynccontextmanager
async def lifespan_open():
    """API/worker 生命周期内开一次 queue_app（FastAPI lifespan / worker 入口用）。"""
    global _app_open
    async with queue_app.open_async():
        _app_open = True
        try:
            yield
        finally:
            _app_open = False


async def ensure_defer(defer: Callable[[], Awaitable[None]]) -> None:
    """执行一次 defer：app 已开（生产 lifespan）→ 直接 defer 复用连接；
    未开（测试 ASGITransport / 脚本）→ 临时开一次。"""
    if _app_open:
        await defer()
    else:
        async with queue_app.open_async():
            await defer()
