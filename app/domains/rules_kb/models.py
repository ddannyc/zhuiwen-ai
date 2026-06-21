"""rules_kb 域数据模型——全局共享平台规则库。

区别 kb_chunks：**无 tenant_id、无 RLS**（平台规则跨租户通用，所有租户共读同一份）。
向量列用 pgvector（1024 维，对齐 embed_text 的 DashScope text-embedding-v3）。
chat agent rules_search 工具经 RulesKbService 检索本表。
"""
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RulesKbRow(Base):
    __tablename__ = "rules_kb"

    rule_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    site: Mapped[str | None] = mapped_column(Text)
    original_language: Mapped[str | None] = mapped_column(Text)
    rule_domain: Mapped[str | None] = mapped_column(Text)
    rule_type: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(Text)
    # 日期列存字符串（jsonl 原样往返，契约要求 last_verified_at 回字符串）
    effective_date: Mapped[str | None] = mapped_column(Text)
    expiry_date: Mapped[str | None] = mapped_column(Text)
    last_verified_at: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(Text)
    product_category: Mapped[list] = mapped_column(JSONB, default=list)
    related_rule_ids: Mapped[list] = mapped_column(JSONB, default=list)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024))
