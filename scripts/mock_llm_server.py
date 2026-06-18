"""最小 OpenAI 兼容 LLM mock（仅本地 demo 用，替代未起的 LiteLLM）。

提供 POST /chat/completions：
  - 不返回 tool_calls（单轮即出答），让 chat agent 走 answer 路径，浏览器能看到回复。
  - 回复用简体中文，呼应用户最后一句。

起：uv run uvicorn scripts.mock_llm_server:app --port 4000
"""
from fastapi import FastAPI, Request

app = FastAPI(title="mock-llm")


@app.post("/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    msgs = body.get("messages", [])
    sys = next((m.get("content", "") for m in msgs if m.get("role") == "system"), "")
    user = next((m["content"] for m in reversed(msgs)
                 if m.get("role") == "user" and isinstance(m.get("content"), str)), "")

    # 标题请求（system 提到「标题」）：回简短标题，别回整段。
    if isinstance(sys, str) and "标题" in sys:
        title = (user.strip().splitlines() or [""])[0][:12] or "新对话"
        return {"id": "mock-t", "object": "chat.completion", "model": body.get("model", "mock"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": title}}]}

    reply = (
        "### 结论先行\n"
        f"已收到你的问题：「{user[:60]}」。\n\n"
        "这是本地 mock LLM 的占位回答（LiteLLM 未接真实模型）。"
        "工具路由、规则检索、SSE 流式与落库均为真实链路。\n\n"
        "| 维度 | 状态 |\n|---|---|\n| 登录/会话 | 真实 |\n| LLM 文本 | mock 占位 |"
    )
    return {
        "id": "mock-1", "object": "chat.completion", "model": body.get("model", "mock"),
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": reply}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
