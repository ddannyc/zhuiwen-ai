"""Chat 域业务逻辑层 —— 域对外暴露的唯一入口。

其他域/渠道（router、飞书适配器）只调这里，不碰 ChatRepository、agent 细节。
对标旧 zhuiwen_web.py 的 chat() / agent_act() / analyze() / chat_vision()。

产出对齐前端 web/src/lib/contract.ts：结构化 ChatAction + SSE 事件流。
"""
import logging
import re
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domains.chat.agent import TOOL_LABELS, prepare
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
from app.domains.chat.stream_guard import GUARD_TAIL, StreamGuard, guard_text
from app.shared.llm.gateway import chat, chat_stream

log = logging.getLogger(__name__)

# 终答生成的输出上限：防超长回复放大 CPU/内存（守卫每 delta 扫描）+ 失控消耗（DoS）。
_ANSWER_MAX_TOKENS = 1500


def _iso(dt) -> str:
    return dt.isoformat() if dt is not None else ""


# 合规召回闸：用户问题含这些信号 → 强制走 rules_search，不让弱模型漏路由后自由编。
# 多召回无妨（检索不到由空检索硬守卫兜「未找到」），漏召回才危险。
_COMPLIANCE_RE = re.compile(
    r"促销|规则|规范|政策|合规|禁售|限售|禁限售|类目|准入|认证|资质|知识产权|商标|侵权"
    r"|处罚|罚款|罚金|封号|封禁|违规|降权|佣金|费率|费用|关税|税|发票|退货|退款|争议|审核|资料要求|文件要求"
)
# 终答防幻觉守卫（泄露/假引用）单一来源在 stream_guard：非流式 guard_text + 流式 StreamGuard。


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
    async def _prepare_turn(self, conversation_id: str, user_message: str) -> dict[str, Any]:
        """落用户消息 + 路由/工具（prepare）+ 首条标题。返回 prep（含 action/needs_gen/
        gen_messages/static_reply/tools_used）。converse 与 converse_stream 共用。"""
        is_first = not await self.repo.list_messages(conversation_id, limit=1)
        await self.repo.add_message(conversation_id, "user", user_message[:MESSAGE_CHAR_CAP])
        history = await self._build_llm_history(conversation_id)
        messages = [{"role": "system", "content": _ACT_SYSTEM}, *history]
        # 合规召回闸：命中合规信号 → 首轮强制 rules_search。
        force_tool = "rules_search" if _COMPLIANCE_RE.search(user_message) else None
        prep = await prepare(self.session, messages, model=self.model,
                             force_tool=force_tool, user_message=user_message)
        if is_first:
            title = await self._gen_title(user_message)
            if title:
                await self.repo.update_conversation_title(conversation_id, title)
        return prep

    async def _run(self, conversation_id: str, user_message: str) -> dict[str, Any]:
        prep = await self._prepare_turn(conversation_id, user_message)
        action = prep["action"]
        # 终答：需生成 → gen_messages 调 chat（非流式）；否则用事前定的静态回复（空检索）。
        reply = await chat(prep["gen_messages"], model=self.model) if prep["needs_gen"] \
            else (prep["static_reply"] or "")
        return {"reply": guard_text(reply, action), "action": action,
                "tools_used": prep["tools_used"]}

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
        1) 立即推占位（检索中/思考中）消除生成期空白；
        2) prepare（路由/工具）；
        3) **真流式**：needs_gen → chat_stream 边生成边吐 token（流式出错降级非流式一次拿全）；
           空检索/静态 → 不流，前端卡片渲。
        守卫：终答累积后做（防幻觉），落库为守卫后文本（实时增量守卫见 P3）。
        """
        compliance = bool(_COMPLIANCE_RE.search(user_message))
        yield {"event": "tool_running",
               "data": {"tool": "thinking",
                        "label": "检索平台规则…" if compliance else "思考中…"}}

        prep = await self._prepare_turn(conversation_id, user_message)
        action = prep["action"]

        yield {"event": "action", "data": {"type": action["type"]}}
        for t in prep["tools_used"]:
            yield {"event": "tool_running", "data": {"tool": t, "label": TOOL_LABELS.get(t, t)}}

        if prep["needs_gen"]:
            guard = StreamGuard(action)
            reply = ""
            emitted = 0  # 已发到前端的"安全前缀"长度（留 GUARD_TAIL 尾巴不发，H1）
            try:
                async for delta in chat_stream(
                    prep["gen_messages"], model=self.model, max_tokens=_ANSWER_MAX_TOKENS
                ):
                    fallback = guard.feed(delta)  # 流式增量守卫：全程在线
                    if fallback is not None:
                        # 命中守卫（泄露/假引用）→ 停流，发 replace 覆盖（含已发的安全前缀）。
                        reply = fallback
                        yield {"event": "replace", "data": {"text": fallback}}
                        break
                    # 只发"已过匹配边界"的安全前缀，留最后 GUARD_TAIL 字符不发——不安全区在
                    # 守卫看清前永不传输（H1：replace 只是事后遮 DOM，不能当唯一控制）。
                    safe = max(0, len(guard.text) - GUARD_TAIL)
                    if safe > emitted:
                        yield {"event": "token", "data": {"delta": guard.text[emitted:safe]}}
                        emitted = safe
                else:
                    reply = guard.text  # 正常流完，无命中 → 发尾巴剩余
                    if len(guard.text) > emitted:
                        yield {"event": "token", "data": {"delta": guard.text[emitted:]}}
            except Exception as e:  # noqa: BLE001 —— 流式出错降级：非流式一次拿全
                log.warning("chat_stream 失败，降级非流式: %s", e)
                try:
                    raw = await chat(prep["gen_messages"], model=self.model, max_tokens=_ANSWER_MAX_TOKENS)
                    reply = guard_text(raw, action)
                    # 已发过部分前缀 → 用 replace 覆盖（前端 append，整段 token 会重复，Blocker#1）。
                    if emitted > 0:
                        yield {"event": "replace", "data": {"text": reply}}
                    elif reply:
                        yield {"event": "token", "data": {"delta": reply}}
                except Exception as e2:  # noqa: BLE001 —— 流式+非流式都挂：error，不留半截无终止
                    log.error("终答生成彻底失败（流式+非流式均挂）: %s", e2)
                    reply = "抱歉，生成失败，请稍后重试。"
                    if emitted > 0:  # 已有前缀残留 → 覆盖掉
                        yield {"event": "replace", "data": {"text": reply}}
                    yield {"event": "error", "data": {"msg": "生成失败，请稍后重试"}}
        else:
            # 空检索/静态：守卫文案由前端卡片渲，不流 token（避免与卡片文案重叠）。
            reply = prep["static_reply"] or ""

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
        # 取最近 N 条（含刚写入的当前 user 消息）。必须是最近的——否则长对话只喂开头
        # 几轮，模型看不到用户后续补充的信息，会反复重问。
        msgs = await self.repo.recent_messages(conversation_id, limit=HISTORY_LIMIT)
        return [
            {"role": m.role, "content": (m.content or "")[:HISTORY_CHAR_CAP]}
            for m in msgs if m.role in ("user", "assistant")
        ]
