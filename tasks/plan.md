# PLAN：rules_kb pgvector 混合检索升级

> 依据 SPEC.md。垂直切片（每任务一条可验证的完整路径），非按层横切。
> 每阶段末有 checkpoint，人工确认后再进下一阶段。

---

## 依赖图

```
T0 spike: DashScope embedding 经 litellm 实测  ──┐ (闸：不通则改方案)
                                                 ▼
T1 embed_text 重写 + config ─────────┐
                                     ├──► T3 灌库脚本 ──┐
T2 migration 0004 + models ──────────┘                 │
        │                                              ▼
        └──────────► T4 repository + service 混合检索 ◄─┘
                              │
                              ▼
                     T5 测试（DB 集成 + 离线契约恒绿）
```

关键依赖：
- **T0 是闸**：DashScope text-embedding-v3 经 `litellm.aembedding` + `dimensions=1024`
  不通，则 T1 改方案（直 httpx 打 DashScope /embeddings 或换模型），先问用户。
- T3 灌库需 T1（embed）+ T2（表）双就绪。
- T4 service 混合需 T1（query embed）+ T4 repository（向量 SQL）；jsonl 回退路径不依赖 DB。
- T5 离线契约测试只依赖 service 回退路径（T4 必须保住 session=None 行为）。

---

## 阶段与切片

### 阶段 A — 地基（T0–T2）

**T0｜spike：DashScope embedding 连通性验证**
- 一次性脚本（可丢弃 / 收进 scripts/）：`litellm.aembedding(model="openai/text-embedding-v3",
  api_base=dashscope_base_url, api_key=dashscope_api_key, input=["测试"], dimensions=1024)`。
- 验收：返回 1 个 1024 维 float 向量，无异常。
- 验证：打印 `len(resp.data[0]["embedding"]) == 1024`。
- 失败处置：记录错误，停下问用户（SPEC §8 风险 1）。**不绕过往下做。**

**T1｜embed_text 重写 + config**
- 改 `app/shared/llm/embeddings.py`：`embed_text(texts, model=...)` 走 `litellm.aembedding`
  → DashScope（同 gateway 模式：`openai/` 前缀 + api_base + api_key + dimensions）。
- 改 `app/core/config.py`：+`embedding_model="text-embedding-v3"`、`embedding_dim=1024`；
  rules_kb_path 注释更新（作回退源，不再标弃用）。
- 验收：`await embed_text(["a","b"])` 返回 2×1024 向量；knowledge_base 既有调用签名不破。
- 验证：`uv run python -c "import asyncio; from app.shared.llm.embeddings import embed_text; print(len(asyncio.run(embed_text(['x']))[0]))"` → 1024。

**T2｜migration 0004 + ORM models**
- `migrations/versions/0004_rules_kb.py`（down_revision="0003_sourcing"）：
  `CREATE EXTENSION IF NOT EXISTS vector`；`CREATE TABLE IF NOT EXISTS rules_kb(...)`
  （SPEC §2 列；**无 tenant_id、无 RLS、无 FORCE RLS**）；
  hnsw 索引 `(embedding vector_cosine_ops)` + btree `(platform)`。
  downgrade：`DROP TABLE IF EXISTS rules_kb CASCADE`。
- `app/domains/rules_kb/models.py`：`Base` + `RulesKbRow`（embedding `Vector(1024)`，
  jsonb 列用 `JSONB`，**不声明 tenant_id**）。
- 验收：`uv run alembic upgrade head` 成功；`\d rules_kb` 有 embedding vector(1024) + hnsw 索引；
  app 角色可 SELECT（db_bootstrap 默认授权）。
- 验证：`uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`（可逆）。

> **CHECKPOINT 1**：T0 通过（embedding 1024 维实测）+ 表建成可逆。人工确认 → 进阶段 B。

---

### 阶段 B — 灌库与检索（T3–T4）

