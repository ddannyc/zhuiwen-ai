-- 首次启动初始化（仅数据目录为空时由官方镜像 entrypoint 以 postgres 身份连 xborder 执行）。
-- 职责：装扩展 + 造业务角色 app + 配默认权限。建表/RLS 策略不在这里 —— 交给 alembic 迁移。

-- pgvector：knowledge_base 向量列用。gen_random_uuid 为 PG13+ 内置，无需 pgcrypto。
CREATE EXTENSION IF NOT EXISTS vector;

-- 业务角色：非超管、非 BYPASSRLS，故受 RLS 约束（这是租户隔离生效的前提）。
-- 超管(postgres)会绕过 RLS，所以业务连接绝不能用超管；迁移才用超管。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app') THEN
    CREATE ROLE app LOGIN PASSWORD 'app';
  END IF;
END
$$;

-- app 能连库、用 public schema，但不建表（建表是迁移的事，由 postgres 做）。
GRANT CONNECT ON DATABASE xborder TO app;
GRANT USAGE ON SCHEMA public TO app;

-- 关键：迁移（postgres 身份）将来建的表/序列，自动授 DML 给 app。
-- 否则 app 连不到迁移后才出现的 conversations/messages/collect_jobs/kb_* 表。
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO app;

-- 覆盖已存在对象（本脚本先于迁移跑，通常为空；保险起见仍授一次）。
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app;
