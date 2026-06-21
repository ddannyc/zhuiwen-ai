# SPEC：商品采集转客户端执行（去 Temporal）

> 状态：草案，待确认。本文档是「可行性 + 设计」规格，回答 *采集任务转移到客户端执行是否可行*，并给出落地边界。
> 不在本轮实现，仅定义方向、范围与红线。

---

## 0. TL;DR / 已定决策

**方向确认：抓取搬客户端（过风控），后处理留服务端，去 Temporal，后台队列用 procrastinate（PG，无 Redis）。**

四项决策（已拍板）：

1. ✅ **抓取（fetch）搬客户端**：1688 详情服务端抓撞风控（IP/登录态/验证码），改在用户已登录的浏览器扩展里抓，天然过风控。核心诉求，落地。
2. ✅ **妙手（Miaoshou）保留**：fetch_item 深采集、采集箱、TikTok 上架继续走妙手 SaaS（`SELECT_CMD`），不自建。扩展负责在登录态 1688 取链接/基础字段并触发；妙手深采 + 上架仍在服务端经妙手 CLI。
3. ✅ **后处理留服务端**：评分（Qwen）、翻译（studio）、图片质检、上架（妙手）全在服务端，借 RLS + 集中计费 + 可观测。评分/翻译无风控问题，不搬客户端、不暴露 key。
4. ✅ **去 Temporal，换 procrastinate（PG 队列）**：后处理是低频线性管线（评分→翻译→上架），不需要 Temporal 的 signal/child-workflow/durable-timer。改用 **procrastinate**（PostgreSQL-backed、asyncio 原生、自带重试/退避/cron/LISTEN-NOTIFY），零新基建。详见 §3a。

**架构净变化**：服务端退出「主动爬取」；扩展抓→`/sourcing/ingest` 收→procrastinate 入队→worker 跑评分/翻译/妙手上架→采集箱（RLS 存库）。Temporal server + worker 下线。

---

## 1. 目标 Objective

把 **sourcing 商品采集**（1688 选品 → 评分/翻译 → 上架 TikTok/采集箱）的**抓取执行**从服务端搬到客户端「1688 采集助手」浏览器扩展，**借用户真实登录态 + IP 过 1688/平台风控**；相应地让服务端退出「主动爬取」角色，并评估去除 Temporal 编排。

**目标用户**：跨境电商运营人员，浏览器已登录 1688 货源端 + TikTok/Ozon 卖家后台，日常做选品上架。

**成功判据（验收）**：
1. 运营在 1688 列表/详情页点扩展「采集」，**无服务端 IP 抓取**即可拿到完整商品详情（标题/价格/主图/SKU），成功率显著高于现服务端抓取（撞风控）。
2. 采集结果经评分（默认阈值可配）后入采集箱，与旧 `ingest_1688_urls` 行为对齐（违禁词清洗、top_n、可选翻译）。
3. 整条链路**不依赖 Temporal**仍能跑通（happy path），或 Temporal 仅作可选增强。
4. 合规红线全程不破（见 §8 边界）：仅采本店授权可见数据，不在浏览器调 LLM 抓后台越权数据。

**非目标**：
- 不动 rules_kb 规则采集（那是另一套「采集」，语义不同，禁合并——见 `docs/chat-redesign-plan.md:73`）。
- 不重写 chat/agent、auth、RLS。
- 不替换妙手（已决定保留，依赖 `SELECT_CMD` 不动）。

---

## 2. 现状 vs 目标架构

### 旧（`tmp/zhuiwen_web.py`，已弃）
```
扩展(登录态1688) ──URL──▶ 服务端 ingest_1688_urls
                              └─ 妙手 SELECT_CMD fetch_item 采集（服务端调，撞风控点）
                              └─ _score_candidates（Qwen 评分）
                              └─ studio 翻译/质检图
                              └─ 妙手 采集箱 / TikTok 上架
状态：进程内存数组（重启即丢、无租户隔离）
```

