"""Phase2 T2.1：collect_jobs 转 batch 语义——加 post_status/attempts/last_error/source。

验收（tasks/plan.md T2.1）：迁移加列、保 RLS；新列默认值正确。
旧 status(poll) 语义弃用但列暂留（Phase5 删 Temporal 时清）。
真 PG 必需；不可达则 skip。
"""
import uuid

import psycopg
import pytest
from sqlalchemy import text

from app.core.config import get_settings


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


async def test_collect_jobs_batch_columns_defaults():
    """app 角色插一行（仅 market），读新列默认：post_status=pending/attempts=0/source=1688。"""
    from app.shared.queue import tenant_session

    tenant = str(uuid.uuid4())
    marker = "t21-" + uuid.uuid4().hex[:10]
    async with tenant_session(tenant) as db:
        await db.execute(
            text("INSERT INTO collect_jobs (market) VALUES (:m)"), {"m": marker}
        )
        row = (
            await db.execute(
                text(
                    "SELECT post_status, attempts, last_error, source "
                    "FROM collect_jobs WHERE market = :m"
                ),
                {"m": marker},
            )
        ).one()
    assert row.post_status == "pending"
    assert row.attempts == 0
    assert row.last_error is None
    assert row.source == "1688"
