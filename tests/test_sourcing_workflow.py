"""CollectWorkflow 编排测试（评审 #3：§3 核心此前零覆盖）。

用 temporalio 的 time-skipping WorkflowEnvironment 跑真 workflow，但 activity 全替成
桩（不碰 DB）：只验编排逻辑——enqueue→等 browser_done 信号→score/translate/publish→
completed；以及无信号时定时器超时→failed（time-skipping 让 1h 等待瞬间完成）。

测试服务器二进制首次需联网下载；不可用则整文件 skip（CI 离线不挂）。
"""
import uuid

import pytest
from temporalio import activity

from app.domains.sourcing.models import COLLECTED, COMPLETED, FAILED
from app.domains.sourcing.workflows import CollectWorkflow

# 跨 worker 记录 activity 调用（time-skipping env 同进程跑 activity）。
CALLS: list = []


@activity.defn(name="enqueue_browser_task")
async def stub_enqueue(tenant_id: str, job_id: str, params: dict) -> str:
    CALLS.append(("enqueue", tenant_id, job_id, params))
    return "pending"


@activity.defn(name="score_products")
async def stub_score(tenant_id: str, job_id: str, raw: dict) -> dict:
    CALLS.append(("score", raw))
    return {"scored": (raw or {}).get("items", []), "count": len((raw or {}).get("items", []))}


@activity.defn(name="translate_products")
async def stub_translate(tenant_id: str, job_id: str, scored: dict) -> dict:
    CALLS.append(("translate", scored))
    return {"translated": scored.get("scored", []), "count": scored.get("count", 0)}


@activity.defn(name="publish_products")
async def stub_publish(tenant_id: str, job_id: str, translated: dict) -> dict:
    CALLS.append(("publish", translated))
    return {"published": translated.get("count", 0)}


@activity.defn(name="mark_job")
async def stub_mark(tenant_id: str, job_id: str, status: str,
                    result: dict | None = None, error: str | None = None) -> None:
    CALLS.append(("mark", status, result, error))


_STUBS = [stub_enqueue, stub_score, stub_translate, stub_publish, stub_mark]


async def _make_env():
    """启 time-skipping 环境；下载/启动失败 → skip。"""
    from temporalio.testing import WorkflowEnvironment
    try:
        return await WorkflowEnvironment.start_time_skipping()
    except Exception as e:  # 二进制下载失败 / 离线
        pytest.skip(f"Temporal 测试环境不可用：{e}")


async def _run(env, *, signal_after_start):
    from temporalio.worker import Worker
    CALLS.clear()
    job_id = str(uuid.uuid4())
    async with Worker(env.client, task_queue="tq-test",
                      workflows=[CollectWorkflow], activities=_STUBS):
        handle = await env.client.start_workflow(
            CollectWorkflow.run, args=["tenant-1", job_id, {"keywords": ["杯子"], "per_kw": 5}],
            id=f"wf-{job_id}", task_queue="tq-test",
        )
        if signal_after_start is not None:
            await handle.signal("browser_done", signal_after_start)
        return await handle.result()


async def test_collect_workflow_happy_path():
    env = await _make_env()
    async with env:
        result = await _run(env, signal_after_start={"items": [{"t": "A"}, {"t": "B"}]})

    assert result["status"] == COMPLETED
    assert result["result"]["raw_count"] == 2
    kinds = [c[0] for c in CALLS]
    # 编排顺序：enqueue → (collected) → score → translate → publish → (completed)
    assert kinds == ["enqueue", "mark", "score", "translate", "publish", "mark"]
    assert CALLS[1][1] == COLLECTED      # 收到信号后先标 collected
    assert CALLS[-1][1] == COMPLETED     # 收尾标 completed
    assert CALLS[-1][2]["raw_count"] == 2


async def test_collect_workflow_browser_timeout_marks_failed():
    env = await _make_env()
    async with env:
        # 不发 browser_done 信号：wait_condition 超时（time-skipping 瞬间推进 1h）。
        result = await _run(env, signal_after_start=None)

    assert result["status"] == FAILED
    assert result["reason"] == "browser_timeout"
    kinds = [c[0] for c in CALLS]
    assert kinds == ["enqueue", "mark"]          # 只 enqueue + 标 failed，不进后处理
    assert CALLS[-1][1] == FAILED
    assert "超时" in (CALLS[-1][3] or "")        # error 文案
