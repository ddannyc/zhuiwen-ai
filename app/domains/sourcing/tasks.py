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
from app.domains.sourcing.publish import publish_to_tiktok
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


async def _translate_title(title: str, lang: str) -> str:
    """翻译标题。真实接 zhuiwen_studio.translate_title（外部模块，本仓库未移植）——
    未接入时 passthrough（不改写），单测注入替身。"""
    return title


async def _pick_good_images(images: list[str]) -> list[str]:
    """图片质检选优。真实接 zhuiwen_studio.pick_good_images（外部模块未移植）——
    未接入时 passthrough，单测注入替身。"""
    return images


# AI 选 TikTok 类目（title, cate_tree)->cid。默认 None（不选，依赖外部 LLM+类目树）。
_pick_category = None


async def _apply_post_edits(miaoshou, cands: list[dict], scores: list[dict], options: dict) -> dict:
    """评分后整理 box：删不达标条目；按 options 翻译标题 / 质检图片，经 miaoshou.edit 回写。
    妙手 edit/delete 仅在确有动作时调用（保持对 mock/真实客户端的最小依赖）。"""
    by_id = {c.get("id"): c for c in cands}
    failing = [str(s["id"]) for s in scores if not s["pass"] and s["id"] is not None]
    passing = [s["id"] for s in scores if s["pass"] and s["id"] is not None]

    if failing:
        miaoshou.delete(failing)

    edited = 0
    for sid in passing:
        c = by_id.get(sid) or {}
        changes: dict = {}
        if options.get("translate"):
            changes["title"] = await _translate_title(c.get("title", ""), options.get("lang", ""))
        if options.get("optimize"):
            imgs = c.get("images") or []
            if imgs:
                changes["imgUrls"] = await _pick_good_images(imgs)
        if changes:
            miaoshou.edit(str(sid), changes)
            edited += 1

    return {"deleted": len(failing), "edited": edited}


async def _process(repo: SourcingRepository, batch_id: str) -> dict:
    """纯管线：读批 → 妙手 fetch → 评分 → 整理 box（删失败/翻译/质检）→ 合并 result。"""
    batch = await repo.get_job(batch_id)
    if batch is None:
        raise ValueError(f"batch 不存在: {batch_id}")
    payload = batch.result or {}
    urls = payload.get("urls") or []
    options = payload.get("options") or {}

    miaoshou = _make_miaoshou()
    cands = miaoshou.url_fetch(urls)
    scored = await score_candidates(
        cands,
        threshold=int(options.get("threshold", 70)),
        top_n=int(options.get("top_n", 0)),
        llm_json=_llm_json,
    )
    edits = await _apply_post_edits(miaoshou, cands, scored["scores"], options)

    result = {
        **payload,
        "cands": cands,
        "scores": scored["scores"],
        "count": scored["count"],
        "passed": scored["passed"],
        "edits": edits,
    }

    # 可选上架：达标 box-id → tk_list_items 编排（认领→认领店铺→预填→可选发布）。
    if options.get("list_tiktok"):
        passing_ids = [s["id"] for s in scored["scores"] if s["pass"] and s["id"] is not None]
        result["publish"] = await publish_to_tiktok(
            miaoshou, passing_ids,
            site=options.get("site", "MY"), auto=bool(options.get("tk_auto")),
            pick_category=_pick_category,
        )
    return result


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
