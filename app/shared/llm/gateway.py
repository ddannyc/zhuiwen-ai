"""LLM 网关封装。所有模型调用都经过这里，集中做：
  - 多模型路由（经 LiteLLM）
  - 按租户打标签（用于计费和 Langfuse 归因）
业务代码不直接调 OpenAI/Anthropic SDK。
"""
import httpx

from app.core.config import get_settings
from app.core.database import current_tenant_id

settings = get_settings()


async def chat(messages: list[dict], model: str = "gpt-4o-mini", **kwargs) -> str:
    tenant_id = current_tenant_id.get()
    async with httpx.AsyncClient(base_url=settings.litellm_base_url) as client:
        resp = await client.post(
            "/chat/completions",
            headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
            json={
                "model": model,
                "messages": messages,
                # LiteLLM 会按 metadata 聚合用量 —— 计费按租户算账靠这个
                "metadata": {"tenant_id": tenant_id},
                **kwargs,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def chat_with_tools(
    messages: list[dict], tools: list[dict], model: str = "gpt-4o-mini", **kwargs
) -> dict:
    """带工具的对话。透传 tools 给 LiteLLM（OpenAI 兼容），返回完整 message
    （含 tool_calls）。供 chat 域 agent 在 LangGraph 节点内手动跑工具循环用。

    刻意不引入 langchain-openai：守住"gateway 是 LLM 唯一出口"。
    返回的 message dict 形如：
      {"role": "assistant", "content": str|None, "tool_calls": [...]?}
    """
    tenant_id = current_tenant_id.get()
    async with httpx.AsyncClient(base_url=settings.litellm_base_url) as client:
        resp = await client.post(
            "/chat/completions",
            headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
            json={
                "model": model,
                "messages": messages,
                "tools": tools,
                "metadata": {"tenant_id": tenant_id},
                **kwargs,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]
