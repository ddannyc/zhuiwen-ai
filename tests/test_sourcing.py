"""SourcingService 编排测试。

不依赖真 DB：用内存假 repo 验 start_collect 落 pending 行 / 无 DB（unavailable）+
采集插件 poll/done 路径 + 跨租户 IDOR（done 前按 RLS 校验归属）。
（Temporal 已移除；采集后处理走 /ingest→post_process，见 test_post_process.py。）
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
            result=None, error=None, created_at=None, updated_at=None,
            post_status="pending", attempts=0, last_error=None, source="1688")
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


def _svc(repo=None):
    svc = SourcingService.__new__(SourcingService)
    svc.repo = repo if repo is not None else FakeRepo()
    return svc


# ---- start_collect ----

async def test_start_collect_writes_pending_row():
    repo = FakeRepo()
    svc = _svc(repo=repo)
    res = await svc.start_collect(tenant_id="t1", keywords=["杯子"], per_kw=10, market=None)

    assert res["mode"] == "ok"
    job = repo.jobs[res["job_id"]]
    assert job.status == PENDING  # 落 pending 行，插件可 poll
    assert job.keywords == ["杯子"]


async def test_start_collect_unavailable_still_returns_job_id():
    svc = _svc(repo=RaisingRepo())
    res = await svc.start_collect(tenant_id=None, keywords=[], per_kw=10, market=None)

    assert res["mode"] == "unavailable"
    assert res["job_id"]  # 连库都写不进，仍返回 job_id 不阻塞 chat


# ---- poll / done ----

async def test_claim_next_serializes_and_marks_collecting():
    repo = FakeRepo()
    await repo.create_job(job_id="j1", keywords=["a"], per_kw=5, market=None)
    svc = _svc(repo=repo)

    job = await svc.claim_next_job()
    assert job["id"] == "j1"
    assert job["status"] == COLLECTING
    assert repo.jobs["j1"].status == COLLECTING

    # 队列空 → None
    assert await svc.claim_next_job() is None


async def test_complete_job_foreign_job_404_not_found():
    # 跨租户 IDOR：foreign job 在 RLS 下不可见（get_job → None）→ not_found，
    # 绝不标 collected / 不触发后处理。
    svc = _svc(repo=FakeRepo())  # 空 repo 模拟 RLS 隔离
    res = await svc.complete_job("foreign-jobid", {"items": [999]})

    assert res["ok"] is False
    assert res["mode"] == "not_found"


async def test_complete_job_unknown_id_not_ok():
    svc = _svc()  # 无此任务 → 归属校验即 not_found
    res = await svc.complete_job("missing", {})
    assert res == {"ok": False, "mode": "not_found"}
