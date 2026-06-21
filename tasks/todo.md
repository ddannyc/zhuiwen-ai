# TODO：rules_kb pgvector 混合检索

依赖：T0 → (T1, T2) → T3 → T4 → T5。CHECKPOINT 处停下人工确认。

## 阶段 A — 地基
- [x] **T0** spike：DashScope text-embedding-v3 经 `litellm.aembedding` 实测返回 1024 维 ✅（须 `litellm.drop_params=True`，dimensions 被丢→取 v3 默认 1024）
- [x] **T1** 重写 `embeddings.py` embed_text 走 litellm SDK→DashScope；config +embedding_model/embedding_dim ✅ commit 4a53821
- [x] **T2** migration `0004_rules_kb.py`（表+扩展+hnsw索引，无RLS）+ `models.py` RulesKbRow ✅（本 worktree .env 指向独立库 xborder_rkb）
- [ ] ⏸ **CHECKPOINT 1** — embedding 实测通 + 表可逆，人工确认

## 阶段 B — 灌库与检索
- [x] **T3** `scripts/load_rules_kb.py`：jsonl→embed→upsert(ON CONFLICT)，幂等 ✅ 灌入 524 条/6 平台，0 null（注：DashScope embedding 批上限 10）
- [x] **T4** `repository.py` search_filtered（向量+platform/site过滤）+ `service.py` 混合检索 RRF + jsonl 回退 ✅ 阈值 _VEC_DIST_MAX=0.55 校准
- [ ] ⏸ **CHECKPOINT 2** — 灌库成功 + DB 路径命中 + `test_rules_kb_search.py` 恒绿，人工确认

## 阶段 C — 验证
- [ ] **T5** `tests/test_rules_kb_pgvector.py`：语义召回/隔离/GLOBAL/RRF/表空回退/契约字段
- [ ] ⏸ **CHECKPOINT 3** — 双测试套件绿 + SPEC §1 不变量对账，人工确认

## 不变量守则（每任务自检）
- search() 签名 + _RETURN_FIELDS 不变
- platform/site 硬隔离 + GLOBAL 适配任意 site
- embedding 唯一经 litellm SDK→DashScope，1024 维
- rules_kb 全局表，无 tenant_id/无 RLS
- 空/无DB 回退 jsonl，离线契约恒绿
