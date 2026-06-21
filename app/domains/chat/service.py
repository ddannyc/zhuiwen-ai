"""Chat 域业务逻辑层 —— 域对外暴露的唯一入口。

其他域/渠道（router、飞书适配器）只调这里，不碰 ChatRepository、agent 细节。
对标旧 zhuiwen_web.py 的 chat() / agent_act() / analyze() / chat_vision()。

产出对齐前端 web/src/lib/contract.ts：结构化 ChatAction + SSE 事件流。
"""
import asyncio
import re
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
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


# 泄露探测：内部工具名（snake_case，绝不该出现在用户回复）、调用参数 JSON、
# 以及模型把"调用计划/反问是否调用"当回复正文吐出的话术。命中即判为内部过程泄露。
_LEAK_RE = re.compile(
    r"rules_search|box_list|box_count|box_delete|box_translate|box_list_tiktok|collect_products"
    r'|"\s*(?:query|platform|keywords)\s*"\s*:'
    r"|是否需要我.{0,12}(?:发起|调用|查询)"
    r"|(?:立即|请|我将|让我)\s*调用.{0,8}工具"
    r"|发起(?:该|这个|此)?查询",
    re.IGNORECASE,
)
_LEAK_FALLBACK = (
    "抱歉，我刚才没能正确处理这个问题。请换种说法再问一次，或更具体地描述你的需求。"
)

# 合规召回闸：用户问题含这些信号 → 强制走 rules_search，不让弱模型漏路由后自由编。
# 多召回无妨（检索不到由空检索硬守卫兜「未找到」），漏召回才危险。
_COMPLIANCE_RE = re.compile(
    r"促销|规则|规范|政策|合规|禁售|限售|禁限售|类目|准入|认证|资质|知识产权|商标|侵权"
    r"|处罚|罚款|罚金|封号|封禁|违规|降权|佣金|费率|费用|关税|税|发票|退货|退款|争议|审核|资料要求|文件要求"
)
# 假引用闸：回复声称官方/规则库背书，但本轮没真检索（action≠rules_search）→ 欺骗性幻觉。
_VERIFY_CLAIM_RE = re.compile(
    r"依据官方|官方文档|官方规则|规则库|附来源|经核查|官方政策|已(?:通过|经).{0,6}(?:验证|核查)"
)
_FALSE_CITE_FALLBACK = (
    "抱歉，这个问题我未能从平台规则库取证，无法给出确证的合规结论。"
    "请以平台官方最新公告为准，或换个更具体的问法以便我检索。"
)


class ChatService:
    def __init__(self, session: AsyncSession, model: str | None = None):
        self.session = session
        self.model = model or get_settings().chat_model
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

        # 合规召回闸：命中合规信号 → 首轮强制 rules_search。
        force_tool = "rules_search" if _COMPLIANCE_RE.search(user_message) else None

        agent = build_chat_agent(self.session, model=self.model)
        final = await agent.ainvoke({
            "messages": messages, "action": None, "tools_used": [], "reply": "", "rounds": 0,
            "force_tool": force_tool, "user_message": user_message,
        })

        if is_first:
            title = await self._gen_title(user_message)
            if title:
                await self.repo.update_conversation_title(conversation_id, title)

        action = final.get("action") or {"type": "answer"}
        reply = final.get("reply", "")
        # 合规硬保障：规则检索为空时，确定性覆盖回复——杜绝模型凭记忆编造平台规则
        # （弱模型即便拿到"未找到"也可能幻觉，prompt 约束不可靠，故在此强制兜底）。
        if action.get("type") == "rules_search" and action.get("empty"):
            reply = "未找到相关平台规则，请以平台最新官方公告为准。（知识库无匹配，不臆测。）"

        # 假引用闸：回复声称官方/规则库背书，但本轮没真检索（无 cite）→ 欺骗性幻觉，替换。
        if action.get("type") != "rules_search" and _VERIFY_CLAIM_RE.search(reply):
            reply = _FALSE_CITE_FALLBACK

        # 兜底：模型把内部工具名/调用参数/调用计划当回复吐给用户 → 判为泄露，替换为安全话术。
        # （提示词已禁，但弱模型仍可能漏；用户绝不应看到内部编排过程。）
        if _LEAK_RE.search(reply):
            reply = _LEAK_FALLBACK

        return {"reply": reply, "action": action, "tools_used": final.get("tools_used", [])}

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
        """SSE 事件流（对齐 contract.ts SseEvent），两段式：
        1) 立即推占位（检索中/思考中），消除生成期空白等待；
        2) 完整生成 + 全部防幻觉守卫（_run）；
        3) 逐字打字推送已守卫的安全文本（块间延迟，可见打字）。
        守卫全程生效：用户只会看到守卫后的最终文本，不会先看到未守卫内容。
        """
        # 1) 立即占位反馈。合规问题确定会检索（召回闸），故直接显示"检索平台规则"。
        compliance = bool(_COMPLIANCE_RE.search(user_message))
        yield {"event": "tool_running",
               "data": {"tool": "thinking",
                        "label": "检索平台规则…" if compliance else "思考中…"}}

        # 2) 完整生成 + 守卫（空检索/泄露/假引用 都在此应用到 reply）
        out = await self._run(conversation_id, user_message)
        action, reply, tools_used = out["action"], out["reply"], out["tools_used"]

        yield {"event": "action", "data": {"type": action["type"]}}
        for t in tools_used:
            yield {"event": "tool_running", "data": {"tool": t, "label": TOOL_LABELS.get(t, t)}}

        # 3) 逐字打字（已守卫文本，块间小延迟让前端可见地逐步渲染）
        for delta in _chunk(reply, 10):
            yield {"event": "token", "data": {"delta": delta}}
            await asyncio.sleep(0.02)

        yield {"event": "payload", "data": action}
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
