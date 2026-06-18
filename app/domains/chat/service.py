"""Chat 域业务逻辑层 —— 域对外暴露的唯一入口。

其他域/渠道（router、飞书适配器）只调这里，不碰 ChatRepository、agent 细节。
对标旧 zhuiwen_web.py 的 chat() / agent_act() / analyze() / chat_vision()。

产出对齐前端 web/src/lib/contract.ts：结构化 ChatAction + SSE 事件流。
"""
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.chat.agent import TOOL_LABELS, build_chat_agent
from app.domains.chat.prompts import (
    ANALYSIS_PROMPTS,
    DEFAULT_ANALYSIS,
    HISTORY_CHAR_CAP,
    HISTORY_LIMIT,
    MESSAGE_CHAR_CAP,
    _ACT_SYSTEM,
    _CHAT_SYS,
    _TITLE_SYS,
    _VISION_DEFAULT,
)
from app.domains.chat.repository import ChatRepository
from app.shared.llm.gateway import chat


def _iso(dt) -> str:
    return dt.isoformat() if dt is not None else ""


class ChatService:
    def __init__(self, session: AsyncSession, model: str = "gpt-4o-mini"):
        self.session = session
        self.model = model
        self.repo = ChatRepository(session)

    # ---- 会话管理 ----
    async def create_conversation(self, user_id: str, title: str = "新对话") -> dict[str, Any]:
        conv = await self.repo.create_conversation(user_id, title)
        return {
            "id": str(conv.id), "user_id": str(conv.user_id),
            "title": conv.title, "created_at": _iso(conv.created_at),
        }

    async def list_conversations(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        convs = await self.repo.list_conversations(user_id, limit)
        return [{
            "id": str(c.id), "user_id": str(c.user_id),
            "title": c.title, "created_at": _iso(c.created_at),
        } for c in convs]

    async def user_owns_conversation(self, user_id: str, conversation_id: str) -> bool:
        """归属校验：会话必须属于该 user_id。RLS 只隔离 tenant，同租户不同员工
        互不可见对方会话靠这里把关（计划假设 6）。非法 id / 不存在 → False。"""
        try:
            conv = await self.repo.get_conversation(conversation_id)
        except (ValueError, TypeError):
            return False
        return conv is not None and str(conv.user_id) == str(user_id)

    async def list_messages(self, conversation_id: str, limit: int = 50) -> list[dict[str, Any]]:
        msgs = await self.repo.list_messages(conversation_id, limit)
        return [{
            "id": str(m.id), "conversation_id": str(m.conversation_id),
            "role": m.role, "content": m.content,
            "action": m.action, "created_at": _iso(m.created_at),
        } for m in msgs]

    # ---- 主入口：跑 agent，返回结构化结果（非流式，给 converse_stream 与单测复用）----
    async def _run(self, conversation_id: str, user_message: str) -> dict[str, Any]:
        # 首条消息（落库前会话无消息）→ 跑完用 LLM 生成会话标题。
        is_first = not await self.repo.list_messages(conversation_id, limit=1)
        await self.repo.add_message(conversation_id, "user", user_message[:MESSAGE_CHAR_CAP])
        history = await self._build_llm_history(conversation_id)
        messages = [{"role": "system", "content": _ACT_SYSTEM}, *history]

        agent = build_chat_agent(self.session, model=self.model)
        final = await agent.ainvoke({
            "messages": messages, "action": None, "tools_used": [], "reply": "", "rounds": 0,
        })

        if is_first:
            title = await self._gen_title(user_message)
            if title:
                await self.repo.update_conversation_title(conversation_id, title)

        return {
            "reply": final.get("reply", ""),
            "action": final.get("action") or {"type": "answer"},
            "tools_used": final.get("tools_used", []),
        }

    async def _gen_title(self, message: str) -> str:
        """首条消息生成简短标题。失败不抛（标题非关键路径），返回空串跳过。"""
        try:
            raw = await chat(
                messages=[{"role": "system", "content": _TITLE_SYS},
                          {"role": "user", "content": message[:200]}],
                model=self.model,
            )
        except Exception:
            return ""
        line = (raw or "").strip().strip("\"'“”「」").splitlines()
        return line[0][:20] if line else ""

    async def converse(self, conversation_id: str, user_message: str) -> dict[str, Any]:
        """一次性返回 {reply, action(结构化)}。对标 /api/agent/act。"""
        out = await self._run(conversation_id, user_message)
        await self.repo.add_message(conversation_id, "assistant", out["reply"], action=out["action"])
        return {"reply": out["reply"], "action": out["action"]}

    async def converse_stream(
        self, conversation_id: str, user_message: str
    ) -> AsyncIterator[dict[str, Any]]:
        """SSE 事件流（对齐 contract.ts SseEvent）：
        action → tool_running* → token* → payload → done。

        注：本期 LLM 调用是非流式，token 事件由最终回复在服务端切块产生（真 SSE 传输，
        逐 token LLM 流式留待 gateway 增 chat_stream 后接入）。
        """
        out = await self._run(conversation_id, user_message)
        action, reply, tools_used = out["action"], out["reply"], out["tools_used"]

        # 1) 骨架：先告诉前端动作类型，挂占位
        yield {"event": "action", "data": {"type": action["type"]}}
        # 2) 工具执行轨迹
        for t in tools_used:
            yield {"event": "tool_running", "data": {"tool": t, "label": TOOL_LABELS.get(t, t)}}
        # 3) token：切块推送（中文按字、英文按词的折中：按空白切，无空白则整体）
        for delta in _chunk(reply):
            yield {"event": "token", "data": {"delta": delta}}
        # 4) 完整结构化 payload（带 rows/cites/job_id）
        yield {"event": "payload", "data": action}
        # 5) 落库 + done
        msg = await self.repo.add_message(conversation_id, "assistant", reply, action=action)
        yield {"event": "done", "data": {"message_id": str(msg.id)}}

    # ---- 纯问答（对标旧 chat()）----
    async def ask(self, messages: list[dict[str, str]]) -> str:
        recent = messages[-HISTORY_LIMIT:]
        capped = [{"role": m["role"], "content": m["content"][:HISTORY_CHAR_CAP]} for m in recent]
        return await chat(messages=[{"role": "system", "content": _CHAT_SYS}, *capped], model=self.model)

    # ---- 6 类分析（对标旧 analyze()）----
    async def analyze(self, keyword: str, atype: str = DEFAULT_ANALYSIS) -> str:
        atype = atype if atype in ANALYSIS_PROMPTS else DEFAULT_ANALYSIS
        prompt = ANALYSIS_PROMPTS[atype].format(keyword=keyword)
        return await chat(
            messages=[{"role": "system", "content": "你是跨境电商分析师，结论先行，用表格。"},
                      {"role": "user", "content": prompt}],
            model=self.model,
        )

    # ---- 视觉理解（对标旧 chat_vision()）----
    async def vision(self, message: str, images: list[str]) -> str:
        imgs = images[:3]
        prompt = message.strip() or _VISION_DEFAULT
        if len(imgs) > 1:
            prompt = f"（共 {len(imgs)} 张图，先分析第 1 张）{prompt}"
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if imgs:
            content.append({"type": "image_url", "image_url": {"url": imgs[0]}})
        return await chat(messages=[{"role": "user", "content": content}], model=self.model)

    async def _build_llm_history(self, conversation_id: str) -> list[dict[str, str]]:
        msgs = await self.repo.list_messages(conversation_id, limit=HISTORY_LIMIT)
        return [
            {"role": m.role, "content": (m.content or "")[:HISTORY_CHAR_CAP]}
            for m in msgs if m.role in ("user", "assistant")
        ]


def _chunk(text: str, size: int = 24) -> list[str]:
    """把回复切成 token 块（折中：定长切片，保证 SSE 可见地逐块推送）。"""
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]
