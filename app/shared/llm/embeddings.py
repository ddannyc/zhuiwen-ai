"""向量化封装。同样经 LiteLLM，方便统一切换 bge-m3 / OpenAI 等。"""
import httpx

from app.core.config import get_settings

settings = get_settings()


async def embed_text(texts: list[str], model: str = "bge-m3") -> list[list[float]]:
    async with httpx.AsyncClient(base_url=settings.litellm_base_url) as client:
        resp = await client.post(
            "/embeddings",
            headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
            json={"model": model, "input": texts},
            timeout=60,
        )
        resp.raise_for_status()
        return [d["embedding"] for d in resp.json()["data"]]
