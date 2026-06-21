# sourcing 客户端化 + 去 Temporal 落地设计

> 配套 `SPEC.md`。本文档定**实现期决策（ADR）+ 数据模型 + 接口契约 + 任务分解**。
> 四项已定方向（见 SPEC §0）：抓取搬客户端 / 妙手保留 / 后处理留服务端 / 去 Temporal 换 procrastinate（PG 队列）。
> 状态：设计草案，待 ADR-001/002 确认后进 `/plan`。

---

## 1. 目标回顾（一句话）

扩展在用户登录态 1688 采集 offer URL（过登录态风控）→ POST 服务端 `/sourcing/ingest` 存库（RLS）→ procrastinate 入队 → worker 跑妙手 fetch/评分/翻译/上架 → 采集箱。Temporal 下线。
（ADR-002：妙手不吃外部商品数据、它自抓详情，故扩展只做 URL 发现，不抓 detail。）

---

## 2. ADR-001：队列连接器 + 投递可靠性 ★核心

### 背景 / 约束
- app 业务层 = SQLAlchemy **async + asyncpg**，DB 会话受 RLS 租户上下文约束（`core/database.py`）。
- procrastinate 连接器（context7 核实 `/procrastinate-org/procrastinate`）共 5 种：
  - 异步：`PsycopgConnector`（psycopg3 async）、`AiopgConnector`（aiopg）。
  - 同步：`SyncPsycopgConnector`、`Psycopg2Connector`、`SQLAlchemyPsycopg2Connector`。
  - **唯一的 SQLAlchemy 连接器是 `SQLAlchemyPsycopg2Connector` —— 同步 + psycopg2，且官方注明「仅供 defer」。无 async-SQLAlchemy / asyncpg 连接器。**

### 结论
**真·单事务原子 defer（与 app 的 asyncpg 异步业务写在同一事务提交）不可行** —— procrastinate 无法共享 asyncpg 异步连接。`SQLAlchemyPsycopg2Connector` 是 psycopg2 同步，与 asyncpg 异步会话不同源、不同事务。

→ **不追求单事务原子，改用「业务表状态列当 outbox + procrastinate cron 兜底重投」实现 at-least-once，零数据丢失。**

### 方案（采纳）
1. **业务写仍走现有 asyncpg/SQLAlchemy async 会话**（保持 RLS 纪律统一）。`/ingest` 在一个异步事务里：存采集批 + 商品行，置 `post_status='pending'`。原子、RLS 正常。
2. **procrastinate 用 `PsycopgConnector`（psycopg3 async）** 跑 worker + defer，独立连接池。
3. **投递**：`/ingest` 事务**提交后**尝试 `await post_process.defer_async(batch_id=...)`。
   - 成功 → 正常路径。
   - 进程在「提交后、defer 前」崩溃 → 批次留 `post_status='pending'`，**不丢数据**（商品已落库）。
4. **兜底 = procrastinate 定时 task（cron，每 N 分钟）**：扫 `post_status='pending' AND updated_at < now()-grace` 的批 → 重新 `defer`。幂等：task 按 `batch_id` 处理，进入即把 `post_status='queued'→'running'`，完成 `'done'`，失败留 `'failed'`（带 attempts）。
   - 这就是 transactional-outbox，但用业务表自身状态列当 outbox，**不另起 outbox 表、不碰 procrastinate 内部表**。

### 为何不选其他
- ❌ 单事务原子（SQLAlchemyPsycopg2Connector）：要把 `/ingest` 的写从 asyncpg 改 psycopg2 同步，破坏全 app 异步 + RLS 会话统一，且阻塞事件循环。
- ❌ 直接 INSERT procrastinate_jobs 表做原子：耦合其内部 schema，升级易碎。
- ✅ 状态列 outbox + cron：保 asyncpg/RLS 统一、零丢失、与现 `claim_next` reaper 思路一致、低频场景 grace 延迟可接受。

### 待确认
- `grace`（pending 多久算掉队需重投）与 cron 周期：建议 grace=2min、cron=1min。
- procrastinate schema 落法：用其自带 migrations 还是包进一条 alembic 版本（推荐 alembic 版本调用 `SchemaManager.get_schema()`，与现有迁移链统一）。

---

## 3. ADR-002：妙手边界（已查实，扩展只能做 URL 发现）

