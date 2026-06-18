"""数据库会话与多租户隔离的核心。

隔离的命脉在这里：每个请求拿到一个 DB session 后，把 tenant_id 注入
PostgreSQL 的会话变量 `app.current_tenant`，RLS 策略据此自动过滤所有查询。
任何业务代码都不需要、也不应该自己写 `WHERE tenant_id = ...`。
"""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, pool_size=20, max_overflow=10)
_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# 当前请求的租户上下文，由中间件设置（见 shared/tenant/middleware.py）
current_tenant_id: ContextVar[str | None] = ContextVar("current_tenant_id", default=None)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """获取一个已注入租户上下文的 DB 会话。

    这是全应用唯一的会话入口。HTTP 请求和 worker 任务都走这里，
    保证 RLS 变量在任何执行路径下都被设置。
    """
    tenant_id = current_tenant_id.get()
    async with _session_factory() as session:
        if tenant_id is not None:
            # SET LOCAL 只在当前事务内生效，连接归还连接池后不残留 —— 防止串租户
            await session.execute(
                text("SET LOCAL app.current_tenant = :tid"), {"tid": tenant_id}
            )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入用的版本。"""
    async with get_session() as session:
        yield session
