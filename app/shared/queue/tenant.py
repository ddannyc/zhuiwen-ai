"""worker task 的租户上下文：显式传 tenant_id → 设 app.current_tenant GUC → 走 RLS。

worker 跨进程、无请求 ContextVar（不同于 core/database.get_session 读 ContextVar）。
故 task 必须把 tenant_id 作为显式参数传入——禁靠 ContextVar 隐式传递
（同旧 Temporal activity 纪律，见 sourcing/workflows.py 历史注释）。

set_config(..., is_local=true) 等价 SET LOCAL：只在当前事务内生效，连接归还池后
不残留，防串租户。用 set_config 而非 SET LOCAL，因 asyncpg 不支持 utility 语句参数化。
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import _session_factory


@asynccontextmanager
async def tenant_session(tenant_id: str) -> AsyncIterator[AsyncSession]:
    async with _session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": tenant_id},
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
