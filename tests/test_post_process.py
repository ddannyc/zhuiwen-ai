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


async def test_post_process_translate_and_delete_failing(monkeypatch):
    """T3.1：开 translate → 删不达标 box 条目 + 翻译达标条目标题（miaoshou.edit 回写）。"""
    import app.domains.sourcing.tasks as t

    calls: dict = {"edit": [], "delete": []}

    class FakeMS:
        def url_fetch(self, urls, limit=None):
            return [
                {"id": "1", "title": "耳机", "images": ["a.jpg", "b.jpg"], "price_cny": 50, "source_url": urls[0]},
                {"id": "2", "title": "垃圾品", "images": [], "price_cny": 1, "source_url": urls[0]},
            ]

        def delete(self, ids):
            calls["delete"].append(list(ids))
            return {"deleted": len(ids)}

        def edit(self, item_id, changes):
            calls["edit"].append((item_id, changes))
            return {"ok": True}

    async def fake_llm(system, user):
        return [{"i": 0, "score": 90, "title_en": "Earbuds"}, {"i": 1, "score": 30}]

    async def fake_translate(title, lang):
        return f"EN:{title}"

    monkeypatch.setattr(t, "_make_miaoshou", lambda: FakeMS())
    monkeypatch.setattr(t, "_llm_json", fake_llm)
    monkeypatch.setattr(t, "_translate_title", fake_translate)

    tenant = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    await _seed_batch(tenant, batch_id, {"threshold": 70, "translate": True, "lang": "en"})
    await _run_worker(tenant, batch_id)

    # 不达标 id=2 删除；达标 id=1 标题翻译回写
    assert ["2"] in calls["delete"]
    assert any(i == "1" and ch.get("title") == "EN:耳机" for i, ch in calls["edit"])
    async with tenant_session(tenant) as db:
        job = await SourcingRepository(db).get_job(batch_id)
        assert job.post_status == "done"
        assert job.result["edits"]["deleted"] == 1
        assert job.result["edits"]["edited"] == 1


async def test_post_process_optimize_picks_images(monkeypatch):
    """T3.1：开 optimize → pick_good_images 选优 → edit 回写 imgUrls。"""
    import app.domains.sourcing.tasks as t

    edits: list = []

    class FakeMS:
        def url_fetch(self, urls, limit=None):
            return [{"id": "1", "title": "好品", "images": ["a.jpg", "b.jpg", "c.jpg"], "price_cny": 50, "source_url": urls[0]}]

        def delete(self, ids):
            return {"deleted": 0}

        def edit(self, item_id, changes):
            edits.append((item_id, changes))
            return {"ok": True}

    async def fake_llm(system, user):
        return [{"i": 0, "score": 90}]

    async def fake_pick(images):
        return images[:1]  # 只留第一张

    monkeypatch.setattr(t, "_make_miaoshou", lambda: FakeMS())
    monkeypatch.setattr(t, "_llm_json", fake_llm)
    monkeypatch.setattr(t, "_pick_good_images", fake_pick)

    tenant = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    await _seed_batch(tenant, batch_id, {"threshold": 70, "optimize": True})
    await _run_worker(tenant, batch_id)

    assert any(i == "1" and ch.get("imgUrls") == ["a.jpg"] for i, ch in edits)


async def test_post_process_list_tiktok_publishes(monkeypatch):
    """T3.2：开 list_tiktok+tk_auto → 达标品走上架编排 → result.publish.summary。"""
    import app.domains.sourcing.tasks as t
    from app.domains.sourcing.publish import CLAIMED_PATH

    class FakeMS:
        def url_fetch(self, urls, limit=None):
            return [{"id": "1", "title": "好品", "images": [], "price_cny": 50, "source_url": urls[0]}]

        def delete(self, ids):
            return {"deleted": 0}

        def shops(self):
            return [{"shopId": 77}]

        def tkcall(self, endpoint, body):
            if endpoint == CLAIMED_PATH:
                return {"ok": True, "data": {"platformCollectBoxDetailIdMap": {"tiktok": {"1": 1001}}}}
            if endpoint == "get_shop_collect_item_info":
                return {"ok": True, "data": {"ossMd5": "m", "shopCollectItemInfo": {"title": "X"}}}
            return {"ok": True, "data": {}}

    async def fake_llm(system, user):
        return [{"i": 0, "score": 90}]

    monkeypatch.setattr(t, "_make_miaoshou", lambda: FakeMS())
    monkeypatch.setattr(t, "_llm_json", fake_llm)

    tenant = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    await _seed_batch(tenant, batch_id, {"threshold": 70, "list_tiktok": True, "tk_auto": True})
    await _run_worker(tenant, batch_id)

    async with tenant_session(tenant) as db:
        job = await SourcingRepository(db).get_job(batch_id)
        assert job.post_status == "done"
        assert job.result["publish"]["summary"]["published"] == 1


