# SPEC：rules_kb 检索升级为 pgvector 向量+词法混合

> 规格驱动开发文档。把 chat agent `rules_search` 工具背后的 `RulesKbService`
> 从「jsonl 种子语料 + 中文 bigram 词法打分」升级为「Postgres + pgvector 向量检索
> 与词法打分混合（RRF 融合）」。契约不变，只换实现（service.py:9 的承诺兑现）。

---

## 1. 目标（Objective）

**做什么**：给平台合规规则库（rules_kb 域）加向量语义检索，与现有中文 bigram 词法
打分**混合**（Reciprocal Rank Fusion），提升召回——语义近但用词不同的规则也能命中，
同时保留词法对专有名词/编号的精确匹配。

**为什么**：当前纯 bigram 词法，换种说法就漏召回；合规问答漏召回 = 模型自由编造 =
项目反幻觉红线被击穿。

**面向谁**：chat agent（唯一调用方，经 `rules_search` 工具）。终端用户是跨境电商卖家。

**不变量（北极星）**：
- `RulesKbService.search()` 签名与返回 `_RETURN_FIELDS` 契约**零改动**。
- platform/site **硬隔离**绝不松动（查 amazon 绝不串 ozon；GLOBAL site 适配任意 site 查询）。
- 空检索 → 返回 `[]`，上层确定性兜底「未找到」，绝不编造。
- LLM/embedding **唯一出口**：litellm SDK 进程内 → DashScope，不直连 provider SDK。

---

## 2. 架构与数据流

```
chat agent (rules_search 工具)
  └─ RulesKbService.search(query, platform, site, limit)
       ├─ session 为 None / 表空 → 【回退】jsonl 词法路径（现逻辑，离线可跑）
       └─ session 有效 → 【主路径】混合检索
            ├─ embed_text([query]) ──litellm SDK──> DashScope text-embedding-v3 (1024d)
            ├─ RulesKbRepository.search_filtered(platform, site)
            │     SQL: SELECT ..., embedding <=> :q AS dist
            │          FROM rules_kb WHERE <platform/site 硬过滤>
            │     （corpus 极小，取回过滤后全集；附 cosine 距离）
            ├─ Python 融合：
            │     vector_rank  = 按 dist 升序
            │     lexical_rank = 按现有 _score(bigram) 降序
            │     RRF: score = Σ 1/(k + rank)，k=60
            └─ 取 top-N，投影 _RETURN_FIELDS
```

**表设计 `rules_kb`（全局共享，无 tenant_id / 无 RLS）**

区别于 `kb_chunks`（租户私有，套 RLS）：平台规则跨租户通用，所有租户共读同一份。

| 列 | 类型 | 来源 |
|---|---|---|
| rule_id | uuid PK | jsonl |
| platform, site, original_language, rule_domain, rule_type | text | jsonl |
| title, summary, content | text | jsonl |
| severity, source_type, source_url, version | text | jsonl |
| effective_date, expiry_date, last_verified_at | date (null 容许) | jsonl |
| verification_status, confidence | text | jsonl |
| product_category, related_rule_ids, tags | jsonb | jsonl 数组 |
| embedding | vector(1024) | embed(title + summary + content 截断) |
| created_at | timestamptz default now() | — |

索引：`USING hnsw (embedding vector_cosine_ops)` + `(platform)` btree。

**embedding 来源统一**：重写 `app/shared/llm/embeddings.py` 的 `embed_text`，
从废弃的 httpx 直连 litellm 代理（localhost:4000，config 标「本期未启用」）改为
`litellm.aembedding(model="openai/text-embedding-v3", api_base=dashscope_base_url,
api_key=dashscope_api_key, dimensions=1024)`——与 `gateway.chat` 同模式同源。
`knowledge_base` 域复用同一 `embed_text`，自动受益。

---

## 3. 命令（Commands）

```bash
# 迁移（建 rules_kb 表 + 扩展 + 索引）
uv run alembic upgrade head

# 灌库：jsonl → embed → upsert（幂等，按 rule_id ON CONFLICT DO UPDATE）
uv run python scripts/load_rules_kb.py            # 全量
uv run python scripts/load_rules_kb.py --platform amazon   # 单平台（可选）

# 测试
uv run pytest tests/test_rules_kb_search.py       # 离线契约（jsonl 回退路径）
uv run pytest tests/test_rules_kb_pgvector.py     # DB 集成（需 Postgres+pgvector）

# 跑服务
uv run uvicorn app.main:app --reload
```

---

