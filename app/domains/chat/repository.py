"""Chat 域数据访问层。

镜像 knowledge_base/repository.py：没有任何 `WHERE tenant_id = ...`，
租户隔离交 DB 层 RLS 兜底。唯一例外是 list_conversations 显式 `WHERE user_id`
—— user_id 是归属键不是租户键，按它过滤是业务需求（同租户员工互不可见对方会话），
不违背"不手写租户过滤"的纪律。
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.chat.models import Conversation, Message


class ChatRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_conversation(self, user_id: str, title: str = "新对话") -> Conversation:
        # 不传 tenant_id —— 由表 DEFAULT current_setting('app.current_tenant') 填充。
        conv = Conversation(user_id=uuid.UUID(str(user_id)), title=title)
        self.session.add(conv)
        await self.session.flush()
        return conv

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        # RLS 已保证只能取到本租户的会话。
        return await self.session.get(Conversation, uuid.UUID(str(conversation_id)))

    async def update_conversation_title(self, conversation_id: str, title: str) -> None:
        conv = await self.session.get(Conversation, uuid.UUID(str(conversation_id)))
        if conv is not None:
            conv.title = title
            await self.session.flush()

    async def add_message(
        self, conversation_id: str, role: str, content: str, action: str | None = None
    ) -> Message:
        msg = Message(
            conversation_id=uuid.UUID(str(conversation_id)),
            role=role,
            content=content,
            action=action,
        )
        self.session.add(msg)
        await self.session.flush()
        return msg

    async def list_messages(self, conversation_id: str, limit: int = 50) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == uuid.UUID(str(conversation_id)))
            .order_by(Message.created_at)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def recent_messages(self, conversation_id: str, limit: int) -> list[Message]:
        """取最近 limit 条（DESC 取，再反转回时间正序）。
        用于喂 LLM 历史：必须是最近的对话，不能像 list_messages 那样取最早 N 条。"""
        stmt = (
            select(Message)
            .where(Message.conversation_id == uuid.UUID(str(conversation_id)))
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(reversed(result.scalars().all()))

    async def list_conversations(self, user_id: str, limit: int = 50) -> list[Conversation]:
        # 显式按归属人过滤（业务键，非租户键）；租户隔离仍由 RLS 兜底。
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == uuid.UUID(str(user_id)))
            .order_by(Conversation.created_at.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
