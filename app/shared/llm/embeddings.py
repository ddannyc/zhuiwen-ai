"""向量化封装。LLM 唯一出口——经 litellm SDK 进程内调 DashScope，与 gateway.chat 同源同 key。

不再走独立 litellm 代理（旧 httpx → localhost:4000，已弃）。默认 text-embedding-v3，
1024 维（对齐 kb_chunks vector(1024)）。knowledge_base 与 rules_kb 域共用本函数。
"""
import litellm

from app.core.config import get_settings

settings = get_settings()
# DashScope text-embedding-v3 走 OpenAI 兼容端点；litellm 把 dimensions 等"OpenAI v3 不支持"
# 的参数视为非法，drop_params 让其自动丢弃（丢后取 v3 默认 1024 维，正合预期）。
litellm.drop_params = True


async def embed_text(texts: list[str], model: str | None = None) -> list[list[float]]:
    # model 加 openai/ 前缀 → 走指定 api_base 的 OpenAI 兼容端点（DashScope），同 gateway。
    resp = await litellm.aembedding(
        model=f"openai/{model or settings.embedding_model}",
        input=texts,
        api_base=settings.dashscope_base_url,
        api_key=settings.dashscope_api_key or "sk-nokey",
    )
    return [d["embedding"] for d in resp.data]
