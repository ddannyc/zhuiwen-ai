"""sourcing cron 兜底：扫掉队的后处理批，重投 procrastinate（ADR-001 outbox）。

为何需要：ingest 先提交 pending 再 defer；若 defer 失败/进程崩，批留 pending（或被
worker 领走置 queued 后 worker 又丢了），不会自己恢复。cron 周期扫
post_status ∈ {pending, queued} 且 updated_at 超 grace 未推进的批 → 重置 queued + 重投。
post_process 成功置 done（移出扫描），失败置 failed（移出）；只有真掉队的会被反复救。

跨租户读：sweep 要看所有租户的掉队批，RLS（app 角色）只见本租户，故用 admin 连接
（database_admin_url，超管 bypass RLS）只读 (id, tenant_id)；重投按 tenant_session 写。
"""
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.domains.sourcing.models import POST_QUEUED
from app.domains.sourcing.repository import SourcingRepository
from app.shared.queue import queue_app, tenant_session

log = logging.getLogger(__name__)
_settings = get_settings()


async def find_stale_pending(grace_seconds: int, running_grace_seconds: int) -> list[tuple[str, str]]:
    """admin 连接跨租户读掉队批 (id, tenant_id)：
      - pending/queued 超 grace（投递丢失/迟迟没被领）；
      - running 超 running_grace（worker 崩在处理中——grace 须 > 妙手 fetch 耗时，不误回收在跑的）。
    每次开/弃引擎避免跨事件循环复用。"""
    engine = create_async_engine(_settings.database_admin_url)
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT id, tenant_id FROM collect_jobs WHERE "
                    "(post_status IN ('pending','queued') AND updated_at < now() - make_interval(secs => :g)) "
                    "OR (post_status = 'running' AND updated_at < now() - make_interval(secs => :rg))"
                ),
                {"g": grace_seconds, "rg": running_grace_seconds},
            )
            return [(str(r[0]), str(r[1])) for r in res]
    finally:
        await engine.dispose()


async def requeue_stale_pending(
    grace_seconds: int | None = None, running_grace_seconds: int | None = None
) -> int:
    """找掉队批（含崩在 running 的）→ 逐个置 queued（刷新 updated_at）+ 重投 post_process。
    返回重投数。调用方需已开 queue_app（worker 内已开；测试外层包 open_async）。"""
    grace = grace_seconds if grace_seconds is not None else _settings.sourcing_requeue_grace_seconds
    rg = (
        running_grace_seconds
        if running_grace_seconds is not None
        else _settings.sourcing_running_grace_seconds
    )
    stale = await find_stale_pending(grace, rg)
    if not stale:
        return 0

    from app.domains.sourcing.tasks import post_process

    for batch_id, tenant_id in stale:
        async with tenant_session(tenant_id) as db:
            await SourcingRepository(db).set_post_status(batch_id, POST_QUEUED)
        await post_process.defer_async(batch_id=batch_id, tenant_id=tenant_id)
    log.info("cron 重投掉队批 %d 个", len(stale))
    return len(stale)
