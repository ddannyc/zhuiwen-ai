"""sourcing 后处理 procrastinate task。

post_process：扩展回传的 URL 批 → 妙手 fetch → 评分 → 翻译 → 上架。
worker 跨进程，tenant_id 必须显式传参（经 tenant_session 设 RLS）。

T2.2 先占位（仅注册 task 供 /ingest defer）；真实管线在 T2.3 实现。
"""
from app.shared.queue import queue_app


@queue_app.task(name="sourcing.post_process")
async def post_process(batch_id: str, tenant_id: str) -> None:
    # T2.3 实现：tenant_session(tenant_id) → 妙手 url_fetch → 评分 → 翻译 → 上架 →
    #            post_status=done；失败 attempts++/last_error/failed。
    # 占位期 no-op：批已落库 post_status=pending/queued，T2.3 接管前不处理。
    return None
