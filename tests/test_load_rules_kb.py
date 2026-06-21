"""灌库脚本测试：纯 helper 单测（无网/无 DB）+ 集成幂等小样本（需 DB+key）。"""
import asyncpg
import pytest

from app.core.config import get_settings
from scripts.load_rules_kb import _embed_input, _vec_literal, load

# ---- 纯 helper（无副作用）----

def test_embed_input_joins_nonempty_and_caps():
    row = {"title": "标题", "summary": "摘要", "content": "正文"}
    assert _embed_input(row) == "标题\n摘要\n正文"
    assert _embed_input({"title": "", "summary": "只有摘要", "content": ""}) == "只有摘要"
    big = {"title": "x" * 5000, "summary": "", "content": ""}
    assert len(_embed_input(big)) == 2000


def test_vec_literal_format():
    assert _vec_literal([1.0, 2.5, -3.0]) == "[1.0,2.5,-3.0]"
    assert _vec_literal([0, 1]).startswith("[") and _vec_literal([0, 1]).endswith("]")


# ---- 集成（需 DB + DASHSCOPE_API_KEY）----

needs_env = pytest.mark.skipif(
    not get_settings().dashscope_api_key, reason="需 DASHSCOPE_API_KEY"
)


async def _count() -> int:
    try:
        c = await asyncpg.connect(get_settings().database_admin_url.replace("+asyncpg", ""), timeout=3)
    except Exception:
        pytest.skip("无 DB")
    try:
        return await c.fetchval("SELECT count(*) FROM rules_kb")
    finally:
        await c.close()


@needs_env
async def test_load_is_idempotent_and_embeds():
    n1 = await load(limit=3)
    assert n1 == 3, "应灌入 3 条"
    after_first = await _count()
    # 嵌入非空
    c = await asyncpg.connect(get_settings().database_admin_url.replace("+asyncpg", ""))
    try:
        null_emb = await c.fetchval("SELECT count(*) FROM rules_kb WHERE embedding IS NULL")
    finally:
        await c.close()
    assert null_emb == 0, "灌入行 embedding 不应为 null"
    # 重跑同样 3 条 → 表计数不变（ON CONFLICT 幂等）
    await load(limit=3)
    assert await _count() == after_first, "重跑不应翻倍"
