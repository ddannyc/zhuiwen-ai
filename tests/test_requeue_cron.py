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


async def test_requeue_reclaims_crashed_running():
    """T4.2：崩在 running 超 running_grace 的批被回收（worker 崩溃恢复）。"""
    from app.domains.sourcing.cron import requeue_stale_pending

    tenant = str(uuid.uuid4())
    crashed = str(uuid.uuid4())
    await _seed(tenant, crashed, backdate=0)
    async with tenant_session(tenant) as db:
        await SourcingRepository(db).set_post_status(crashed, "running")
        await db.execute(
            text("UPDATE collect_jobs SET updated_at = now() - make_interval(secs => 1000) WHERE id = :i"),
            {"i": crashed},
        )

    async with queue_app.open_async():
        n = await requeue_stale_pending(grace_seconds=120, running_grace_seconds=900)

    assert n >= 1
    assert await _post_status(tenant, crashed) == "queued"  # 回收重投


async def test_requeue_skips_fresh_running():
    """正在跑的 running（updated_at 新）不被误回收（grace > 妙手 fetch 耗时）。"""
    from app.domains.sourcing.cron import requeue_stale_pending

    tenant = str(uuid.uuid4())
    running = str(uuid.uuid4())
    await _seed(tenant, running, backdate=200)  # 200s 前，仍在 running grace(900) 内
    async with tenant_session(tenant) as db:
        await SourcingRepository(db).set_post_status(running, "running")
        await db.execute(
            text("UPDATE collect_jobs SET updated_at = now() - make_interval(secs => 200) WHERE id = :i"),
            {"i": running},
        )

    async with queue_app.open_async():
        await requeue_stale_pending(grace_seconds=120, running_grace_seconds=900)

    assert await _post_status(tenant, running) == "running"  # 没被误回收


async def test_requeue_does_not_clobber_freshly_claimed_running(monkeypatch):
    """竞态防护（review #1）：find 选中某 stale 批后，worker 恰好认领它（running, fresh
    updated_at）。requeue 必须原子条件重置——不得把刚被认领的 running 打回 queued
    （否则会触发同批双跑、双上架）。"""
    from app.domains.sourcing import cron

    tenant = str(uuid.uuid4())
    bid = str(uuid.uuid4())
    await _seed(tenant, bid, backdate=0)
    async with tenant_session(tenant) as db:
        await SourcingRepository(db).set_post_status(bid, "running")  # 刚被认领，updated_at 新

    # 模拟 TOCTOU 窗口：find 在它还是 queued 时返回了它，但此刻已 running。
    async def fake_find(grace, running_grace):
        return [(bid, tenant)]

    monkeypatch.setattr(cron, "find_stale_pending", fake_find)
    async with queue_app.open_async():
        await cron.requeue_stale_pending(grace_seconds=120, running_grace_seconds=900)

    assert await _post_status(tenant, bid) == "running"  # 未被打回 queued


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
