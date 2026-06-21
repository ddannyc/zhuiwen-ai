"""Phase1 T1.1：procrastinate 队列地基 + tenant_session（RLS 显式注入）。

验收（tasks/plan.md T1.1）：
- queue_app 是 procrastinate.App（PsycopgConnector）。
- tenant_session(tenant_id) 显式设 app.current_tenant GUC（worker 无请求 ContextVar，
  租户必须由 task 参数显式传——同旧 Temporal activity 纪律）。
DB 不可达则整文件 skip。
"""
import psycopg
import pytest
from sqlalchemy import text

from app.core.config import get_settings


def _db_reachable() -> bool:
    try:
        url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
        with psycopg.connect(url, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_reachable(), reason="本地 Postgres(xborder) 不可达")


@pytest.fixture(autouse=True)
async def _fresh_engine():
    """asyncpg 连接与事件循环绑定，pytest 每测试用新 loop——dispose 强制重建。"""
    from app.core.database import engine
    await engine.dispose()
    yield


def test_queue_app_is_procrastinate():
    import procrastinate

    from app.shared.queue import queue_app

    assert isinstance(queue_app, procrastinate.App)


async def test_tenant_session_sets_guc():
    from app.shared.queue import tenant_session

    tid = "11111111-1111-1111-1111-111111111111"
    async with tenant_session(tid) as db:
        got = (
            await db.execute(text("SELECT current_setting('app.current_tenant', true)"))
        ).scalar()
    assert got == tid


async def test_tenant_session_guc_is_local_no_leak():
    """set_config is_local=true：会话归还连接池后租户不残留，防串租户。"""
    from app.shared.queue import tenant_session

    tid = "22222222-2222-2222-2222-222222222222"
    async with tenant_session(tid):
        pass
    # 新开一个无租户会话：GUC 不应残留上一个 tid（is_local 只在那个事务内）
    from app.core.database import _session_factory

    async with _session_factory() as s:
        got = (
            await s.execute(text("SELECT current_setting('app.current_tenant', true)"))
        ).scalar()
    assert got != tid
