"""0003 sourcing 域：collect_jobs 采集任务表 + 租户隔离 RLS

替代旧 zhuiwen_web.py 的内存 _COLLECT_JOBS 队列：持久化 + 多租户隔离。
任务由 chat 的 collect_products 工具下发（经 Temporal CollectWorkflow 编排，
不可达时降级直接写本表 pending 行）；浏览器采集插件经 /sourcing/jobs/poll
认领、/sourcing/jobs/{id}/done 回结果。tenant_id 由 RLS 默认值填充，
应用层不手填、不手查（与 conversations/messages 同套路，见 0001）。

Revision ID: 0003_sourcing
"""
from alembic import op

revision = "0003_sourcing"
down_revision = "0002_kb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS collect_jobs (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   uuid NOT NULL,
            status      varchar(32) NOT NULL DEFAULT 'pending',
            keywords    jsonb NOT NULL DEFAULT '[]'::jsonb,
            per_kw      integer NOT NULL DEFAULT 10,
            market      varchar(64),
            result      jsonb,
            error       text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("ALTER TABLE collect_jobs ALTER COLUMN tenant_id "
               "SET DEFAULT current_setting('app.current_tenant')::uuid")
    op.execute("ALTER TABLE collect_jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE collect_jobs FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON collect_jobs
            USING (tenant_id = current_setting('app.current_tenant')::uuid)
            WITH CHECK (tenant_id = current_setting('app.current_tenant')::uuid)
    """)

    # poll 端点按 status+created_at 取最早的 pending 任务（FIFO）。
    op.execute("CREATE INDEX IF NOT EXISTS collect_jobs_status_idx "
               "ON collect_jobs (status, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS collect_jobs CASCADE")