async def test_post_process_idempotent_double_defer(monkeypatch):
    """T4.2：同批 defer 两次（模拟 cron 重投撞 worker）→ CAS 认领只让一个跑，fetch 一次。"""
    import app.domains.sourcing.tasks as t

    calls = {"fetch": 0}

    class FakeMS:
        def url_fetch(self, urls, limit=None):
            calls["fetch"] += 1
            return [{"id": "1", "title": "x", "images": [], "price_cny": 1, "source_url": urls[0]}]

        def delete(self, ids):
            return {"deleted": 0}

    async def fake_llm(system, user):
        return [{"i": 0, "score": 90}]

    ms = FakeMS()
    monkeypatch.setattr(t, "_make_miaoshou", lambda: ms)
    monkeypatch.setattr(t, "_llm_json", fake_llm)

    tenant = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    await _seed_batch(tenant, batch_id, {"threshold": 70})
    async with queue_app.open_async():
        from app.domains.sourcing.tasks import post_process

        await post_process.defer_async(batch_id=batch_id, tenant_id=tenant)
        await post_process.defer_async(batch_id=batch_id, tenant_id=tenant)
        await queue_app.run_worker_async(wait=False, install_signal_handlers=False)

    assert calls["fetch"] == 1  # 第二个 job 认领失败被跳过
    async with tenant_session(tenant) as db:
        assert (await SourcingRepository(db).get_job(batch_id)).post_status == "done"


async def test_post_process_skips_already_done(monkeypatch):
    """T4.2：已 done 的批不重跑（fetch 不被调用）。"""
    import app.domains.sourcing.tasks as t

    class BoomMS:
        def url_fetch(self, urls, limit=None):
            raise AssertionError("已 done 不该再 fetch")

    monkeypatch.setattr(t, "_make_miaoshou", lambda: BoomMS())

    tenant = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    await _seed_batch(tenant, batch_id, {"threshold": 70})
    async with tenant_session(tenant) as db:
        await SourcingRepository(db).set_post_status(batch_id, "done")

    await _run_worker(tenant, batch_id)

    async with tenant_session(tenant) as db:
        assert (await SourcingRepository(db).get_job(batch_id)).post_status == "done"


async def test_post_process_uses_items_when_no_urls(monkeypatch):
    """桥接 done→post_process：插件已回传商品(items)时直接评分，不再走妙手 url_fetch。"""
    import json

    from sqlalchemy import text

    import app.domains.sourcing.tasks as t

    class FakeMS:
        def url_fetch(self, urls, limit=None):
            raise AssertionError("有 items 不该再 fetch")

        def delete(self, ids):
            return {"deleted": 0}

    async def fake_llm(system, user):
        return [{"i": 0, "score": 90}]

    monkeypatch.setattr(t, "_make_miaoshou", lambda: FakeMS())
    monkeypatch.setattr(t, "_llm_json", fake_llm)

    tenant = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    async with tenant_session(tenant) as db:
        await SourcingRepository(db).create_batch(
            batch_id=batch_id, urls=[], options={"threshold": 70}, market="1688"
        )
        await db.execute(
            text("UPDATE collect_jobs SET result = CAST(:r AS jsonb) WHERE id = :i"),
            {
                "r": json.dumps({
                    "items": [{"id": "1", "title": "耳机", "price_cny": 5, "source_url": "u"}],
                    "options": {"threshold": 70},
                }),
                "i": batch_id,
            },
        )

    await _run_worker(tenant, batch_id)

    async with tenant_session(tenant) as db:
        job = await SourcingRepository(db).get_job(batch_id)
        assert job.post_status == "done"
        assert job.result["scores"][0]["pass"] is True


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
