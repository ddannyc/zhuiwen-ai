"""Phase2 T2.3 / C2：post_process task 端到端（mock 妙手+评分 → defer → worker → done）。

验收：defer → worker 跑 → batch post_status=done + scores 落库 + RLS 正确；
妙手失败 → post_status=failed + attempts++ + last_error。
真 PG 必需；不可达则 skip。
"""
import uuid

import psycopg
import pytest

from app.core.config import get_settings
from app.domains.sourcing.miaoshou import MiaoshouError
from app.domains.sourcing.repository import SourcingRepository
from app.shared.queue import queue_app, tenant_session

_OFFER = "https://detail.1688.com/offer/1.html"


def _db_reachable() -> bool:
    try:
        url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
        with psycopg.connect(url, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_reachable(), reason="本地 Postgres(xborder) 不可达")


@pytest.fixture(autouse=True)
async def _fresh_engine():
    from app.core.database import engine

    await engine.dispose()
    yield


async def _seed_batch(tenant: str, batch_id: str, options: dict) -> None:
    async with tenant_session(tenant) as db:
        await SourcingRepository(db).create_batch(
            batch_id=batch_id, urls=[_OFFER], options=options, market="1688"
        )


async def _run_worker(tenant: str, batch_id: str) -> None:
    async with queue_app.open_async():
        from app.domains.sourcing.tasks import post_process

        await post_process.defer_async(batch_id=batch_id, tenant_id=tenant)
        await queue_app.run_worker_async(wait=False, install_signal_handlers=False)


async def test_post_process_done_with_scores(monkeypatch):
    import app.domains.sourcing.tasks as t

    class FakeMS:
        def url_fetch(self, urls, limit=None):
            return [{"id": "1", "title": "耳机 工厂直销", "price_cny": 50, "source_url": urls[0]}]

    async def fake_llm(system, user):
        return [{"i": 0, "score": 88, "title_en": "Earbuds"}]

    monkeypatch.setattr(t, "_make_miaoshou", lambda: FakeMS())
    monkeypatch.setattr(t, "_llm_json", fake_llm)

    tenant = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    await _seed_batch(tenant, batch_id, {"threshold": 70})
    await _run_worker(tenant, batch_id)

    async with tenant_session(tenant) as db:
        job = await SourcingRepository(db).get_job(batch_id)
        assert job.post_status == "done"
        assert job.result["scores"][0]["pass"] is True
        assert job.result["scores"][0]["title"] == "耳机"  # 违禁词清掉


async def test_post_process_miaoshou_failure_marks_failed(monkeypatch):
    import app.domains.sourcing.tasks as t

    class BadMS:
        def url_fetch(self, urls, limit=None):
            raise MiaoshouError("fetch boom")

    monkeypatch.setattr(t, "_make_miaoshou", lambda: BadMS())

    tenant = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    await _seed_batch(tenant, batch_id, {"threshold": 70})
    await _run_worker(tenant, batch_id)

    async with tenant_session(tenant) as db:
        job = await SourcingRepository(db).get_job(batch_id)
        assert job.post_status == "failed"
        assert job.attempts >= 1
        assert "boom" in (job.last_error or "")
