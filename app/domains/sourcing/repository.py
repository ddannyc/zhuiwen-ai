"""sourcing 数据访问层。

无任何 `WHERE tenant_id = ...`：RLS 已在 DB 层保证只见当前租户的行。
poll 的认领用 FOR UPDATE SKIP LOCKED，防多个采集插件实例抢到同一任务。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.sourcing.models import COLLECTING, PENDING, CollectJob


def _now() -> datetime:
    # 用 Python 时间戳赋值 updated_at，而非 func.now() SQL 表达式：后者在 flush 后
    # 会让该列 expire，随后序列化读取触发异步惰性刷新 → MissingGreenlet。
    return datetime.now(timezone.utc)


class SourcingRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_job(self, *, job_id: str, keywords: list[str], per_kw: int,
                         market: str | None) -> CollectJob:
        # 不传 tenant_id —— 由表 DEFAULT current_setting('app.current_tenant') 填充。
        # id 显式传入：与 Temporal workflow_id 对齐，便于 /done 按 id 找 workflow 发信号。
        job = CollectJob(
            id=uuid.UUID(str(job_id)), keywords=keywords, per_kw=per_kw, market=market,
            status=PENDING,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def get_job(self, job_id: str) -> CollectJob | None:
        return await self.session.get(CollectJob, uuid.UUID(str(job_id)))

    async def claim_next(self) -> CollectJob | None:
        """采集插件 poll：认领最早的 pending 任务并置 collecting。
        FOR UPDATE SKIP LOCKED 让并发的多个插件各取不同任务，不重复抓。"""
        stmt = (
            select(CollectJob)
            .where(CollectJob.status == PENDING)
            .order_by(CollectJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = (await self.session.execute(stmt)).scalars().first()
        if job is None:
            return None
        job.status = COLLECTING
        job.updated_at = _now()
        await self.session.flush()
        return job

    async def mark(self, job_id: str, status: str, *, result: dict | None = None,
                   error: str | None = None) -> CollectJob | None:
        job = await self.get_job(job_id)
        if job is None:
            return None
        job.status = status
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        job.updated_at = _now()
        await self.session.flush()
        return job

    async def list_jobs(self, limit: int = 50) -> list[CollectJob]:
        stmt = select(CollectJob).order_by(CollectJob.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())
