"""LLM 网关封装。所有 chat 调用经此 —— LLM 唯一出口。

LiteLLM **SDK 进程内**调用（无独立代理服务）：在本进程完成 provider 路由、
请求/响应在 OpenAI 格式与 provider 格式间翻译、工具调用归一、按租户 metadata 归因。
默认打阿里百炼 (DashScope) OpenAI 兼容端点；本地无 key 时把 dashscope_base_url
指向 scripts/mock_llm_server.py 即可。

业务代码（agent/service）不直接调 litellm/provider SDK，只调这里。
"""
import litellm

from app.core.config import get_settings
from app.core.database import current_tenant_id

settings = get_settings()
litellm.drop_params = True  # provider 不支持的参数自动丢弃，避免 400


def _params(model: str | None) -> dict:
    # model 加 openai/ 前缀 → 走指定 api_base 的 OpenAI 兼容端点（DashScope / mock）。
    return {
        "model": f"openai/{model or settings.chat_model}",
        "api_base": settings.dashscope_base_url,
        "api_key": settings.dashscope_api_key or "sk-nokey",
        # LiteLLM 按 metadata 聚合用量 —— 计费按租户算账靠这个
        "metadata": {"tenant_id": current_tenant_id.get()},
    }


async def chat(messages: list[dict], model: str | None = None, **kwargs) -> str:
    resp = await litellm.acompletion(messages=messages, **_params(model), **kwargs)
    return resp.choices[0].message.content or ""


async def chat_with_tools(
    messages: list[dict], tools: list[dict], model: str | None = None, **kwargs
) -> dict:
    """带工具的对话。返回完整 message（含 tool_calls）的 dict，供 chat 域 agent
    在 LangGraph 节点内手动跑工具循环。LiteLLM 已把各 provider 的工具调用归一为
    OpenAI 格式。"""
    resp = await litellm.acompletion(
        messages=messages, tools=tools, **_params(model), **kwargs
    )
    msg = resp.choices[0].message
    # litellm 返回 pydantic Message → 转 dict：agent 用 .get() 访问，且要回灌进 messages
    return msg.model_dump()