### 现（本仓库，Temporal）
```
chat/agent ─collect_products工具─▶ /sourcing/collect
                                     └─ CollectWorkflow（Temporal，server 编排）
                                          ├─ enqueue_browser_task（落 pending 行，RLS）
                                          ├─ wait browser_done 信号（≤1h）
                                          └─ score→translate→publish（当前为穿透桩）
扩展 ──poll/done──▶ 桥端点 ──signal──▶ workflow
```

### 目标（客户端自治）
```
扩展(登录态1688) ── 在浏览器内：抓详情 →〔评分/翻译可留服务端〕→ 结果
       └──────── POST 最终结果 ──▶ 服务端 /sourcing/ingest（仅收+存库，RLS）
服务端：不再主动爬；提供 LLM/规则/采集箱存储能力；Temporal 下线或可选
```

| 维度 | 现（Temporal） | 目标（客户端） |
|---|---|---|
| 抓取执行 | 浏览器插件 poll/done，server 编排等待 | 浏览器扩展自驱，本地排队 |
| 风控 | 妙手/服务端抓易撞 | 用户登录态+IP，天然过 |
| 编排/durable | Temporal server | procrastinate（PG 队列，durable+重试）；扩展本地队列管抓取 |
| 后处理 | 服务端 activity（桩） | **服务端**（procrastinate task：评分/翻译/妙手上架） |
| 服务端角色 | 编排 + 存储 + LLM | 收结果 + 存储 + 后处理 + LLM + 规则（退出主动爬取） |

---

## 3. 可行性分析（按管线阶段）

| 阶段 | 客户端可行性 | 风控收益 | 风险 / Blocker |
|---|---|---|---|
| 1688 列表/详情抓 URL+基础字段 | ✅ 高（扩展读 DOM，已有 v1.x 油猴范式） | ⭐⭐⭐ 核心收益 | 1688 DOM 改版需维护；SPA 路由 hook（油猴脚本已有先例） |
| 商品完整详情 fetch_item（SKU/规格/全图） | ⚠️ 中 | ⭐⭐⭐ | **妙手在扛这块**。扩展直读详情页可拿大部分，但深字段（批量 SKU、官方接口字段）可能要妙手 |
| AI 评分 _score_candidates | ✅ 技术可行 | ⭐ 无风控收益 | 浏览器调 DashScope 暴露 key；**建议留服务端** |
| 翻译标题/图 studio.translate_* | ✅ 技术可行 | ⭐ 无 | 同上，且需图片公网直链供翻译——服务端做更顺 |
| 图片质检 pick_good_images（Qwen-VL） | ✅ | ⭐ 无 | 同上 |
| 违禁词清洗 _clean_title | ✅ 纯文本，扩展可做 | — | 词库要同步（现硬编码在 server） |
| 入采集箱 | 🚩 取决于妙手 | — | 妙手采集箱是 SaaS；弃妙手则要自建 box 存储（现 `box/` 域是桩） |
| 上架 TikTok（OAuth+发布） | 🚩 妙手扛 | — | 扩展重写 TikTok 上架=大工程；强烈建议保留妙手或服务端代理 |

**净判断**：风控痛点**只在抓取**。把抓取搬扩展拿满收益；评分/翻译/上架搬扩展是负收益（暴露 key、重写妙手）。故「全客户端」过度，**抓取客户端 + 后处理服务端**才是最优解。

---

## 3a. 后台队列方案：procrastinate（替代 Temporal）

**为什么 PG 队列而非 Redis/Sidekiq 类**：本仓库已重度依赖 Postgres（RLS、pgvector），后处理低频、量小（单批妙手 fetch ≤520s）。引 Redis = 多一套要跑/监控/备份的基建，且队列在 Redis 时 RLS 管不到、租户隔离要另写。PG 队列：durable 免费、RLS 天然、已有 `claim_next`（`FOR UPDATE SKIP LOCKED`）雏形为证。Redis 仅在吞吐上千 job/s、需 pub/sub 扇出、或已为缓存跑 Redis 时才值——本项目都不沾。

**选 procrastinate**（已核实，context7 `/procrastinate-org/procrastinate`）：
- PostgreSQL-backed、**asyncio 原生**（`PsycopgConnector` / `run_worker_async` / `defer_async`），与 FastAPI（async）契合。
- 自带：重试 + 退避、cron 定时、`LISTEN/NOTIFY` 低延迟唤醒、job 锁、worker。等于把「手搓 reaper + 重试列」打包好。
- **原子入队**：`task.configure(connection=conn).defer(...)` 可与业务写同事务提交——`/ingest` 存采集结果与「入队后处理 job」一起原子落库，不丢任务。
- 自带 schema（独立表），经其 migrations 或 `SchemaManager` 应用。

**管线落地形态**：
```
扩展抓取 ─POST─▶ /sourcing/ingest
                  └─ 存原始采集结果（RLS）+ defer 后处理 job（同事务）
procrastinate worker（复用 app/workers/main.py，去 Temporal）:
  task: post_process(tenant_id, batch_id)
    → 评分(Qwen) → 翻译(studio) → 图片质检 → 妙手上架/入采集箱
    → 失败 task 级 retry+退避；达上限标 failed
```

**两个落地约束（实现时必守）**：
1. **租户上下文显式传参**：procrastinate worker 跨进程，和旧 Temporal activity 同理——`tenant_id` 必须作为 job 参数贯穿 task 与所有 DB 调用，**禁靠 ContextVar**；task 内显式 `set_config('app.current_tenant', tenant_id)` 再走 RLS。procrastinate 自己的表不参与租户隔离（它是基建）。
2. **连接器/投递**（已查实定案，详 `docs/sourcing-client-migration.md` ADR-001）：procrastinate 无 async-SQLAlchemy/asyncpg 连接器（唯一 SQLAlchemy 连接器是 psycopg2 同步），**真·单事务原子 defer 不可行**。改用「业务表 `post_status` 列当 outbox + procrastinate cron 兜底重投」实现 at-least-once、零丢失。业务写仍走 asyncpg/RLS，procrastinate 用 `PsycopgConnector`(psycopg3)。

**迁移步骤**：① 加 procrastinate 依赖 + 应用其 schema（新 alembic 版本或其自带 migrations）；② `post_process` task 实现（吸收旧 `ingest_1688_urls` 的评分/top_n/翻译/上架逻辑）；③ `app/workers/main.py` 去 Temporal、改跑 procrastinate worker；④ `compose.yaml` 移除 temporal 服务；⑤ 删 `sourcing/workflows.py` + `activities.py` 的 Temporal 编排（保留被复用的纯业务逻辑）。

---

## 4. 范围 Scope

**In**
- 浏览器扩展「1688 采集助手」：在登录态 1688 抓详情，本地批量队列，结果 POST 服务端。
- 服务端 `/sourcing/ingest`：收扩展回传 → 存库（RLS）+ defer procrastinate 后处理 job。
- procrastinate 后处理 task：评分 → 翻译 → 妙手上架/采集箱。对齐旧 `ingest_1688_urls` 语义（threshold/top_n/违禁词/翻译/list_tiktok）。
- 移除 Temporal：删 workflow/activity 编排 + compose temporal 服务；worker 改 procrastinate。

**Out**
- rules_kb 采集、chat 重构、auth/RLS。
- 弃妙手 / 自建 fetch_item·采集箱·TikTok 上架（已决定保留妙手）。
- 后处理客户端化（已决定留服务端）。

---

## 5. 命令 Commands

**后端**
```bash
docker compose up -d                 # db(5433)；temporal 服务移除
uv run alembic upgrade head          # 含 procrastinate schema（新版本或其自带 migrations）
uv run uvicorn app.main:app --reload --port 8000
uv run python -m app.workers.main    # procrastinate worker（替代 Temporal worker）
uv run pytest -q
```

**扩展（新增 `client/` 目录，待建）**
```bash
cd client && pnpm install
pnpm dev          # 扩展开发模式（watch 构建到 dist/，浏览器加载 unpacked）
pnpm build        # 打包扩展
pnpm test         # 扩展单测（抓取解析器、队列状态机）
pnpm lint
```

---

## 6. 项目结构

```
client/                         # ★ 新增：浏览器扩展「1688 采集助手」
├── manifest.json               #   MV3；host_permissions 限 1688 域
├── src/
│   ├── content/                #   注入 1688 页：DOM 抓取 + 采集面板 UI
│   │   ├── scrape.ts           #   详情解析器（标题/价/图/SKU），对标妙手字段
│   │   └── panel.ts            #   采集面板（选择/批量/进度）
│   ├── background/             #   MV3 service worker：本地队列 + 限频 + 重试
│   │   └── queue.ts            #   采集任务状态机（替代 Temporal 编排，弱持久=storage）
│   ├── api/                    #   回传服务端 + 鉴权（带 JWT）
│   └── lib/clean.ts            #   违禁词清洗（与服务端词库同源）
└── tests/

app/domains/sourcing/           # 后端：从「编排爬取」转「收结果存库 + 入队后处理」
├── router.py                   #   + POST /sourcing/ingest（扩展回传入口，存库 + defer job）
├── service.py                  #   ingest 落库；评分/翻译/上架移到 tasks
├── tasks.py                    #   ★ 新：procrastinate task post_process（吸收旧 ingest_1688_urls 逻辑）
├── ingest.py                   #   ★ 新：评分/top_n/违禁词清洗（纯逻辑，task 调用）
└── workflows.py / activities.py#   ✂ 删 Temporal 编排；保留可复用的纯业务逻辑

app/workers/main.py             # ✂ 去 Temporal worker → 跑 procrastinate run_worker_async
app/shared/queue/               # ★ 新：procrastinate App 实例 + 连接器 + 租户上下文包装
app/domains/box/                # 采集箱：经妙手（保留），本地仅存元数据/状态
compose.yaml                    # ✂ 移除 temporal 服务
docs/sourcing-client-migration.md  # ★ 落地设计 + 连接器/原子defer 决策（ADR）
```

---

## 7. 代码风格

- 后端：沿用本仓库纪律——按业务域分包、禁跨域读表、租户上下文走 `tenant/middleware` + RLS，业务码不写 `WHERE tenant_id`。
- 扩展：TypeScript + MV3；内容脚本只读 DOM 不调 LLM（合规红线）；抓取解析器纯函数、可单测；网络请求带 JWT、限频、指数退避重试。
- 字段契约：扩展回传的商品 schema 与服务端 `StartCollectRequest`/采集箱模型对齐，单一来源（共享 `contract.ts` 风格定义），禁两端漂移（参考现有 chat contract 漂移教训）。

---

## 8. 测试策略

- **扩展单测**：详情解析器喂固定 1688 HTML 夹具 → 断言字段；本地队列状态机（pending→fetching→done/failed、限频、重试）。
- **后端**：`/sourcing/ingest` 评分/top_n/违禁词清洗回归（对标旧 `ingest_1688_urls`）；RLS 隔离（扩展 A 回传不污染租户 B）；真 HTTP e2e。
- **procrastinate task 测试**：`post_process` 用 procrastinate 的内存/测试连接器（`InMemoryConnector`）单测——评分/翻译/上架管线 + 重试行为，不起真 worker。
- **去 Temporal 回归**：删 workflow 后，sourcing 全链路（ingest→入队→存库）e2e 通过；现 `force_degraded` fixture 的「无 Temporal 也能跑」语义可复用/改写为「ingest 直存」断言。
- **合规测试**：断言内容脚本 host 权限仅限 1688 域；断言不抓登录后台越权字段。
- LLM 仍用替身（mock gateway），其余真跑。

---

## 9. 边界 Boundaries

**Always（必做）**
- 抓取仅限**用户本店授权可见**的 1688 货源数据（`docs/chat-redesign-plan.md:73` 合规边界）。
- 扩展内容脚本 `host_permissions` 收窄到 1688 域；只读 DOM，**不在浏览器调 LLM**。
- 回传带 JWT，服务端按 RLS 存库；沿用 user_id 归属 / tenant 隔离模型。
- procrastinate task **显式传 `tenant_id`**、task 内 `set_config` 设租户再走 RLS（禁 ContextVar，同旧 Temporal activity 纪律）。
- 删 Temporal 前确认无其他长流程依赖（publishing 占位 workflow）；连接器/原子-defer 方案先写 ADR（`docs/sourcing-client-migration.md`）再动代码。

**Ask first（先问）**
- 扩展分发方式（内部 unpacked / 企业私有 / 应用商店）——影响 manifest 与更新机制。
- procrastinate 连接器选型（SQLAlchemy 连接器做原子 defer vs 自有连接小事务）。
- 扩展本地队列丢任务（浏览器关闭/崩溃）的可接受度，是否要服务端「采集意图」兜底登记。

**Never（禁止）**
- 不抓登录态后台的越权/他人数据；不绕平台风控做未授权批量爬取。
- 不把 1688 原文整段转储入库（与 rules_kb 红线一致：只存提炼/结构化结果）。
- 不在扩展硬编码任何密钥（DashScope/妙手/center）。
- 不合并 sourcing 与 rules_kb 两套「采集」。
- 不为这套低频后处理引 Redis/独立 broker（PG/procrastinate 足够）。

---

## 10. 决策记录 + 剩余待决

**已定（本轮拍板）**
1. ~~妙手去留~~ → **保留**妙手（fetch_item 深采 / 采集箱 / TikTok 上架）。
2. ~~后处理位置~~ → **留服务端**（评分/翻译/上架，借 RLS+计费+可观测）。
3. ~~Temporal 去留~~ → **移除**，换 procrastinate（PG 队列）。
4. ~~队列方案~~ → **procrastinate**（PG，无 Redis）。

**已查实定案**（详 `docs/sourcing-client-migration.md`）
- A. ~~连接器/原子 defer~~ → procrastinate 无 asyncpg/async-SQLAlchemy 连接器，原子 defer 不可行 → **outbox 状态列 + cron 兜底**（ADR-001）。
- C. ~~扩展弱持久~~ → 抓取阶段弱持久可接受，回传即落库持久，不做服务端意图预登记（ADR-003）。

**ADR-002 已查实定论**（反推妙手 CLI 11 个调用点 + `tk_list_items`）
- 妙手**不接受外部商品数据**：上架按采集箱 box-id 驱动，箱内条目只能由妙手 `url`/`save` 自抓 1688 生成。「扩展抓 detail → 妙手上架」**不可行**。
- → 扩展职责锁定 = **登录态采集 offer URL（过风控）**；妙手保留 fetch+box+上架（同旧 `ingest_1688_urls`）。ingest 契约回传 **URL 列表**。

**剩余待决**
- 🚩 **妙手 `url` fetch 风控验证（T0，上线前实测）**：若妙手自抓详情也撞风控，妙手吃不了外部数据 → 整方案重议。属实测非设计阻塞。
- B. 扩展分发（unpacked / 企业私有 / 商店）+ JWT 注入方式。
- D. publishing 占位 `BulkPublishWorkflow` 删 Temporal 后归宿（procrastinate task 或暂搁）。

---

*下一步：本 SPEC 已据四项决策定稿。细化 `docs/sourcing-client-migration.md`（连接器 ADR + task 设计 + 扩展 manifest）+ 任务分解，即可进 `/plan`。*
