"""0004 rules_kb 域：全局共享平台规则库（pgvector 向量检索，无 RLS）

区别于 kb_chunks（0002，租户私有，套 RLS）：平台合规规则跨租户通用，所有租户共读
同一份语料，故本表**无 tenant_id、无 RLS**。chat agent 的 rules_search 工具经
RulesKbService 检索本表（向量 + 词法混合）。语料由 scripts/load_rules_kb.py 从
data/rules_kb/*_rules.jsonl 灌入（embed 经 litellm SDK→DashScope，1024 维）。

日期列（effective_date/expiry_date/last_verified_at）用 text 存：jsonl 里是字符串/null，
text 无损往返，且 _RETURN_FIELDS 契约要求 last_verified_at 原样回字符串（无日期运算需求）。

授权：迁移以 admin 跑，新表由 db_bootstrap 的 ALTER DEFAULT PRIVILEGES 自动授 app
（与 0002/0003 同套路，不在此显式 GRANT）。

Revision ID: 0004_rules_kb
"""
from alembic import op

revision = "0004_rules_kb"
down_revision = "0003_sourcing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("""
        CREATE TABLE IF NOT EXISTS rules_kb (
            rule_id             uuid PRIMARY KEY,
            platform            text NOT NULL,
            site                text,
            original_language   text,
            rule_domain         text,
            rule_type           text,
            title               text,
            summary             text,
            content             text,
            severity            text,
            source_type         text,
            source_url          text,
            version             text,
            effective_date      text,
            expiry_date         text,
            last_verified_at    text,
            verification_status text,
            confidence          text,
            product_category    jsonb NOT NULL DEFAULT '[]'::jsonb,
            related_rule_ids    jsonb NOT NULL DEFAULT '[]'::jsonb,
            tags                jsonb NOT NULL DEFAULT '[]'::jsonb,
            embedding           vector(1024),
            created_at          timestamptz NOT NULL DEFAULT now()
        )
    """)
    # 向量相似度索引（cosine，对齐 service 检索用的 cosine_distance）。
    op.execute("CREATE INDEX IF NOT EXISTS rules_kb_embedding_idx "
               "ON rules_kb USING hnsw (embedding vector_cosine_ops)")
    # platform 硬过滤高频，建 btree 加速。
    op.execute("CREATE INDEX IF NOT EXISTS rules_kb_platform_idx ON rules_kb (platform)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rules_kb CASCADE")
