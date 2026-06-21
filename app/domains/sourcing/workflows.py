"""商品采集的 durable workflow（Temporal）。

替代旧 zhuiwen_web.py 内存队列 + 轮询：Temporal 持久化状态，进程崩了也能从
当前步骤恢复，activity 自带重试。这与 chat/agent.py 的 LangGraph 是两层不同东西
（一个编排长流程、一个跑单轮工具推理），别混用。

租户上下文：workflow 跨进程，tenant_id 必须作为显式参数贯穿 workflow 与所有
activity（绝不能靠 ContextVar）。见 activities.py 里的 current_tenant_id.set。

流程：enqueue（落 pending 行）→ 等采集插件回结果信号（browser_done）→
score → translate → publish → 标 completed。插件超时则标 failed。
"""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

# activity 引用要穿过 workflow sandbox（它们有 IO/import 副作用，不参与确定性重放）。
with workflow.unsafe.imports_passed_through():
    from app.domains.sourcing.activities import (
        enqueue_browser_task,
        mark_job,
        publish_products,
        score_products,
        translate_products,
    )
    from app.domains.sourcing.models import COLLECTED, COMPLETED, FAILED

# 等浏览器插件回结果的最长时限：用户端抓取可能拖很久，给 1 小时。
_BROWSER_TIMEOUT = timedelta(hours=1)
_ACT_RETRY = RetryPolicy(maximum_attempts=5)
_ACT_TIMEOUT = timedelta(minutes=5)


@workflow.defn
class CollectWorkflow:
    def __init__(self) -> None:
        self._browser_result: dict | None = None
        self._done = False

    @workflow.signal
    def browser_done(self, result: dict) -> None:
        """采集插件经 /sourcing/jobs/{id}/done 回结果时，桥端点向本 workflow 发此信号。"""
        self._browser_result = result or {}
        self._done = True

    @workflow.query
    def status(self) -> str:
        return "collected" if self._done else "waiting_browser"

    @workflow.run
    async def run(self, tenant_id: str, job_id: str, params: dict) -> dict:
        # 1) 落 pending 行，供采集插件 poll 认领。
        await workflow.execute_activity(
            enqueue_browser_task, args=[tenant_id, job_id, params],
            start_to_close_timeout=timedelta(seconds=30), retry_policy=_ACT_RETRY,
        )

        # 2) 等插件抓完回结果（信号）。超时则标 failed 收尾。
        try:
            await workflow.wait_condition(lambda: self._done, timeout=_BROWSER_TIMEOUT)
        except asyncio.TimeoutError:
            await workflow.execute_activity(
                mark_job, args=[tenant_id, job_id, FAILED, None, "采集插件超时未回结果"],
                start_to_close_timeout=_ACT_TIMEOUT, retry_policy=_ACT_RETRY,
            )
            return {"status": FAILED, "reason": "browser_timeout"}

        raw = self._browser_result or {}
        await workflow.execute_activity(
            mark_job, args=[tenant_id, job_id, COLLECTED, None, None],
            start_to_close_timeout=_ACT_TIMEOUT, retry_policy=_ACT_RETRY,
        )

        # 3) 后处理：评分 → 翻译 → 上架（当前为集成桩）。
        scored = await workflow.execute_activity(
            score_products, args=[tenant_id, job_id, raw],
            start_to_close_timeout=_ACT_TIMEOUT, retry_policy=_ACT_RETRY,
        )
        translated = await workflow.execute_activity(
            translate_products, args=[tenant_id, job_id, scored],
            start_to_close_timeout=_ACT_TIMEOUT, retry_policy=_ACT_RETRY,
        )
        published = await workflow.execute_activity(
            publish_products, args=[tenant_id, job_id, translated],
            start_to_close_timeout=_ACT_TIMEOUT, retry_policy=_ACT_RETRY,
        )

        # 4) 收尾。
        result = {"published": published, "raw_count": len((raw.get("items") or []))}
        await workflow.execute_activity(
            mark_job, args=[tenant_id, job_id, COMPLETED, result, None],
            start_to_close_timeout=_ACT_TIMEOUT, retry_policy=_ACT_RETRY,
        )
        return {"status": COMPLETED, "result": result}
