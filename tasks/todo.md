# TODO：sourcing 客户端化 + 去 Temporal

> 配套 `tasks/plan.md`。逐阶段勾，检查点(C*)是阶段闸门——不过不进下一阶段。

## Phase 0 — 去风险闸门 🚪
- [ ] **T0** 妙手 `url` fetch 风控实测（真实账号小批量）→ 记录成功率
  - 闸门：失败则**暂停全计划**，方案重议

## Phase 1 — 队列地基
- [x] T1.1 `app/shared/queue/` procrastinate App(PsycopgConnector) + `tenant_session`
- [x] T1.2 迁移 `0004_procrastinate`：注入 procrastinate schema（alembic up/down）
- [x] T1.3 trivial `ping` task：defer→worker→RLS 隔离验证
- [x] T1.A（并行）`sourcing/miaoshou.py`：url_fetch/box/detail/edit/delete/shops/tkcall 封装
      （tk_list_items 认领→上架编排留 T3.2，用这些原语拼）
- [x] **✅ C1**：procrastinate worker 起；trivial task defer→执行→RLS 过；alembic up/down 净

## Phase 2 — ingest 垂直切片 ★MVP
- [x] T2.1 迁移 `0005`：collect_jobs 加 post_status/attempts/last_error/source；弃 poll 语义
- [x] T2.2 `POST /sourcing/ingest`（收 urls）+ IngestRequest 校验 + 存批 + defer
      （删旧 /jobs/poll+/done 推迟到 Phase5：现 e2e/sourcing/workflow 测试仍依赖，T5.3 一并清）
- [x] T2.3 `tasks.post_process`：妙手 fetch + 评分 + 违禁词清洗 + top_n → result
- [x] T2.4 `GET /sourcing/jobs/{batch_id}` 返 post_status/result/scores
- [x] **✅ C2**：ingest→存→入队→worker妙手fetch+评分→done+scores；跨租户隔离（mock 妙手/LLM）
      ← 第一个可演示里程碑（真实跑通待 T0 妙手实测 + 真 DASHSCOPE_API_KEY）

## Phase 3 — 后处理深化
- [x] T3.1 翻译 + 图片质检（编排 + miaoshou.edit/delete 回写，options 开关）
      ⚠ 翻译/质检钩子默认 passthrough：zhuiwen_studio 外部模块未移植，真实接入另列
- [x] T3.2 上架 `publish_to_tiktok`（box-id 驱动：认领→认领店铺→预填→可选发布）
      精简：略类目属性补全/用量统计；AI 选类目作钩子(默认跳过)，真实联调再补
- [x] **✅ C3**：全管线 fetch→评分→翻译→上架按 options 跑通，各段 mock 断言调用链

## Phase 4 — 可靠性
- [x] T4.1 cron 兜底 task：扫 pending/queued 超 grace(120s) 重投，cron 每分钟
      （跨租户读用 admin 连接 bypass RLS；periodic 任务随 procrastinate worker 起，T5.1 后生效）
- [x] T4.2 幂等：CAS pending/queued→running（独立提交）；done 跳过；cron 回收崩溃 running
      ⚠ 崩在 publish 中途的 exactly-once 未保证（妙手 tkcall 非事务，需妙手侧幂等键，另列）
- [x] **✅ C4**：双 defer→CAS 只一个跑；崩溃 running 超 grace 回收重投；正在跑的不误回收

## Phase 5 — 去 Temporal
- [x] T5.1 `workers/main.py` 改 procrastinate `run_worker_async`（含 cron periodic 生效）
- [x] T5.2 删残留：workflows/activities、config temporal_*、compose temporal、pyproject temporalio、
      test_sourcing_workflow.py、README；service start_collect/complete_job 改降级直写（保 chat poll/done UX）
- [x] T5.3 测试改写：删 2 个 temporal-mode 单测 + force_degraded fixture（degraded 现为唯一路径）
- [x] **✅ C5**：pytest 全绿（87）；temporalio 不可导入；compose 仅 db；app 无 temporal 引用

## Phase 6 — 扩展端 client/ ❌ 砍掉（用现有 1688采集助手插件 + poll/done）
- [x] 决策：不另建 MV3 扩展；客户端采集走 chat 关键词→pending→插件 poll/done
- [x] 桥接 done→post_process：插件 /done 回结果后自动 defer post_process（评分/翻译/上架）；
      post_process 兼容 URL(妙手 fetch)/items(直接评分) 两种入口；_defer 提取为 service 复用

## Phase 7 — 测试硬化
- [x] T7.1 补真实缺口：评分异常 LLM(非数组/部分)、loose_json 鲁棒性、publish 失败分支
      (prefill_fail/info_fail/publish_fail/claim_fail)。扩展测试 N/A（Phase6 砍）
- [x] **✅ C7**：py 全绿（95 测试）；关键路径 + 失败分支均有覆盖

---

## 待决（执行中定，不阻塞）
- [ ] ADR-001：grace/cron 周期最终值；procrastinate schema 落 alembic 方式
- [ ] B：扩展分发渠道 + JWT 注入方式（Phase6 前定）
- [ ] D：publishing `BulkPublishWorkflow` 删 Temporal 后归宿
- [ ] 列表级字段预筛（优化项）

## 执行顺序
T0 → [Phase1 ∥ T1.A] → Phase2(C2) → Phase3 → Phase4 → Phase5 → Phase6 → Phase7
