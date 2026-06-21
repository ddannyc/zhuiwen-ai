"""Phase1 T1.2：procrastinate schema 迁移 + app 角色授权。

验收（tasks/plan.md T1.2）：alembic upgrade head 建出 procrastinate_jobs 等表，
且 worker 用的 app 角色（database_url，受 RLS 约束）能读写——否则队列连不动。
用 app 角色会话 SELECT procrastinate_jobs：一举验「表存在 + app 有授权」。
DB 不可达则 skip。
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
    from app.core.database import engine
    await engine.dispose()
    yield


async def test_app_role_can_access_procrastinate_jobs():
    """app 角色（业务连接）能 SELECT procrastinate_jobs —— 表存在 + 授权到位。"""
    from app.core.database import _session_factory

    async with _session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM procrastinate_jobs"))).scalar()
    assert n is not None


async def test_app_role_can_insert_into_procrastinate_jobs():
    """worker 要 INSERT/UPDATE/DELETE（fetch/defer）；验 app 写权限（回滚不留痕）。"""
    from app.core.database import _session_factory

    async with _session_factory() as s:
        # 仅探权限：插一行再回滚，不污染队列
        await s.execute(
            text(
                "INSERT INTO procrastinate_jobs (task_name, queue_name, args) "
                "VALUES ('t1_2_probe', 'probe', '{}'::jsonb)"
            )
        )
        await s.rollback()
