"""知识库的数据模型。

注意 tenant_id 列：它存在是为了让 RLS 策略能过滤，但业务代码从不手动填它，
也从不手动按它查询 —— 由数据库的 RLS 策略 + 默认值负责。
向量列用 pgvector。
"""
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# RLS 列默认值：server_default 必须声明在 ORM 上，否则 SQLAlchemy INSERT 会塞 NULL
# （而非省略列让 DB DEFAULT 生效），导致 RLS WITH CHECK 失败。
_TENANT_DEFAULT = text("current_setting('app.current_tenant')::uuid")


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "kb_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # RLS 用列。DEFAULT current_setting(...) 由迁移脚本设置，应用层不填。
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=_TENANT_DEFAULT)
    title: Mapped[str] = mapped_column(String(512))
    source_uri: Mapped[str] = mapped_column(String(1024))


class Chunk(Base):
    __tablename__ = "kb_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=_TENANT_DEFAULT)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024))  # bge-m3 = 1024 维