### 妙手 CLI mode 全集（反推自 `tmp/zhuiwen_web.py` 11 个 `SELECT_CMD` 调用点）
> select.py 在仓库外（`~/.openclaw/skills/zhuiwen-product-selection/scripts/select.py`，本机未装），以下据调用点反推。

| mode | 参数 | 作用 |
|---|---|---|
| `url` | `--urls --limit --format json` | **妙手自己 fetch** 1688 链接 → 返回商品 cands JSON |
| `save` | `--urls` | 妙手 fetch URL → 存采集箱 |
| `box` | `--limit` | 列采集箱 |
| `detail` | `--id` | 箱内条目详情 |
| `edit` | `--id ...` | 改箱内条目（标题/图回写，翻译/优化用） |
| `delete` | `--ids` | 删箱内条目 |
| `images` | `--urls` | 妙手抓图 |
| `shops` | — | 列已绑定 TikTok 店铺 |
| `tkcall` | `--tk endpoint` | TikTok API 代理调用 |

### 关键结论：妙手不吃外部商品数据，它本身就是 fetcher
`tk_list_items(detail_ids, ...)`（`zhuiwen_web.py:1290`）决定性证据——上架全程按**采集箱条目 ID** 驱动：claimed 认领 → claim_to_shop → AI 选类目+物流模板 → 发布。而箱内条目**只能由 `save`/`url` 让妙手自己 fetch 1688 URL 生成**。**无任何 mode 接受「完整商品数据 payload」**；`edit` 只能改已在箱内（妙手抓的）条目。

→ **「扩展抓完整 detail → 妙手只负责上架」不可行**（妙手无外部数据入口）。

### 定案：扩展职责 = URL 发现（过登录态风控），妙手 = fetch + box + 上架（不变）
风控收益**不在 detail fetch**（妙手是 1688 采集 SaaS，自带反风控，旧流程服务端 fetch 能跑），**在 URL 发现层**：1688 搜索/收藏/类目列表常需登录可见，服务端抓列表撞风控。
- ✅ **扩展**：登录态浏览器采集 **offer URL**（+ 列表级可见基础字段），回传服务端。
- ✅ **妙手保留**：`url` fetch 详情 → 入箱 → `edit` 翻译/优化 → `tk_list_items` 上架。全服务端、按 box-id，**与旧 `ingest_1688_urls(urls,...)` 一致**。
- 此模型回传的是 **URL 列表**，非完整商品数据（修正原 §6 契约）。

### 残留未知（不阻塞主链，需观测）
- 妙手 **offer-detail fetch 自身是否撞风控**？旧 `zhuiwen_web` 服务端 fetch 能跑 → 大概率妙手扛得住。**若连妙手 fetch 都被挡，则死路**（妙手吃不了外部数据，只能另寻上架通道）。上线前用真实账号小批量验证妙手 fetch 成功率。

---

## 4. ADR-003：扩展弱持久 + 采集意图兜底

- 扩展本地队列（MV3 service worker + `storage`）抓取中，浏览器关闭/崩溃会丢未回传的抓取进度。
- 决策：**抓取阶段弱持久可接受**（用户重开扩展重抓即可，1688 页面还在）。**一旦回传到 `/ingest` 即落库持久**，后续后处理由服务端 procrastinate 保证（ADR-001）。
- 不做服务端「采集意图」预登记（旧 Temporal 的 pending 行模式废弃）——抓取未完成前服务端无状态，简化。

---

## 5. 数据模型

### 5.1 复用 `collect_jobs` → 语义转「采集批 batch」
旧字段 `status(pending/collecting/collected/completed/failed)` 是「服务端推任务、插件 poll」模型，**废弃**。新模型扩展直接回传，无服务端任务推送。

**迁移 `0004_sourcing_client.py`**（admin 连接 + 手写 RLS，遵现有迁移纪律）：
```
ALTER collect_jobs:
  + post_status   TEXT NOT NULL DEFAULT 'pending'   -- pending|queued|running|done|failed
  + attempts      INT  NOT NULL DEFAULT 0
  + last_error    TEXT
  + source        TEXT DEFAULT '1688'               -- 采集来源市场
  (保留 tenant_id/result/created_at/updated_at + RLS 策略)
  旧 status 列：保留一版做兼容，下一迁移再删；或本迁移直接弃用并改用 post_status
```
- `result` JSONB 阶段性存：① 扩展回传的 `urls`；② 妙手 fetch 后的 cands + scores；③ 上架结果。量大时再拆 `collect_items` 子表（P2，先 JSONB）。

### 5.2 procrastinate 自有表
其 `procrastinate_jobs` 等表经 schema 应用（见 ADR-001 待确认）。**不参与 RLS**（基建表）。

---

## 6. 接口契约：`POST /sourcing/ingest`

扩展回传入口。替代旧 `/sourcing/jobs/poll` + `/done`（删）。

**Request**（`IngestRequest`，对齐旧 `ingest_1688_urls(urls, ...)`；据 ADR-002，回传 **URL 列表**非完整商品）：
```jsonc
{
  "market": "1688",
  "urls": [                           // 扩展在登录态采集的 offer URL（过风控的产物）
    "https://detail.1688.com/offer/123.html",
    "https://detail.1688.com/offer/456.html"
  ],
  "options": {                        // 后处理选项，对齐旧语义
    "threshold": 70, "top_n": 0,
    "translate": false, "lang": "",
    "list_tiktok": false, "tk_auto": false, "optimize": false,
    "platform": "tiktok"
  }
}
```
**Response**：`{ "batch_id": "...", "accepted": N, "post_status": "queued" }`（异步，立即返回；结果经 GET 轮询）。

**校验/安全**：JWT → tenant 中间件 → RLS；每个 URL 必须含 `1688.com/offer/`（对齐旧 `ingest_1688_urls` 过滤）；去重、上限 200；`urls` 非空；写入用 user_id 归属。

**查询**：`GET /sourcing/jobs/{batch_id}` 复用（返回 `post_status` + `result` + scores）。

---

## 7. post_process task 设计（procrastinate）

`app/domains/sourcing/tasks.py`：
```python
@queue_app.task(name="sourcing.post_process", retry=...)   # 重试+退避：context7 task retry
async def post_process(batch_id: str, tenant_id: str):
    # 1) 显式设租户上下文（禁 ContextVar，同旧 Temporal activity 纪律）
    async with tenant_session(tenant_id) as db:        # set_config('app.current_tenant', tenant_id)
        batch = await repo.get(batch_id)                # RLS 限本租户
        mark(batch, 'running')
        urls = batch.result["urls"]                 # 扩展回传的 offer URL
        # 1.5) 妙手 fetch：SELECT_CMD --mode url --urls ... → cands（妙手自抓详情入箱）
        cands = miaoshou_url_fetch(urls)
        # 2) 评分（Qwen）——吸收旧 _score_candidates；top_n 后删不达标 box 条目(delete)
        scored = score(cands, batch.options.threshold, top_n=...)
        # 3) 翻译/优化：SELECT_CMD --mode edit 回写 box 条目（studio.translate_*，违禁词清洗）
        # 4) 图片质检 pick_good_images 可选 → edit 回写
        # 5) 上架：tk_list_items(box detail_ids, shop) —— 全按 box-id（ADR-002）
        if options.list_tiktok: publish_tiktok(scored, options)
        mark(batch, 'done', result=scored)
```
- **租户上下文**：`tenant_session(tenant_id)` 包装——开会话 + `SET app.current_tenant` + RLS。worker 跨进程，`tenant_id` 必为 task 入参。
- **重试**：task 级 `retry`（次数 + 指数退避）。妙手 520s 超时 → task 超时配置需 > 妙手单批耗时。失败 `attempts++`、`last_error`、达上限 `post_status='failed'`。
- **幂等**：进入即 CAS `pending/queued → running`；已 `done` 直接跳过（cron 重投防重复执行）。
- **逻辑来源**：移植 `tmp/zhuiwen_web.py` 的 `_score_candidates` / `_clean_title`(违禁词) / `studio.translate_*` / `ingest_1688_urls` 的 top_n+save_passing+list_tiktok 段。

---

## 8. 扩展架构（`client/`，MV3）

```
client/
├── manifest.json          # MV3；host_permissions 仅 *.1688.com；不申请越权
├── src/
│   ├── content/scrape.ts  # 1688 列表/搜索/收藏页 → 提取 offer URL（+列表级字段）；纯函数可单测
│   ├── content/panel.ts   # 采集面板：勾选 offer/批量采 URL/进度
│   ├── background/queue.ts # service worker 本地队列：限频+重试+进度（弱持久 storage）
│   ├── api/ingest.ts      # 带 JWT POST /sourcing/ingest
│   └── lib/clean.ts       # 违禁词清洗（与服务端词库同源，避免漂移）
└── tests/
```
- 内容脚本**只读 DOM、不调 LLM、不存密钥**（合规红线）。
- 鉴权：扩展持 JWT（登录态从主站/配置注入），回传带 Bearer。
- 违禁词库：服务端为权威源，扩展构建期同步或运行期拉取，禁两份硬编码漂移。

---

## 9. Temporal 移除步骤

1. 加 `procrastinate` 依赖（`pyproject.toml` + uv.lock）；应用其 schema（alembic `0004` 或并入 `0005_procrastinate`）。
2. 实现 `app/shared/queue/`（procrastinate `App` + `PsycopgConnector` + `tenant_session` 包装）。
3. 实现 `tasks.post_process` + `/sourcing/ingest` + 迁移 `0004`。
4. `app/workers/main.py`：删 Temporal Worker，改 `await queue_app.run_worker_async()`（含 cron 兜底 task）。
5. 删 `sourcing/workflows.py`、`sourcing/activities.py` 的 Temporal 部分；保留被 task 复用的纯业务函数。
6. `compose.yaml`：移除 `temporal` 服务（恢复纯 db）。
7. `app/core/config.py`：删 `temporal_*` 配置。
8. README：去 Temporal 启动段，改 procrastinate worker 说明。

---

## 10. 测试策略

- **扩展**：解析器喂固定 1688 HTML 夹具 → 断言字段；队列状态机（限频/重试/进度）。host 权限仅 1688。
- **task**：procrastinate `InMemoryConnector` 单测 `post_process` 管线 + 重试 + 幂等（重复 defer 同 batch 不重复上架）。
- **ingest e2e**：真 PG + RLS，扩展回传 → 落库 `post_status='pending'` → （worker mock 或 InMemory）→ 评分/存库；跨租户隔离（A 回传不污染 B）。
- **outbox 兜底**：造「pending 超 grace」批 → cron 扫到重投 → done。
- **去 Temporal 回归**：现 `tests/test_sourcing_workflow.py`（Temporal time-skipping）删除/替换；`test_e2e_http.py` 的 `force_degraded` 改写为「ingest 直存 + 入队」断言。
- LLM/妙手用替身；其余真跑。

---

## 11. 任务分解（进 /plan 用）

| # | 任务 | 依赖 | 备注 |
|---|---|---|---|
| T0 | 真实账号验证妙手 `url` fetch 成功率（风控残留未知，ADR-002） | — | 上线前置；失败则整方案重议 |
| T1 | 妙手调用封装：`url`/`edit`/`delete`/`tk_list_items` 包成服务端 client（移植 zhuiwen_web） | — | |
| T2 | procrastinate 接入：依赖 + schema + `shared/queue` + `tenant_session` | — | |
| T3 | 迁移 0004：collect_jobs 加 post_status/attempts/last_error | — | |
| T4 | `/sourcing/ingest` 端点（收 **urls**）+ IngestRequest 契约 + 落库 | T3 | |
| T5 | `tasks.post_process`：妙手 fetch → 评分 + 违禁词清洗（移植旧逻辑） | T1,T2,T3 | |
| T6 | 翻译/质检段（studio + `edit` 回写，可选开关） | T5 | |
| T7 | 上架段 `tk_list_items`（box-id 驱动） | T5,T1 | |
| T8 | cron 兜底 task（扫 pending 重投）+ 幂等 | T2,T5 | |
| T9 | worker 改 procrastinate；删 Temporal（workflows/activities/compose/config/README） | T5 | |
| T10 | 扩展 `client/`：manifest + URL 采集器 + 队列 + ingest client | T4 | |
| T11 | 测试全套（§10） | T4-T10 | |

**关键路径**：T0（妙手 fetch 风控验证）最先，结果决定整方案是否成立；其次 T1（妙手 client 封装）→ T5。

---

## 12. 未决（汇总）

- 🚩 **妙手 `url` fetch 风控验证（T0）**—— 若妙手自抓详情也被风控挡，则妙手吃不了外部数据、整方案需重议。最高优先，但属上线前实测，非设计阻塞。
- ADR-001：grace/cron 周期；procrastinate schema 落 alembic 方式。
- 扩展 JWT 注入方式 + 分发渠道（unpacked/企业/商店）。
- 列表级字段：扩展除 URL 外是否带回列表页可见的标题/价/缩略图，供「妙手 fetch 前预筛」减少 fetch 量（优化项，非必需）。

> ADR-002「妙手是否接受外部数据」已查实定论：**不接受**，妙手自抓详情、按 box-id 上架。扩展职责锁定为 URL 发现。
