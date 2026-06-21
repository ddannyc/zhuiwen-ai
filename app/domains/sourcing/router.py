"""sourcing 域 HTTP 路由。

浏览器采集插件经这两个桥端点驱动长流程（替代旧 collect-job/poll|done）：
  POST /sourcing/jobs/poll        插件认领下一个 pending 任务
  POST /sourcing/jobs/{id}/done   插件回传抓取结果 → 给 workflow 发信号续跑
插件用 JWT 认证，租户由 token 决定，RLS 自动限定只见本租户任务。

另留 POST /sourcing/collect 手动下发（便于不经 chat 直接测）；正常下发走 chat
的 collect_products 工具。router 只校验参数 + 调 service，不写业务。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.domains.sourcing.schemas import (
    IngestRequest,
    JobDoneRequest,
    StartCollectRequest,
)
from app.domains.sourcing.service import SourcingService

router = APIRouter(prefix="/sourcing", tags=["sourcing"])


@router.post("/ingest")
async def ingest(body: IngestRequest, request: Request,
                 db: AsyncSession = Depends(get_db)):
    """扩展回传登录态采集的 1688 offer URL 批 → 存库 + 入队后处理。
    tenant_id 显式贯穿到后处理 task（跨进程，不靠 ContextVar）。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    return await SourcingService(db).ingest(
        tenant_id=tenant_id, market=body.market, urls=body.urls,
        options=body.options.model_dump(),
    )


@router.post("/collect")
async def start_collect(body: StartCollectRequest, request: Request,
                        db: AsyncSession = Depends(get_db)):
    # tenant_id 必须显式贯穿到 Temporal（跨进程，不能靠 ContextVar）。从中间件已解析的
    # 请求态取，传给 service。
    tenant_id = getattr(request.state, "tenant_id", None)
    res = await SourcingService(db).start_collect(
        tenant_id=tenant_id, keywords=body.keywords, per_kw=body.per_kw, market=body.market,
    )
    return res


@router.post("/jobs/poll")
async def poll_job(db: AsyncSession = Depends(get_db)):
    job = await SourcingService(db).claim_next_job()
    return {"job": job}


@router.post("/jobs/{job_id}/done")
async def job_done(job_id: str, body: JobDoneRequest, request: Request,
                   db: AsyncSession = Depends(get_db)):
    # tenant_id 显式传给 service → 桥接 defer post_process（跨进程不靠 ContextVar）。
    tenant_id = getattr(request.state, "tenant_id", None)
    res = await SourcingService(db).complete_job(job_id, body.result, tenant_id=tenant_id)
    if not res["ok"]:
        raise HTTPException(status_code=404, detail="job not found")
    return res


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, db: AsyncSession = Depends(get_db)):
    job = await SourcingService(db).get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job
