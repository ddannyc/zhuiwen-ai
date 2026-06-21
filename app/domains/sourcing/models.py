"""sourcing 域数据模型。

collect_jobs 替代旧版进程内存的 _COLLECT_JOBS 数组：持久化、可重试、租户隔离。
tenant_id 列只为 RLS 过滤存在，应用层从不手填、从不手查 —— 由 DB 的
server_default + RLS 策略负责（与 knowledge_base/models.py 同套路）。
"""
import uuid
from datetime import datetime

from sqlalchemy import Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# RLS 列默认值：server_default 必须声明在 ORM 上，否则 SQLAlchemy INSERT 会塞 NULL
# （而非省略列让 DB DEFAULT 生效），导致 RLS WITH CHECK 失败。
_TENANT_DEFAULT = text("current_setting('app.current_tenant')::uuid")

# 任务状态机：
#   pending    刚下发，等采集插件认领
#   collecting 插件已 poll 认领，正在抓取
#   collected  插件已 /done 回结果，等 workflow 后处理（评分/翻译/上架）
#   completed  全流程完成
#   failed     超时或下游失败
PENDING = "pending"
COLLECTING = "collecting"
COLLECTED = "collected"
COMPLETED = "completed"
FAILED = "failed"


class Base(DeclarativeBase):
    pass


class CollectJob(Base):
    __tablename__ = "collect_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # RLS 用列。DEFAULT current_setting(...) 由迁移脚本设置，应用层不填。
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=_TENANT_DEFAULT)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'pending'"))
    keywords: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    per_kw: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    market: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
