-- 多租户隔离的真正落地点：数据库层的 Row-Level Security。
-- 这一步用 admin 连接（database_admin_url）执行，而不是业务连接。
--
-- 关键思想：业务连接的角色（app）受 RLS 约束，只能看到
-- 当前会话变量 app.current_tenant 对应的行。即使应用代码漏写过滤条件，
-- 数据库也兜底，不会串租户。

-- 0. 启用 pgvector
CREATE EXTENSION IF NOT EXISTS vector;

-- 1. 让每张多租户表的 tenant_id 默认取会话变量。
--    这样应用 INSERT 时无需手动填 tenant_id。
ALTER TABLE kb_documents
    ALTER COLUMN tenant_id SET DEFAULT current_setting('app.current_tenant')::uuid;
ALTER TABLE kb_chunks
    ALTER COLUMN tenant_id SET DEFAULT current_setting('app.current_tenant')::uuid;

-- 2. 开启 RLS
ALTER TABLE kb_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_chunks    ENABLE ROW LEVEL SECURITY;

-- 强制连表主也受约束（防止表 owner 绕过）
ALTER TABLE kb_documents FORCE ROW LEVEL SECURITY;
ALTER TABLE kb_chunks    FORCE ROW LEVEL SECURITY;

-- 3. 策略：只能操作 tenant_id 等于当前会话变量的行
CREATE POLICY tenant_isolation ON kb_documents
    USING (tenant_id = current_setting('app.current_tenant')::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant')::uuid);

CREATE POLICY tenant_isolation ON kb_chunks
    USING (tenant_id = current_setting('app.current_tenant')::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant')::uuid);

-- 4. 向量索引（HNSW，cosine）
CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx
    ON kb_chunks USING hnsw (embedding vector_cosine_ops);

-- 提示：迁移/建表请用 admin 连接；运行期业务务必用受 RLS 约束的 app 角色，
-- 否则隔离形同虚设。