## 4. 项目结构（改动清单）

```
新增：
  migrations/versions/0004_rules_kb.py        # 建 rules_kb 表 + vector 扩展 + hnsw 索引
  app/domains/rules_kb/models.py              # ORM：RulesKbRow（无 tenant_id/RLS）
  app/domains/rules_kb/repository.py          # search_filtered() 向量+过滤 SQL
  scripts/load_rules_kb.py                    # jsonl → embed → upsert 灌库
  tests/test_rules_kb_pgvector.py             # DB 集成测试（混合检索/隔离）

改：
  app/domains/rules_kb/service.py             # search() 内分流：主走 pgvector 混合，
                                              #   session=None/表空回退 jsonl；
                                              #   抽出共享 helper（_score/_apply_filters/RRF）
  app/shared/llm/embeddings.py                # embed_text 改走 litellm SDK→DashScope
  app/core/config.py                          # +embedding_model/embedding_dim；
                                              #   rules_kb_path 注释更新（不再标弃用，作回退源）
  pyproject.toml                              # pgvector pin 收紧（如需）
```

---

## 5. 代码风格（Code Style）

- 跟随现仓：domain 分层 service/repository/models，跨域只调对方 service。
- repository **不写** `WHERE tenant_id`——rules_kb 全局表无 RLS，但 platform/site
  过滤**必须**显式写进 SQL（这是业务隔离，非 RLS）。
- 中文注释，解释「为什么」而非「做什么」，与现有文件密度一致。
- 类型注解齐全；async 全程；`list[dict[str, Any]]` 等现有风格。
- 不引新依赖（pgvector/litellm/sqlalchemy 已在）。RRF 纯 Python 实现，~10 行。

---

## 6. 测试策略（Testing Strategy）

- **离线契约（不动现有文件语义）**：`test_rules_kb_search.py` 继续用 `session=None`
  跑 jsonl 回退路径，全部 9 条断言保持绿（metadata 隔离 / 溯源字段 / 空检索 /
  GLOBAL site / limit）。这是回退路径的回归网。
- **DB 集成（新增）**：`test_rules_kb_pgvector.py` 真连 Postgres，先灌小样本，验证：
  - 语义召回：换词查询（如「店铺被关」vs 语料「封号」）向量能命中、纯词法漏。
  - platform/site 硬隔离在 SQL 路径同样成立（amazon 查询绝不返 ozon）。
  - RRF：词法精确命中仍排前；融合不破坏 _RETURN_FIELDS 契约。
  - 表空 → 回退 jsonl，不抛错。
  - 无 DB 环境用 pytest marker / fixture skip（对齐现有 DB 测试惯例）。
- **embeddings**：embed_text 改造后，knowledge_base 既有路径不回归（若有相关测试）。

---

## 7. 边界（Boundaries）

**永远（Always）**
- 保 `search()` 签名 + `_RETURN_FIELDS` 投影不变（只暴露这些，不泄 content 全文）。
- platform/site 硬隔离 + GLOBAL site 适配任意 site 查询。
- embedding 唯一经 litellm SDK→DashScope；维度对齐 1024（与 kb_chunks 一致）。
- rules_kb 为全局共享表，无 tenant_id、无 RLS。
- 空/无 DB 优雅回退 jsonl，离线契约测试恒绿。

**先问（Ask first）**
- 改 embedding 维度（连带影响 kb_chunks 表与 0002 迁移）。
- 删除 jsonl 回退路径或 `rules_kb_path` 配置。
- 引入 PG 中文分词扩展（zhparser/pg_bigm）做真 BM25（当前用 Python bigram，足够）。
- 任何触及前端 chat 契约（contract.ts ChatAction）的改动。

**绝不（Never）**
- rules_kb 改成租户私有 / 套 RLS。
- 绕过 gateway/embeddings 直连 provider SDK。
- 让 rules_search 在检索失败时自由生成规则（反幻觉红线）。
- 把 content 全文/内部字段透给上层或前端。

---

## 8. 已知风险

- **DashScope text-embedding-v3 经 litellm**：需实测 `litellm.aembedding` 对 DashScope
  兼容端点 + `dimensions=1024` 的支持；若不通，回退方案见「先问」。
- **混合融合质量**：corpus 仅 524 条，全集载入 Python 融合零压力；规模增长后需把
  vector top-K 下推 SQL（HNSW 索引已建，改 repository 即可，service 不动）。
- **灌库一致性**：embedding 用 title+summary+content 拼接；改拼接策略需重灌全量。
