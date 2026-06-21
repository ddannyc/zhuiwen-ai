"""SourcingService 编排测试。

不依赖真 DB / 真 Temporal：用内存假 repo + 替换 _connect 模拟 Temporal 可达/不可达，
验证三级降级（temporal / degraded / unavailable）与采集插件 poll/done 路径。
CollectWorkflow 的活动编排由本地 Temporal 的端到端用例覆盖（计划 §验证 步6-7）。
"""
import types

import pytest

from app.domains.sourcing.models import COLLECTED, COLLECTING, PENDING
from app.domains.sourcing.service import SourcingService


class FakeRepo:
    def __init__(self):
        self.jobs: dict = {}

    async def create_job(self, *, job_id, keywords, per_kw, market):
        job = types.SimpleNamespace(
            id=job_id, status=PENDING, keywords=keywords, per_kw=per_kw, market=market,
            result=None, error=None, created_at=None, updated_at=None)
        self.jobs[job_id] = job
        return job

    async def get_job(self, job_id):
        return self.jobs.get(job_id)

    async def claim_next(self):
        for j in self.jobs.values():
            if j.status == PENDING:
                j.status = COLLECTING
                return j
        return None

    async def mark(self, job_id, status, *, result=None, error=None):
        j = self.jobs.get(job_id)
        if j is None:
            return None
        j.status = status
        if result is not None:
            j.result = result
        if error is not None:
            j.error = error
        return j


class RaisingRepo(FakeRepo):
    async def create_job(self, **kw):
        raise RuntimeError("no db")


def _svc(repo=None, connect=None):
    svc = SourcingService.__new__(SourcingService)
    svc.repo = repo if repo is not None else FakeRepo()
    if connect is not None:
        svc._connect = connect
    return svc


async def _connect_fail():
    raise ConnectionError("temporal down")


class FakeHandle:
    def __init__(self):
        self.signaled = None

    async def signal(self, name, arg):
        self.signaled = (name, arg)


class FakeClient:
    def __init__(self):
        self.started = None
        self.handle = FakeHandle()

    async def start_workflow(self, *a, **k):
        self.started = (a, k)

    def get_workflow_handle(self, wid):
        self.last_wid = wid
        return self.handle


# ---- start_collect 三级降级 ----

async def test_start_collect_temporal_mode():
    client = FakeClient()

    async def connect_ok():
        return client

    repo = FakeRepo()
    svc = _svc(repo=repo, connect=connect_ok)
    res = await svc.start_collect(tenant_id="t1", keywords=["杯子"], per_kw=20, market="my")

    assert res["mode"] == "temporal"
    assert res["job_id"]
    assert client.started is not None  # workflow 已启动
    assert repo.jobs == {}  # temporal 模式不直接写库（由 activity 落行）


async def test_start_collect_degraded_writes_row():
    repo = FakeRepo()
    svc = _svc(repo=repo, connect=_connect_fail)
    res = await svc.start_collect(tenant_id="t1", keywords=["杯子"], per_kw=10, market=None)

    assert res["mode"] == "degraded"
    job = repo.jobs[res["job_id"]]
    assert job.status == PENDING  # 降级直接落 pending 行，插件可 poll
    assert job.keywords == ["杯子"]


async def test_start_collect_unavailable_still_returns_job_id():
    svc = _svc(repo=RaisingRepo(), connect=_connect_fail)
    res = await svc.start_collect(tenant_id=None, keywords=[], per_kw=10, market=None)

    assert res["mode"] == "unavailable"
    assert res["job_id"]  # 连库都写不进，仍返回 job_id 不阻塞 chat


# ---- poll / done ----

async def test_claim_next_serializes_and_marks_collecting():
    repo = FakeRepo()
    await repo.create_job(job_id="j1", keywords=["a"], per_kw=5, market=None)
    svc = _svc(repo=repo, connect=_connect_fail)

    job = await svc.claim_next_job()
    assert job["id"] == "j1"
    assert job["status"] == COLLECTING
    assert repo.jobs["j1"].status == COLLECTING

    # 队列空 → None
    assert await svc.claim_next_job() is None


async def test_complete_job_temporal_signals_workflow():
    client = FakeClient()

    async def connect_ok():
        return client

    repo = FakeRepo()
    await repo.create_job(job_id="j1", keywords=[], per_kw=10, market=None)  # 本租户拥有
    svc = _svc(repo=repo, connect=connect_ok)
    res = await svc.complete_job("j1", {"items": [1, 2]})

    assert res == {"ok": True, "mode": "temporal"}
    assert client.handle.signaled == ("browser_done", {"items": [1, 2]})


async def test_complete_job_foreign_job_not_signaled(monkeypatch):
    # 评审 blocker（跨租户 IDOR）：Temporal 按 job_id 全局直签、无 RLS。
    # 必须先按 RLS 校验归属——查不到（跨租户不可见）的 job 绝不下发信号。
    client = FakeClient()

    async def connect_ok():
        return client

    repo = FakeRepo()  # 空：模拟 RLS 下 foreign job 不可见（get_job → None）
    svc = _svc(repo=repo, connect=connect_ok)
    res = await svc.complete_job("foreign-jobid", {"items": [999]})

    assert res["ok"] is False
    assert res["mode"] == "not_found"
    assert client.handle.signaled is None  # 关键：未向他人 workflow 注入信号


async def test_complete_job_degraded_marks_collected():
    repo = FakeRepo()
    await repo.create_job(job_id="j1", keywords=[], per_kw=10, market=None)
    svc = _svc(repo=repo, connect=_connect_fail)

    res = await svc.complete_job("j1", {"items": [1]})
    assert res == {"ok": True, "mode": "degraded"}
    assert repo.jobs["j1"].status == COLLECTED
    assert repo.jobs["j1"].result == {"items": [1]}


async def test_complete_job_unknown_id_not_ok():
    svc = _svc(connect=_connect_fail)  # 无此任务 → 归属校验即 not_found，不触 Temporal
    res = await svc.complete_job("missing", {})
    assert res == {"ok": False, "mode": "not_found"}
