"""Phase1 T1.3 / 检查点 C1：procrastinate 队列端到端 + RLS 隔离。

验收（tasks/plan.md T1.3）：defer 一个 task → worker 执行 → 行落正确租户，
另一租户 RLS 查不到。证「队列地基跑通」+「worker 内 tenant_session 注入 RLS 生效」。

ping_probe 是临时探针 task（Phase2 起被真实 post_process 取代）。
真 PG 必需（RLS）；不可达则 skip。
"""
import uuid

import psycopg
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.shared.queue import queue_app, tenant_session


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


@queue_app.task(name="ping_probe")
async def ping_probe(tenant_id: str, marker: str) -> None:
    """探针：worker 内经 tenant_session 设租户上下文，往 collect_jobs 落一行。
    tenant_id 不手填——靠 collect_jobs.tenant_id DEFAULT current_setting('app.current_tenant')。"""
    async with tenant_session(tenant_id) as db:
        await db.execute(
            text("INSERT INTO collect_jobs (market) VALUES (:m)"), {"m": marker}
        )


async def _count(tenant_id: str, marker: str) -> int:
    async with tenant_session(tenant_id) as db:
        return (
            await db.execute(
                text("SELECT count(*) FROM collect_jobs WHERE market = :m"), {"m": marker}
            )
        ).scalar()


async def test_defer_worker_writes_under_tenant_rls():
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())
    marker = "probe-" + uuid.uuid4().hex[:10]

    async with queue_app.open_async():
        await ping_probe.defer_async(tenant_id=tenant_a, marker=marker)
        # wait=False：处理完积压 job 即退出，确定性。
        await queue_app.run_worker_async(wait=False, install_signal_handlers=False)

    # 行落租户 A；租户 B 经 RLS 查不到。
    assert await _count(tenant_a, marker) == 1
    assert await _count(tenant_b, marker) == 0
