"""0001 基线：chat 域 conversations / messages + 租户隔离 RLS

多租户隔离命脉：DB 层 Row-Level Security，用 admin 连接执行。
业务连接的 app 角色受 RLS 约束，只能看到 app.current_tenant 对应的行。
会话按 user_id 归属、按 tenant_id（RLS）隔离；action 列存结构化 ChatAction。

设为基线（无 pgvector 依赖），让 chat 全栈在未装 pgvector 的库上也能跑。
knowledge_base（需 pgvector）放在 0002。

Revision ID: 0001_chat
"""
from alembic import op

revision = "0001_chat"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   uuid NOT NULL,
            user_id     uuid NOT NULL,
            title       varchar(512) NOT NULL DEFAULT '新对话',
            created_at  timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        uuid NOT NULL,
            conversation_id  uuid NOT NULL,
            role             varchar(32) NOT NULL,
            content          text NOT NULL,
            action           jsonb,
            created_at       timestamptz NOT NULL DEFAULT now()
        )
    """)

    for t in ("conversations", "messages"):
        op.execute(f"ALTER TABLE {t} ALTER COLUMN tenant_id "
                   f"SET DEFAULT current_setting('app.current_tenant')::uuid")
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {t}
                USING (tenant_id = current_setting('app.current_tenant')::uuid)
                WITH CHECK (tenant_id = current_setting('app.current_tenant')::uuid)
        """)

    op.execute("CREATE INDEX IF NOT EXISTS conversations_user_idx "
               "ON conversations (user_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS messages_conv_idx "
               "ON messages (conversation_id, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS messages CASCADE")
    op.execute("DROP TABLE IF EXISTS conversations CASCADE")
