"""embed_text 经 litellm SDK→DashScope 的连通契约（需真实 DASHSCOPE_API_KEY，否则 skip）。

固化 SPEC：embedding 唯一经 litellm SDK→DashScope，维度 1024（与 kb_chunks 对齐）。
"""
import pytest

from app.core.config import get_settings
from app.shared.llm.embeddings import embed_text

needs_key = pytest.mark.skipif(
    not get_settings().dashscope_api_key, reason="需 DASHSCOPE_API_KEY 才能打真实 embedding 端点"
)


@needs_key
async def test_embed_text_returns_1024_dim_per_input():
    embs = await embed_text(["跨境电商平台合规规则", "second item"])
    assert len(embs) == 2, "每条输入对应一个向量"
    assert all(len(e) == 1024 for e in embs), "维度须 1024（对齐 kb_chunks vector(1024)）"
    assert all(isinstance(x, float) for x in embs[0][:5])


@needs_key
async def test_embed_text_single():
    [emb] = await embed_text(["单条输入"])
    assert len(emb) == 1024
