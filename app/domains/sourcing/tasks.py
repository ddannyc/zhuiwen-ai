"""sourcing 后处理 procrastinate task。

post_process：扩展回传的 URL 批 → 妙手 fetch → 评分（+违禁词清洗 + top_n）→ 存 result。
worker 跨进程，tenant_id 必须显式传参（经 tenant_session 设 RLS）。

依赖注入：_make_miaoshou / _llm_json 为模块级钩子，单测 monkeypatch 替身，
生产用真实妙手 CLI + gateway。

翻译/上架（T3.1/T3.2）后续接在评分之后；当前到「fetch+评分+存库」为 C2 MVP。
"""
import logging

from app.domains.sourcing.ingest import loose_json_array, score_candidates
from app.domains.sourcing.miaoshou import MiaoshouClient
from app.domains.sourcing.repository import SourcingRepository
from app.shared.queue import queue_app, tenant_session

log = logging.getLogger(__name__)


def _make_miaoshou() -> MiaoshouClient:
    return MiaoshouClient()


async def _default_llm_json(system: str, user: str) -> list:
    """真实评分：经 gateway 打 Qwen，抠出 JSON 数组。"""
    from app.shared.llm import gateway

    raw = await gateway.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}]
    )
    return loose_json_array(raw)


_llm_json = _default_llm_json


async def _process(repo: SourcingRepository, batch_id: str) -> dict:
    """纯管线：读批 → 妙手 fetch → 评分 → 合并 result（不改状态，由 task 包装收尾）。"""
    batch = await repo.get_job(batch_id)
    if batch is None:
        raise ValueError(f"batch 不存在: {batch_id}")
    payload = batch.result or {}
    urls = payload.get("urls") or []
    options = payload.get("options") or {}

    cands = _make_miaoshou().url_fetch(urls)
    scored = await score_candidates(
        cands,
        threshold=int(options.get("threshold", 70)),
        top_n=int(options.get("top_n", 0)),
        llm_json=_llm_json,
    )
    return {
        **payload,
        "cands": cands,
        "scores": scored["scores"],
        "count": scored["count"],
        "passed": scored["passed"],
    }


@queue_app.task(name="sourcing.post_process")
async def post_process(batch_id: str, tenant_id: str) -> None:
    # 不在 tenant_session 内中途 commit：set_config(is_local) 是事务级，commit 会清掉
    # 租户 GUC，后续查询撞 RLS current_setting 未设 → 事务中止。整条管线一个事务，
    # 退出时提交（done）。失败则主事务回滚（批回到 queued/pending，cron 可重投，不卡
    # running），再开一个新事务把 failed 落库。
    try:
        async with tenant_session(tenant_id) as db:
            result = await _process(SourcingRepository(db), batch_id)
            await SourcingRepository(db).mark_post_done(batch_id, result)
    except Exception as e:  # noqa: BLE001 —— 标 failed 留痕，重试策略见 T4
        log.warning("post_process 失败 batch=%s: %s", batch_id, e)
        async with tenant_session(tenant_id) as db:
            await SourcingRepository(db).mark_post_failed(batch_id, str(e))
        raise
