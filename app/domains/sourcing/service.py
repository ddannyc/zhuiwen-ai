"""SourcingService —— sourcing 域唯一公开接口。

跨域只准调它（如 chat 的 collect_products 工具），外部不碰本域 repository/表。

两条采集路径：
  ① chat 关键词下发：start_collect 落 pending 行 → 采集插件 poll/done（旧插件模型）；
  ② 扩展 URL 回传：ingest 落批 + defer post_process（妙手 fetch→评分→翻译→上架，新模型）。
均返回结果保证 chat 路径不阻塞、前端有据可渲染。
"""
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.sourcing.models import COLLECTED, POST_PENDING, POST_QUEUED, CollectJob
from app.domains.sourcing.repository import SourcingRepository

log = logging.getLogger(__name__)


class SourcingService:
    def __init__(self, session: AsyncSession):
        self.repo = SourcingRepository(session)

    async def ingest(self, *, tenant_id: str | None, market: str, urls: list[str],
                     options: dict) -> dict:
        """扩展回传 URL 批 → 存库（post_status=pending）→ defer 后处理（置 queued）。

        ADR-001：procrastinate 无法与 asyncpg 业务写同事务原子 defer，故先提交 pending，
        提交后再 defer；defer 失败/崩溃留 pending，由 cron 兜底重投（零丢失）。
        """
        batch_id = str(uuid.uuid4())
        await self.repo.create_batch(
            batch_id=batch_id, urls=urls, options=options, market=market,
        )
        await self.repo.session.commit()  # 先持久化 pending（defer 前提交）
        post_status = await self._defer_post_process(batch_id, tenant_id)
        return {"batch_id": batch_id, "accepted": len(urls), "post_status": post_status}

    async def _defer_post_process(self, batch_id: str, tenant_id: str | None) -> str:
        """提交后 defer post_process 并置 queued（ADR-001：先提交再 defer，失败留 pending 待 cron）。
        调用方须已提交批行。返回最终 post_status。

        置 queued 用独立 tenant_session（重设租户 GUC），不复用请求会话——请求会话在
        commit 后 is_local 租户 GUC 已清，再查会撞 RLS。"""
        try:
            # 延迟导入避免循环（tasks 依赖 queue_app）。每次开/关连接池避免跨事件循环复用。
            from app.domains.sourcing.repository import SourcingRepository
            from app.domains.sourcing.tasks import post_process
            from app.shared.queue import queue_app, tenant_session

            async with queue_app.open_async():
                await post_process.defer_async(batch_id=batch_id, tenant_id=str(tenant_id))
            async with tenant_session(str(tenant_id)) as db2:
                await SourcingRepository(db2).set_post_status(batch_id, POST_QUEUED)
            return POST_QUEUED
        except Exception as e:  # noqa: BLE001 —— defer 失败不丢数据，cron 兜底
            log.warning("defer post_process 失败，批 %s 留 pending 待 cron 兜底: %s", batch_id, e)
            return POST_PENDING

    async def start_collect(self, *, tenant_id: str | None, keywords: list[str],
                            per_kw: int = 10, market: str | None = None) -> dict:
        """chat 下发关键词采集任务：直接落 pending 行（RLS 由中间件已设的租户上下文兜底）。
        采集插件经 /jobs/poll 认领、/jobs/{id}/done 回结果。无可用 DB 时如实标未持久化。
        （旧版的 Temporal 编排已移除；采集后自动评分/翻译/上架迁到 /ingest→post_process。）"""
        job_id = str(uuid.uuid4())
        base = {"job_id": job_id, "keywords": keywords, "per_kw": per_kw, "market": market}
        try:
            await self.repo.create_job(job_id=job_id, keywords=keywords, per_kw=per_kw, market=market)
            return {**base, "mode": "degraded"}
        except Exception as e:  # noqa: BLE001 —— 无 DB 时不让 chat 失败
            log.warning("采集任务写库失败（无可用 DB？）: %s", e)
        return {**base, "mode": "unavailable"}

    async def claim_next_job(self) -> dict | None:
        """采集插件 poll：认领下一个 pending 任务。RLS 限本租户。"""
        job = await self.repo.claim_next()
        return _serialize(job) if job else None

    async def complete_job(self, job_id: str, result: dict, tenant_id: str | None = None) -> dict:
        """采集插件 /done 回结果：标 collected 落 result，并桥接 defer post_process
        （评分/翻译/上架自动续跑）。插件回传 {items:[...]} 或 {urls:[...]} 都行。

        安全：先按 RLS 校验归属（get_job 走 RLS，他租户不可见 / 非法 id → not_found），
        防跨租户用他人 job_id 注入伪造结果（IDOR）。"""
        if await self.repo.get_job(job_id) is None:
            return {"ok": False, "mode": "not_found"}
        job = await self.repo.mark(job_id, COLLECTED, result=result)
        if job is None:
            return {"ok": False, "mode": "not_found"}
        await self.repo.session.commit()  # 先持久化结果（defer 前提交）
        post_status = await self._defer_post_process(job_id, tenant_id)
        return {"ok": True, "mode": "degraded", "post_status": post_status}

    async def get_job(self, job_id: str) -> dict | None:
        job = await self.repo.get_job(job_id)
        return _serialize(job) if job else None


def _serialize(job: CollectJob) -> dict:
    return {
        "id": str(job.id),
        "status": job.status,
        "keywords": job.keywords or [],
        "per_kw": job.per_kw,
        "market": job.market,
        "result": job.result,
        "error": job.error,
        # 后处理状态（客户端化）：前端轮询 ingest 批的处理进度。
        "post_status": job.post_status,
        "attempts": job.attempts,
        "last_error": job.last_error,
        "source": job.source,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }
