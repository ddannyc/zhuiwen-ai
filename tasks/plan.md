# 实施计划：sourcing 客户端化 + 去 Temporal（procrastinate）

> 源：`SPEC.md` + `docs/sourcing-client-migration.md`。本计划按**垂直切片**（每任务一条完整可验路径）+ **阶段检查点**组织。
> 读取范围已核：sourcing 域、`tmp/zhuiwen_web.py` 妙手调用、procrastinate（context7）、config/deps。
> 状态：待人工评审。批准后逐阶段执行，每阶段检查点过了才进下一阶段。

---

## 0. 关键事实（计划前提）

- procsastinate 无 asyncpg/async-SQLAlchemy 连接器 → **不做单事务原子 defer**，用 `post_status` outbox + cron 兜底（ADR-001）。
- 妙手不吃外部商品数据、按 box-id 上架、自抓 1688 → **扩展只回传 offer URL**（ADR-002）。
- `psycopg[binary]>=3.3.4` 已在依赖 → procrastinate `PsycopgConnector`(psycopg3) 即用，无新驱动。
- 删除目标：`temporalio` 依赖、`temporal_*` config、`sourcing/workflows.py`+`activities.py` 的 Temporal 部分、compose `temporal` 服务、`tests/test_sourcing_workflow.py`。

---

## 1. 依赖图（组件级）

```
                 ┌─────────────────────────────────────────┐
   T0 妙手fetch实测(GATE) ── 决定方案是否成立                 │
                 └─────────────────────────────────────────┘
                                │ pass
   ┌──────────────┐   ┌──────────────────┐
   │ Phase1 队列地基 │   │ T1 妙手client封装  │   (二者可并行)
   │ procrastinate  │   │ url/edit/delete/  │
   │ +tenant_session│   │ tk_list_items     │
   │ +trivial task  │   └────────┬─────────┘
   └───────┬────────┘            │
           │   ┌─────────────────┘
           ▼   ▼
   ┌──────────────────────────────────┐
   │ Phase2 ingest 垂直切片             │  migration0004 → /ingest(urls) →
   │ URL→存库→入队→妙手fetch→评分→存result │  post_process(fetch+score) → GET 状态
   └───────┬──────────────────────────┘
           ▼
   ┌──────────────┐   ┌──────────────────┐   ┌─────────────────┐
   │ Phase3 后处理深 │   │ Phase4 可靠性      │   │ Phase5 去Temporal │
   │ 翻译/质检/上架  │   │ outbox cron+幂等   │   │ worker改/删编排    │
   └───────┬──────┘   └────────┬─────────┘   └────────┬────────┘
           └──────────┬─────────┴──────────────────────┘
                      ▼
              ┌──────────────┐      ┌──────────────┐
              │ Phase6 扩展端  │      │ Phase7 测试硬化 │
              │ client/ URL采集 │      │ 全套           │
              └──────────────┘      └──────────────┘
```

切片纪律：**每任务跑通一条端到端路径**（非按层堆）。如 Phase2 一次打通「URL 进→结果出」，而非先写完所有 model 再写所有 endpoint。

---

## 2. 阶段与任务

### Phase 0 — 去风险闸门（GATE）

**T0 妙手 `url` fetch 风控实测**
- 目标：真实账号小批量验证 `SELECT_CMD --mode url --urls <真实1688offer>` 能成功 fetch 详情。
- 验收：≥1 批真实 offer URL 返回非空 cands JSON；记录成功率。
- 验证：手跑妙手 CLI（需妙手凭证 + select.py 环境）。
- **闸门**：若妙手自抓也撞风控/失败 → 暂停，整方案重议（妙手吃不了外部数据，上架链断）。**不过不进 Phase1+。**
- 依赖：无。阻断全局。

---

### Phase 1 — 队列地基（垂直：defer→worker→RLS 写库）

**T1.1 procrastinate 接入 + queue app**
- 目标：`app/shared/queue/` 建 procrastinate `App`(PsycopgConnector) + `tenant_session(tenant_id)` 包装（开会话 + `SET app.current_tenant` + RLS）。
- 文件：`app/shared/queue/__init__.py`、`app.py`、`tenant.py`；`pyproject.toml` 加 `procrastinate`。
- 验收：`import` 通；app.open_async() 连库成功。

**T1.2 procrastinate schema 迁移**
- 目标：procrastinate 自有表入库。新迁移 `0004_procrastinate.py` 调 `SchemaManager.get_schema()` 注入（与 alembic 链统一）。
- 验收：`alembic upgrade head` 建出 procrastinate_jobs 等表；`alembic downgrade -1` 可回退。

**T1.3 trivial task 垂直验证**
- 目标：定义临时 `ping` task（写一行到某租户表），defer → worker 执行 → RLS 隔离正确。
- 文件：临时 task + worker 入口雏形。
- 验收：`defer_async(tenant_id=A)` → worker 跑 → 行落 A 租户，B 租户查不到。

**✅ 检查点 C1**：`uv run python -m app.workers.main`（procrastinate worker）跑起；trivial task defer→执行→RLS 隔离过；`alembic up/down` 干净。删 trivial task 前留一条 e2e 证据。

**并行可做：T1.A 妙手 client 封装**
- 目标：把 zhuiwen_web 的妙手调用移植为服务端 client：`miaoshou.url_fetch(urls)`、`edit(id, ch)`、`delete(ids)`、`tk_list_items(ids, shop)`、`shops()`。封 `SELECT_CMD` + `_run` 超时。
- 文件：`app/domains/sourcing/miaoshou.py`。
- 验收：单测用 fake `SELECT_CMD`（echo 固定 JSON）→ client 解析正确；超时/非零退出 → 结构化错误。

---

### Phase 2 — ingest 垂直切片（URL→存库→入队→妙手fetch→评分→结果）★核心

**T2.1 迁移 0004→0005：collect_jobs 转 batch 语义**
- 目标：加列 `post_status/attempts/last_error/source`；旧 `status` poll 语义弃用。保 RLS。
- 验收：迁移 up/down 干净；现有 sourcing e2e 不炸（或同步改）。

**T2.2 `/sourcing/ingest` 端点（收 urls）**
- 目标：`IngestRequest{market, urls[], options}` → 校验（1688 offer URL、去重、≤200）→ 存批 `post_status='pending'`（asyncpg/RLS）→ 提交后 `defer_async(post_process, batch_id, tenant_id)` 置 `queued`。
- 文件：`router.py` + `schemas.py` + `service.py`。删旧 `/jobs/poll`+`/done`。
- 验收：curl 带 JWT POST urls → 201 + batch_id + post_status；非 1688 URL → 422；空 → 422。

**T2.3 `post_process` task：妙手 fetch + 评分**
- 目标：task 取 batch → `tenant_session` 设租户 → `miaoshou.url_fetch(urls)` → `_score_candidates`（移植）+ 违禁词清洗 + top_n → 存 `result`(cands+scores) → `post_status='done'`。失败 `attempts++`/`last_error`/`failed`。
- 文件：`app/domains/sourcing/tasks.py` + `ingest.py`(评分/清洗纯逻辑)。
- 验收：mock 妙手 client → defer → worker 跑 → batch done + scores 落库 + RLS 正确。

**T2.4 GET 状态对齐**
- 目标：`GET /sourcing/jobs/{batch_id}` 返回 `post_status/result/scores`。
- 验收：轮询见 pending→queued→running→done。

**✅ 检查点 C2**（端到端，无扩展）：真实 JWT + 真实 1688 offer URL，curl `/ingest` → worker 妙手 fetch + 评分 → `GET` 见 `done` + scores。跨租户隔离过。**这是 MVP 闭环。**

---

### Phase 3 — 后处理深化（翻译/质检/上架）

**T3.1 翻译 + 图片质检段**
- 目标：options.translate → `studio.translate_title/_images` + `miaoshou.edit` 回写 box；optimize → `pick_good_images`。
- 验收：开 translate → box 条目标题/图被改写（mock studio 断言调用）。

**T3.2 上架段 `tk_list_items`**
- 目标：options.list_tiktok → 取达标 box-id → `miaoshou.tk_list_items(ids, shop)`（claimed→认领→选类目→可选发布 tk_auto）。
- 验收：mock 妙手 → 上架按 box-id 调用；无绑定店铺 → 结构化错误；status≠success 的条目跳过（对齐旧逻辑）。

**✅ 检查点 C3**：全管线（fetch→评分→翻译→上架）按 options 开关跑通；各段 mock 妙手/studio 断言调用链。

---

### Phase 4 — 可靠性（outbox cron + 幂等）

**T4.1 cron 兜底 task**
- 目标：procrastinate periodic task，扫 `post_status='pending' AND updated_at<now()-grace` → 重 `defer`。grace=2min、cron=1min（ADR-001 待确认值）。
- 验收：造「pending 超 grace」批 → cron 扫到重投 → done。

**T4.2 幂等**
- 目标：`post_process` 进入 CAS `pending/queued→running`；已 `done` 跳过；上架前查重避免重复 list。
- 验收：对同 batch 连 defer 两次 → 只执行一次上架（断言妙手 list 调一次）。

**✅ 检查点 C4**（崩溃恢复）：worker 跑 task 中途 kill → batch 留 pending/running → cron 重驱 → 最终 done **且只上架一次**。

---

### Phase 5 — 去 Temporal

**T5.1 worker 入口改 procrastinate**
- 目标：`app/workers/main.py` 删 Temporal Worker → `await queue_app.run_worker_async()`（含 cron）。
- 验收：worker 起、跑 task、跑 cron；Temporal 不再被 import。

**T5.2 删 Temporal 残留**
- 目标：删 `sourcing/workflows.py`+`activities.py` 的 Temporal 部分（保留被 task 复用的纯逻辑已移 `ingest.py`/`miaoshou.py`）；`config.py` 删 `temporal_*`；`compose.yaml` 删 temporal 服务；`pyproject.toml` 删 `temporalio`；删 `tests/test_sourcing_workflow.py`；README 去 Temporal 段。
- 验收：`grep -rn temporalio app/` 空；`docker compose config` 无 temporal；`uv sync` 后无 temporalio。

**T5.3 e2e 改写**
- 目标：`test_e2e_http.py` 的 `force_degraded`/sourcing 用例改为「ingest→存→（InMemory task）→done」断言；删 poll/done 旧断言。
- 验收：`uv run pytest -q` 全绿，无 Temporal 依赖。

**✅ 检查点 C5**：`pytest` 全绿且**不起 temporal**；`docker compose up` 仅 db；代码无 temporalio。

---

### Phase 6 — 扩展端 `client/`

**T6.1 扩展骨架 + URL 采集器**
- 目标：MV3 `manifest.json`（host 仅 `*.1688.com`）；`content/scrape.ts` 从列表/搜索/收藏页提 offer URL（纯函数）；`panel.ts` 勾选/批量。
- 验收：解析器喂固定 1688 列表 HTML 夹具 → 断言 offer URL 集；host 权限仅 1688。

**T6.2 本地队列 + ingest client**
- 目标：`background/queue.ts`（限频/重试/进度，storage 弱持久）；`api/ingest.ts` 带 JWT POST `/sourcing/ingest`。
- 验收：扩展 unpacked 加载 → 1688 页采 URL → POST → 服务端 batch 出现。

**✅ 检查点 C6**（真端到端）：浏览器装扩展 → 登录态 1688 采 URL → 自动回传 → 后端妙手 fetch+评分+（可选上架）→ 采集箱见结果。

---

### Phase 7 — 测试硬化

**T7.1 全套**
- 扩展：解析器夹具 + 队列状态机 + host 权限。
- task：`InMemoryConnector` 跑 post_process 管线 + 重试 + 幂等。
- ingest e2e：真 PG+RLS，URL→存→done；跨租户隔离。
- outbox：pending 超 grace → cron 重投。
- 验收：`uv run pytest -q` + `cd client && pnpm test` 全绿；覆盖关键路径。

**✅ 检查点 C7**：全绿；C2/C4/C6 关键路径各有自动化用例兜底。

---

## 3. 检查点汇总（阶段闸门）

| 检查点 | 关口 | 通过判据 |
|---|---|---|
| C1 | 队列地基 | trivial task defer→执行→RLS；alembic up/down 净 |
| C2 | ingest MVP | 真 URL curl→妙手fetch+评分→done；跨租户隔离 |
| C3 | 后处理全 | fetch/翻译/上架按 options 跑通 |
| C4 | 可靠性 | worker kill→cron 重驱→done 且只上架一次 |
| C5 | 去 Temporal | pytest 全绿、不起 temporal、无 temporalio |
| C6 | 扩展端到端 | 浏览器采 URL→回传→采集箱见结果 |
| C7 | 测试硬化 | py+扩展测试全绿 |

GATE T0 不过 → 全计划暂停。

---

## 4. 回滚 / 风险

- **妙手 fetch 风控（T0）**：最大风险，前置闸门。挡住则方案重议。
- **procrastinate 投递丢窗**：outbox+cron 兜底（C4 验证），零数据丢失。
- **去 Temporal 不可逆**：Phase5 前所有功能已在 procrastinate 跑通（C2-C4），删 Temporal 只是去死代码，低风险。保留 git 分支可回退。
- **扩展分发/JWT 注入**（待决 B）：不阻塞后端 Phase0-5，Phase6 前定。

---

## 5. 建议执行顺序

T0(GATE) → [Phase1 ∥ T1.A 妙手client] → Phase2(C2 MVP) → Phase3 → Phase4 → Phase5(去Temporal) → Phase6(扩展) → Phase7。

每阶段检查点过了再进下一阶段。Phase2 的 C2 是第一个可演示里程碑。
