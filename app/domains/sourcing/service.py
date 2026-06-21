"""SourcingService —— sourcing 域唯一公开接口。

跨域只准调它（如 chat 的 collect_products 工具），外部不碰本域 repository/表。

降级纪律：chat 请求触发采集时若 Temporal 不可达，不应让对话失败。
start_collect 三级回退：
  temporal    —— 正常：启 CollectWorkflow，由其 activity 落 pending 行；
  degraded    —— Temporal 连不上：用请求 session 直接写 pending 行，插件照样能 poll；
  unavailable —— 连库也写不进（如无 DB 的单测）：仅返回 job_id，如实标记未持久化。
无论哪级都返回 job_id，保证 chat 路径不阻塞、前端有据可渲染。
"""
import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domains.sourcing.models import COLLECTED, CollectJob
from app.domains.sourcing.repository import SourcingRepository

log = logging.getLogger(__name__)
_settings = get_settings()


class SourcingService:
    def __init__(self, session: AsyncSession):
        self.repo = SourcingRepository(session)

    async def start_collect(self, *, tenant_id: str | None, keywords: list[str],
                            per_kw: int = 10, market: str | None = None) -> dict:
        job_id = str(uuid.uuid4())
        params = {"keywords": keywords, "per_kw": per_kw, "market": market}
        base = {"job_id": job_id, "keywords": keywords, "per_kw": per_kw, "market": market}

        # 1) 正常：启 Temporal workflow（workflow_id = job_id，便于 /done 按 id 发信号）。
        try:
            client = await self._connect()
            from app.domains.sourcing.workflows import CollectWorkflow
            await client.start_workflow(
                CollectWorkflow.run, args=[tenant_id, job_id, params],
                id=job_id, task_queue=_settings.sourcing_task_queue,
            )
            return {**base, "mode": "temporal"}
        except Exception as e:  # 连不上/启动失败 → 降级，不让 chat 失败
            log.warning("Temporal 不可达，采集任务降级直写库: %s", e)

        # 2) 降级：用请求 session 直接落 pending 行（RLS 由中间件已设的租户上下文兜底）。
        try:
            await self.repo.create_job(job_id=job_id, keywords=keywords, per_kw=per_kw, market=market)
            return {**base, "mode": "degraded"}
        except Exception as e:
            log.warning("采集任务降级写库失败（无可用 DB？）: %s", e)

        # 3) 兜底：仅返回 job_id，如实告知未持久化。
        return {**base, "mode": "unavailable"}

    async def claim_next_job(self) -> dict | None:
        """采集插件 poll：认领下一个 pending 任务。RLS 限本租户。"""
        job = await self.repo.claim_next()
        return _serialize(job) if job else None

    async def complete_job(self, job_id: str, result: dict) -> dict:
        """采集插件 /done 回结果：优先给 workflow 发信号续跑后处理；
        Temporal 不可达则降级直接把任务标 collected。"""
        try:
            client = await self._connect()
            handle = client.get_workflow_handle(job_id)
            await handle.signal("browser_done", result)
            return {"ok": True, "mode": "temporal"}
        except Exception as e:
            log.warning("Temporal 信号失败，降级直更任务状态: %s", e)

        job = await self.repo.mark(job_id, COLLECTED, result=result)
        return {"ok": job is not None, "mode": "degraded"}

    async def get_job(self, job_id: str) -> dict | None:
        job = await self.repo.get_job(job_id)
        return _serialize(job) if job else None

    async def _connect(self):
        # 探活带超时，避免 Temporal 宕机时 chat 请求被长时间阻塞。
        from temporalio.client import Client
        return await asyncio.wait_for(
            Client.connect(_settings.temporal_host),
            timeout=_settings.temporal_connect_timeout,
        )


def _serialize(job: CollectJob) -> dict:
    return {
        "id": str(job.id),
        "status": job.status,
        "keywords": job.keywords or [],
        "per_kw": job.per_kw,
        "market": job.market,
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }
