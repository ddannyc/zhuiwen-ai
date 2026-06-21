"""sourcing 域 Temporal activities（有副作用的步骤，跑在 worker 进程）。

租户上下文纪律：workflow 跨进程，ContextVar 不会传过来。每个 activity 必须先
current_tenant_id.set(tenant_id)（用 workflow 显式传入的参数）再开 get_session()，
RLS 才会对本 activity 的 DB 操作生效。这是除 HTTP 中间件外，唯一允许 .set 的地方。

实际的商品抓取不在这里：那是浏览器采集插件在用户端做的（经 /sourcing/jobs/poll
认领、/done 回结果）。本文件的 score/translate/publish 是回结果后的后处理，
当前为集成桩，下游域真身接入后替换。
"""
from temporalio import activity

from app.core.database import current_tenant_id, get_session
from app.domains.sourcing.models import PENDING
from app.domains.sourcing.repository import SourcingRepository


@activity.defn
async def enqueue_browser_task(tenant_id: str, job_id: str, params: dict) -> str:
    """写入/落地 pending 任务行，供采集插件 poll 认领。幂等：行已存在则跳过。"""
    current_tenant_id.set(tenant_id)
    async with get_session() as s:
        repo = SourcingRepository(s)
        if await repo.get_job(job_id) is None:
            await repo.create_job(
                job_id=job_id,
                keywords=params.get("keywords") or [],
                per_kw=int(params.get("per_kw", 10)),
                market=params.get("market"),
            )
    return PENDING


@activity.defn
async def score_products(tenant_id: str, job_id: str, raw: dict) -> dict:
    """蓝海评分。集成桩：真身调下游选品/评分域 service。"""
    items = (raw or {}).get("items") or []
    return {"scored": items, "count": len(items)}


@activity.defn
async def translate_products(tenant_id: str, job_id: str, scored: dict) -> dict:
    """标题/属性翻译。集成桩：真身调翻译域 service。"""
    return {"translated": scored.get("scored") or [], "count": scored.get("count", 0)}


@activity.defn
async def publish_products(tenant_id: str, job_id: str, translated: dict) -> dict:
    """落入采集箱/上架。集成桩：真身调 box/listing 域 service。"""
    return {"published": translated.get("count", 0)}


@activity.defn
async def mark_job(tenant_id: str, job_id: str, status: str,
                   result: dict | None = None, error: str | None = None) -> None:
    """更新任务状态（RLS 经显式 tenant_id 生效）。"""
    current_tenant_id.set(tenant_id)
    async with get_session() as s:
        await SourcingRepository(s).mark(job_id, status, result=result, error=error)


# 供 worker 注册用的清单。
ALL_ACTIVITIES = [
    enqueue_browser_task, score_products, translate_products, publish_products, mark_job,
]
