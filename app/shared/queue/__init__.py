"""procrastinate 后台队列（替代 Temporal）。

PG-backed、asyncio 原生。后处理长流程（妙手 fetch / 评分 / 翻译 / 上架）在此跑，
不占 API 请求。worker 入口见 app/workers/main.py。

租户纪律：worker 跨进程、无请求 ContextVar——task 必须显式传 tenant_id，
经 tenant_session 设 app.current_tenant 后走 RLS（同旧 Temporal activity）。
"""
from app.shared.queue.app import ensure_defer, lifespan_open, queue_app
from app.shared.queue.tenant import tenant_session

__all__ = ["queue_app", "tenant_session", "lifespan_open", "ensure_defer"]
