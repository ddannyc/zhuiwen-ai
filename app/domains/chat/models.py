"""Chat 域数据模型。

镜像 knowledge_base/models.py 的纪律：
  - tenant_id 列存在只为让 RLS 过滤，应用代码从不手动填、从不手动按它查询，
    靠迁移脚本的 DEFAULT current_setting('app.current_tenant') + RLS 策略兜底。
  - user_id 是会话归属人（非租户键），由应用按登录态填（见计划假设 6）：
    会话按 user_id 归属、按 tenant_id（RLS）隔离 —— 同租户不同员工互不可见对方会话。
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # RLS 用列。server_default 必须声明在 ORM 上，否则 SQLAlchemy 会在 INSERT 里
    # 塞 NULL（而非省略列让 DB DEFAULT 生效），导致 RLS WITH CHECK 失败。
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False,
        server_default=text("current_setting('app.current_tenant')::uuid"),
    )
    # 归属人（应用按登录态填）。非租户键，RLS 不管它。
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(512), default="新对话")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False,
        server_default=text("current_setting('app.current_tenant')::uuid"),
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(32))          # user / assistant / system / tool
    content: Mapped[str] = mapped_column(Text)
    # 结构化 ChatAction（对齐前端 contract.ts）：
    # {"type":"answer"|"analyze"|"box_list"(+rows)|"rules_search"(+empty,cites)|"collect_products"(+job_id)}
    action: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