**T3｜灌库脚本 scripts/load_rules_kb.py**
- 复用 service 的 `_load_corpus`/`_resolve_path` 读 `data/rules_kb/*_rules.jsonl`（524 条，按 rule_id 去重）。
- 每条 embed `title + "\n" + summary + "\n" + content`（截断到模型上限）；批量调 embed_text。
- 用 `database_admin_url`（psycopg 同步即可，仿 db_bootstrap）upsert：
  `INSERT ... ON CONFLICT (rule_id) DO UPDATE`（幂等，重跑不翻倍）。
- 日期字段 null 容错；数组字段（product_category/related_rule_ids/tags）转 jsonb。
- `--platform` 可选过滤；打印灌入条数。
- 验收：跑后 `SELECT count(*) FROM rules_kb` ≈ 去重后条数，embedding 非 null；重跑条数不变。
- 验证：`uv run python scripts/load_rules_kb.py` 两次，count 一致。

**T4｜repository + service 混合检索（核心垂直切片）**
- `app/domains/rules_kb/repository.py`：`RulesKbRepository.search_filtered(query_emb, platform, site)`
  → `SELECT <字段>, embedding <=> :q AS dist FROM rules_kb WHERE <platform/site 硬过滤>`
  （site：精确 OR GLOBAL，对齐现 jsonl 逻辑 service.py:138-140）。corpus 小，取回过滤全集。
- `app/domains/rules_kb/service.py`：
  - 抽共享 helper：`_apply_filters`（platform/site）、`_score`（bigram，复用）、`_rrf` 融合。
  - `search()` 分流：`self.session is None` → jsonl 回退（现逻辑，原样保留）；
    否则主路径：embed query → repository 取候选 → vector_rank + lexical_rank → RRF（k=60）
    → top-N → 投影 `_RETURN_FIELDS`。
  - **表空 / 异常 → 回退 jsonl**（不抛给上层，SPEC 优雅降级）。
- 验收：session=None 时行为与今日逐字节一致（离线契约不破）；session 有效时 DB 路径返回
  契约字段齐全、platform/site 隔离成立。
- 验证：`uv run pytest tests/test_rules_kb_search.py`（全绿，回退路径）。

> **CHECKPOINT 2**：灌库成功 + DB 路径手测命中 + 离线契约恒绿。人工确认 → 进阶段 C。

---

### 阶段 C — 验证（T5）

**T5｜DB 集成测试 tests/test_rules_kb_pgvector.py**
- fixture：连 DB（无 DB 环境 skip，对齐现有 DB 测试惯例）；灌小样本或依赖已灌库。
- 用例：
  1. 语义召回：换词 query（语料「封号」← 查「店铺被关停」）向量路径命中。
  2. platform 硬隔离在 SQL 路径成立（amazon 查询 0 串 ozon）。
  3. site GLOBAL 适配任意 site 查询。
  4. RRF：词法精确命中仍靠前。
  5. 表空 → 回退 jsonl 不抛错。
  6. `_RETURN_FIELDS` 契约齐全。
- 验收：新测试通过；离线契约测试仍全绿。
- 验证：`uv run pytest tests/test_rules_kb_search.py tests/test_rules_kb_pgvector.py`。

> **CHECKPOINT 3**：双测试套件绿 + SPEC §1 不变量逐条对账。人工确认 → 完成/提交。

---

## 回滚策略
- 代码：service `search()` 分流隔离，删 DB 分支即回纯 jsonl；embeddings 改动可单独 revert。
- DB：`alembic downgrade -1` 删 rules_kb 表（不影响 chat/kb/sourcing 其他域）。

## 非目标（本期不做）
- PG 中文分词扩展（zhparser）做真 BM25——用 Python bigram，足够。
- vector top-K 下推 SQL 的规模优化——corpus 小，内存融合；HNSW 索引已备，后续改 repository。
- 改 embedding 维度 / kb_chunks 表。
