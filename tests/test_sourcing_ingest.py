"""Phase2 T2.2：POST /sourcing/ingest（扩展回传 URL 批 → 存库 + defer 后处理）。

验收（tasks/plan.md T2.2）：带 JWT POST urls → 200 + batch_id + post_status；
非 1688 URL → 422；空 → 422；存批走 RLS（跨租户 GET 404）。
真 PG 必需（RLS + defer 落 procrastinate_jobs）；不可达则 skip。
"""
import uuid

import httpx
import psycopg
import pytest

from app.core.config import get_settings
from app.main import app
from app.shared.auth.jwt import issue_token

_OFFER1 = "https://detail.1688.com/offer/111.html"
_OFFER2 = "https://detail.1688.com/offer/222.html"


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


def _token() -> str:
    return issue_token(user_id=str(uuid.uuid4()), tenant_id=str(uuid.uuid4()))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_ingest_stores_batch_and_queues():
    h = {"Authorization": f"Bearer {_token()}"}
    async with _client() as c:
        r = await c.post(
            "/sourcing/ingest",
            headers=h,
            json={"market": "1688", "urls": [_OFFER1, _OFFER2, _OFFER1], "options": {"threshold": 80}},
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["accepted"] == 2  # 去重
        assert j["post_status"] in ("queued", "pending")
        bid = j["batch_id"]

        g = await c.get(f"/sourcing/jobs/{bid}", headers=h)
        assert g.status_code == 200, g.text
        gj = g.json()
        assert gj["result"]["urls"] == [_OFFER1, _OFFER2]


async def test_ingest_rejects_non_1688_url():
    h = {"Authorization": f"Bearer {_token()}"}
    async with _client() as c:
        r = await c.post("/sourcing/ingest", headers=h, json={"urls": ["https://taobao.com/x"]})
        assert r.status_code == 422, r.text


async def test_ingest_rejects_empty_urls():
    h = {"Authorization": f"Bearer {_token()}"}
    async with _client() as c:
        r = await c.post("/sourcing/ingest", headers=h, json={"urls": []})
        assert r.status_code == 422, r.text


async def test_ingest_batch_is_tenant_isolated():
    a = {"Authorization": f"Bearer {_token()}"}
    b = {"Authorization": f"Bearer {_token()}"}
    async with _client() as c:
        bid = (
            await c.post("/sourcing/ingest", headers=a, json={"urls": [_OFFER1]})
        ).json()["batch_id"]
        # 另一租户 GET 拿不到（RLS）
        assert (await c.get(f"/sourcing/jobs/{bid}", headers=b)).status_code == 404
