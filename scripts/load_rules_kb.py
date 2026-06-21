"""灌库：data/rules_kb/*_rules.jsonl → embed → upsert 到 rules_kb 表。

幂等（ON CONFLICT (rule_id) DO UPDATE），重跑不翻倍。embedding 经 litellm SDK→
DashScope text-embedding-v3（1024 维），与 chat 同源。复用 RulesKbService 的语料加载
（多平台 *_rules.jsonl，按 rule_id 去重）。以 admin 连接写（全局共享表，无 RLS）。

用法：
  uv run python scripts/load_rules_kb.py                 # 全量
  uv run python scripts/load_rules_kb.py --platform amazon
  uv run python scripts/load_rules_kb.py --limit 5       # 调试少量
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app.core.config import get_settings
from app.domains.rules_kb.service import _load_corpus, _resolve_path
from app.shared.llm.embeddings import embed_text

# 嵌入输入：标题+摘要+正文拼接（截断防超模型上限）。改拼接策略需重灌全量。
_EMBED_CHAR_CAP = 2000
_ARRAY_FIELDS = ("product_category", "related_rule_ids", "tags")

_UPSERT = """
INSERT INTO rules_kb (
    rule_id, platform, site, original_language, rule_domain, rule_type,
    title, summary, content, severity, source_type, source_url, version,
    effective_date, expiry_date, last_verified_at, verification_status, confidence,
    product_category, related_rule_ids, tags, embedding
) VALUES (
    $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
    $14, $15, $16, $17, $18, $19::jsonb, $20::jsonb, $21::jsonb, $22::vector
)
ON CONFLICT (rule_id) DO UPDATE SET
    platform=EXCLUDED.platform, site=EXCLUDED.site,
    original_language=EXCLUDED.original_language, rule_domain=EXCLUDED.rule_domain,
    rule_type=EXCLUDED.rule_type, title=EXCLUDED.title, summary=EXCLUDED.summary,
    content=EXCLUDED.content, severity=EXCLUDED.severity, source_type=EXCLUDED.source_type,
    source_url=EXCLUDED.source_url, version=EXCLUDED.version,
    effective_date=EXCLUDED.effective_date, expiry_date=EXCLUDED.expiry_date,
    last_verified_at=EXCLUDED.last_verified_at, verification_status=EXCLUDED.verification_status,
    confidence=EXCLUDED.confidence, product_category=EXCLUDED.product_category,
    related_rule_ids=EXCLUDED.related_rule_ids, tags=EXCLUDED.tags, embedding=EXCLUDED.embedding
"""


def _embed_input(row: dict) -> str:
    parts = [row.get("title") or "", row.get("summary") or "", row.get("content") or ""]
    return "\n".join(p for p in parts if p)[:_EMBED_CHAR_CAP]


def _vec_literal(emb: list[float]) -> str:
    # pgvector 文本字面量：'[1.0,2.0,...]'，配 ::vector 转换。
    return "[" + ",".join(repr(float(x)) for x in emb) + "]"


def _admin_dsn() -> str:
    return get_settings().database_admin_url.replace("+asyncpg", "")


def _params(row: dict, emb: list[float]) -> list:
    def arr(f):
        v = row.get(f)
        return json.dumps(v if isinstance(v, list) else [])
    return [
        row["rule_id"], row.get("platform"), row.get("site"),
        row.get("original_language"), row.get("rule_domain"), row.get("rule_type"),
        row.get("title"), row.get("summary"), row.get("content"), row.get("severity"),
        row.get("source_type"), row.get("source_url"), row.get("version"),
        row.get("effective_date"), row.get("expiry_date"), row.get("last_verified_at"),
        row.get("verification_status"), row.get("confidence"),
        arr("product_category"), arr("related_rule_ids"), arr("tags"),
        _vec_literal(emb),
    ]


async def load(platform: str | None = None, limit: int | None = None, batch: int = 10) -> int:
    rows = _load_corpus(_resolve_path(get_settings().rules_kb_path))
    rows = [r for r in rows if r.get("rule_id")]  # 须有主键
    if platform:
        pf = platform.strip().lower()
        rows = [r for r in rows if str(r.get("platform", "")).lower() == pf]
    if limit:
        rows = rows[:limit]
    if not rows:
        return 0

    batch = min(batch, 10)  # DashScope embedding 单批上限 10
    conn = await asyncpg.connect(_admin_dsn())
    n = 0
    try:
        for i in range(0, len(rows), batch):
            chunk = rows[i : i + batch]
            embs = await embed_text([_embed_input(r) for r in chunk])
            for r, e in zip(chunk, embs):
                await conn.execute(_UPSERT, *_params(r, e))
                n += 1
    finally:
        await conn.close()
    return n


async def _main() -> None:
    ap = argparse.ArgumentParser(description="灌 rules_kb 向量库")
    ap.add_argument("--platform", help="只灌指定平台")
    ap.add_argument("--limit", type=int, help="只灌前 N 条（调试）")
    ap.add_argument("--batch", type=int, default=10, help="embedding 批大小（DashScope 上限 10）")
    args = ap.parse_args()
    n = await load(platform=args.platform, limit=args.limit, batch=args.batch)
    print(f"✓ 灌入/更新 {n} 条规则到 rules_kb")


if __name__ == "__main__":
    asyncio.run(_main())
