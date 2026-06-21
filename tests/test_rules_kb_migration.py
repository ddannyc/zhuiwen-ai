"""0006_rules_kb 迁移结构契约（需 DB；无 DB 自动 skip）。

固化 SPEC：rules_kb 为全局共享表——无 tenant_id、无 RLS（区别 kb_chunks），
embedding vector(1024)，hnsw 索引。
"""
import asyncpg
import pytest

from app.core.config import get_settings


def _admin_dsn() -> str:
    return get_settings().database_admin_url.replace("+asyncpg", "")


async def _conn():
    try:
        return await asyncpg.connect(_admin_dsn(), timeout=3)
    except Exception:
        pytest.skip("无 DB（database_admin_url 不可达）")


async def test_rules_kb_table_columns():
    c = await _conn()
    try:
        cols = await c.fetch(
            "SELECT column_name, udt_name FROM information_schema.columns "
            "WHERE table_name='rules_kb'"
        )
    finally:
        await c.close()
    names = {r["column_name"]: r["udt_name"] for r in cols}
    assert names, "rules_kb 表应存在"
    assert "tenant_id" not in names, "全局共享表不应有 tenant_id"
    assert names.get("embedding") == "vector", "embedding 须为 pgvector vector 类型"
    assert names.get("rule_id") == "uuid"
    for col in ("platform", "site", "summary", "source_url", "version",
                "verification_status", "confidence", "rule_domain"):
        assert col in names, f"缺列 {col}"


async def test_rules_kb_no_rls():
    c = await _conn()
    try:
        rls = await c.fetchval(
            "SELECT relrowsecurity FROM pg_class WHERE relname='rules_kb'"
        )
    finally:
        await c.close()
    assert rls is False, "全局共享表不应启用 RLS"


async def test_rules_kb_hnsw_index():
    c = await _conn()
    try:
        idx = await c.fetch(
            "SELECT indexdef FROM pg_indexes WHERE tablename='rules_kb'"
        )
    finally:
        await c.close()
    defs = " ".join(r["indexdef"] for r in idx)
    assert "hnsw" in defs.lower(), "应有 hnsw 向量索引"
