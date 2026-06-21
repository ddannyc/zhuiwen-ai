"""Phase4 T4.1：cron 兜底——扫掉队 pending/queued 批重投（ADR-001 outbox）。

验收：updated_at 超 grace 的 pending/queued 批被重投（post_status→queued、updated_at 刷新）；
未掉队（recent）的不动。真 PG 必需；不可达则 skip。
"""
import uuid

import psycopg
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.domains.sourcing.repository import SourcingRepository
from app.shared.queue import queue_app, tenant_session

_OFFER = "https://detail.1688.com/offer/1.html"


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


async def _seed(tenant: str, batch_id: str, *, backdate: int) -> None:
    async with tenant_session(tenant) as db:
        await SourcingRepository(db).create_batch(
            batch_id=batch_id, urls=[_OFFER], options={}, market="1688"
        )
        # 回拨 updated_at 模拟掉队
        await db.execute(
            text("UPDATE collect_jobs SET updated_at = now() - make_interval(secs => :s) WHERE id = :i"),
            {"s": backdate, "i": batch_id},
        )


async def _post_status(tenant: str, batch_id: str) -> str:
    async with tenant_session(tenant) as db:
        return (await SourcingRepository(db).get_job(batch_id)).post_status


async def test_requeue_picks_stale_skips_fresh():
    from app.domains.sourcing.cron import requeue_stale_pending

    tenant = str(uuid.uuid4())
    stale = str(uuid.uuid4())
    fresh = str(uuid.uuid4())
    await _seed(tenant, stale, backdate=600)  # 掉队
    await _seed(tenant, fresh, backdate=0)     # 新鲜

    async with queue_app.open_async():
        n = await requeue_stale_pending(grace_seconds=120)

    assert n >= 1
    assert await _post_status(tenant, stale) == "queued"   # 重投
    assert await _post_status(tenant, fresh) == "pending"  # 未动


async def test_requeue_recovers_stuck_queued():
    """掉队的 queued（worker 丢了）也要被重投，防卡死。"""
    from app.domains.sourcing.cron import requeue_stale_pending

    tenant = str(uuid.uuid4())
    stuck = str(uuid.uuid4())
    await _seed(tenant, stuck, backdate=600)
    async with tenant_session(tenant) as db:
        await SourcingRepository(db).set_post_status(stuck, "queued")
        await db.execute(
            text("UPDATE collect_jobs SET updated_at = now() - make_interval(secs => 600) WHERE id = :i"),
            {"i": stuck},
        )

    async with queue_app.open_async():
        await requeue_stale_pending(grace_seconds=120)

    assert await _post_status(tenant, stuck) == "queued"  # 仍 queued 但已重投（updated_at 刷新）
