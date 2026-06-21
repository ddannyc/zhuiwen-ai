"""API 进程入口：uvicorn app.main:app

这是处理 HTTP 请求的进程。长耗时任务不在这里跑，交给 worker。
所有域的 router 在这里聚合挂载。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.domains.auth.router import router as auth_router
from app.domains.chat.router import router as chat_router
from app.domains.knowledge_base.router import router as kb_router
from app.domains.sourcing.router import router as sourcing_router
from app.shared.queue import lifespan_open
from app.shared.tenant.middleware import TenantMiddleware


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时开一次 procrastinate queue_app，defer 复用连接池，不必每请求开/关（review #2）。
    async with lifespan_open():
        yield

# 其他域的 router（占位，按需取消注释）：
# from app.domains.listing.router import router as listing_router
# from app.domains.publishing.router import router as publishing_router
# from app.domains.customer_service.router import router as cs_router

app = FastAPI(title="XBorder AI", lifespan=lifespan)
# 中间件顺序：Starlette 最后 add 的在最外层。CORS 必须最外层，
# 否则 TenantMiddleware 会先拦截带 Authorization 的 OPTIONS 预检（无 token → 401）
# 导致浏览器报 CORS 失败。故先 add Tenant、后 add CORS。
app.add_middleware(TenantMiddleware)
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _settings.cors_origins.split(",") if o.strip()],
    allow_origin_regex=_settings.cors_origin_regex or None,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}


app.include_router(auth_router)
app.include_router(kb_router)
app.include_router(chat_router)
app.include_router(sourcing_router)
# app.include_router(listing_router)
# app.include_router(publishing_router)
# app.include_router(cs_router)
