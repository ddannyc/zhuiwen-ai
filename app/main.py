"""API 进程入口：uvicorn app.main:app

这是处理 HTTP 请求的进程。长耗时任务不在这里跑，交给 worker。
所有域的 router 在这里聚合挂载。
"""
from fastapi import FastAPI

from app.domains.knowledge_base.router import router as kb_router
from app.shared.tenant.middleware import TenantMiddleware

# 其他域的 router（占位，按需取消注释）：
# from app.domains.listing.router import router as listing_router
# from app.domains.publishing.router import router as publishing_router
# from app.domains.customer_service.router import router as cs_router

app = FastAPI(title="XBorder AI")
app.add_middleware(TenantMiddleware)


@app.get("/health")
async def health():
    return {"status": "ok"}


app.include_router(kb_router)
# app.include_router(listing_router)
# app.include_router(publishing_router)
# app.include_router(cs_router)
