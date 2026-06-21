"""collect_jobs 转 batch 语义：加 post_status / attempts / last_error / source。

客户端化后 collect_jobs 从「服务端推任务、插件 poll」转为「扩展回传 URL 批 → 服务端
入队后处理」。新增 post_status 后处理状态机（pending|queued|running|done|failed）+
重试列 + 来源市场。旧 status(poll) 列暂留兼容，Phase5 删 Temporal 时一并清。

post_status 索引供 cron 兜底扫掉队 pending（ADR-001 outbox）。RLS 不变（加列不影响）。

Revision ID: 0005_sourcing_batch
Revises: 0004_procrastinate
"""
from alembic import op

revision = "0005_sourcing_batch"
down_revision = "0004_procrastinate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE collect_jobs "
        "ADD COLUMN IF NOT EXISTS post_status text NOT NULL DEFAULT 'pending', "
        "ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0, "
        "ADD COLUMN IF NOT EXISTS last_error text, "
        "ADD COLUMN IF NOT EXISTS source text DEFAULT '1688'"
    )
    # cron 兜底扫掉队 pending（post_status='pending' AND updated_at<now()-grace）。
    op.execute(
        "CREATE INDEX IF NOT EXISTS collect_jobs_post_status_idx "
        "ON collect_jobs (post_status, updated_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS collect_jobs_post_status_idx")
    op.execute(
        "ALTER TABLE collect_jobs "
        "DROP COLUMN IF EXISTS post_status, "
        "DROP COLUMN IF EXISTS attempts, "
        "DROP COLUMN IF EXISTS last_error, "
        "DROP COLUMN IF EXISTS source"
    )
