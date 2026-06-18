"""0002 knowledge_base 域：pgvector + kb 表 + RLS

需要 Postgres 装了 pgvector 扩展。未装则本迁移失败（chat 域在 0001 已可独立运行）。
安装（MacPorts 示例）：sudo port install pg-vector && 在库里 CREATE EXTENSION vector。

Revision ID: 0002_kb
"""
from alembic import op

revision = "0002_kb"
down_revision = "0001_chat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("""
        CREATE TABLE IF NOT EXISTS kb_documents (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   uuid NOT NULL,
            title       varchar(512) NOT NULL,
            source_uri  varchar(1024) NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS kb_chunks (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    uuid NOT NULL,
            document_id  uuid NOT NULL,
            content      text NOT NULL,
            embedding    vector(1024)
        )
    """)

    for t in ("kb_documents", "kb_chunks"):
        op.execute(f"ALTER TABLE {t} ALTER COLUMN tenant_id "
                   f"SET DEFAULT current_setting('app.current_tenant')::uuid")
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {t}
                USING (tenant_id = current_setting('app.current_tenant')::uuid)
                WITH CHECK (tenant_id = current_setting('app.current_tenant')::uuid)
        """)

    op.execute("CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx "
               "ON kb_chunks USING hnsw (embedding vector_cosine_ops)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS kb_chunks CASCADE")
    op.execute("DROP TABLE IF EXISTS kb_documents CASCADE")
