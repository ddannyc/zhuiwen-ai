# XBorder AI — 跨境电商多租户 AI 应用骨架

模块化单体（modular monolith）。一个仓库，两类进程，按业务域分包。

## 目录结构

```
app/
├── main.py                    # ① API 进程入口  (uvicorn app.main:app)
├── core/                      # 跨域基础设施（不含业务逻辑）
│   ├── config.py              #   集中读环境变量
│   └── database.py            # ★ DB 会话 + RLS 租户上下文注入（隔离命脉）
├── shared/                    # 跨域共享能力
│   ├── auth/jwt.py            #   JWT 编解码
│   ├── tenant/middleware.py   # ★ 唯一解析租户身份的地方
│   ├── llm/gateway.py         #   LLM 网关（经 LiteLLM，按租户计费）
│   ├── llm/embeddings.py      #   向量化
│   └── billing/               #   计费（占位）
├── domains/                   # 业务域：每个域内部 router→service→repository 分层
│   ├── auth/                  #   登录换 JWT（demo 账号；签名稳定，后续替真实鉴权）
│   ├── chat/                  #   会话 + SSE 流式 + agent 工具路由（已闭环）
│   ├── sourcing/             # ★ 商品采集：HTTP 桥 + CollectWorkflow（Temporal，已闭环）
│   │   ├── workflows.py       #     CollectWorkflow：等采集插件信号→评分→翻译→上架
│   │   └── activities.py      #     activity 落库（带 tenant RLS）；后处理目前为集成桩
│   ├── knowledge_base/        #   知识库（pgvector）— 分层示范；embeddings 尚未接通（PARTIAL）
│   ├── listing/agent.py       #   listing 本地化 agent（LangGraph）— 骨架，router 未挂
│   ├── publishing/workflows.py#   多平台批量刊登（Temporal）— 占位，整段注释
│   └── customer_service/      #   智能客服（示范跨域调用纪律）— 骨架，router 未挂
└── workers/main.py            # ② Worker 进程入口  (python -m app.workers.main)
migrations/                    # Alembic 迁移（版本化，up/down）
├── env.py                     #   连 admin 连接；URL 取自 config
└── versions/
    ├── 0001_chat_setup.py     #   chat 域表 + RLS（基线，无 pgvector 依赖）
    ├── 0002_knowledge_base.py #   pgvector + kb 表 + RLS（需装 pgvector）
    └── 0003_sourcing_setup.py #   collect_jobs 表 + RLS（采集任务，tenant 级共享）
web/                           # 前端（Vite + React + TS），打后端 /auth、/chat
```

## 两类进程，一份代码

| 进程 | 启动命令 | 职责 |
|------|----------|------|
| API | `uvicorn app.main:app` | 处理 HTTP，快进快出 |
| Worker | `python -m app.workers.main` | 长流程、批量、定时任务 |

同仓库、同 import，只是入口不同。这样隔离了负载，又没有微服务的协作成本。

## 三条必须守住的纪律

1. **按业务域分包**，不按技术层分包。一个功能聚在一个域目录里，将来好拆。
2. **禁止跨模块直接读对方的表**。要数据就调对方 service 暴露的方法
   （见 `customer_service/service.py` 如何调 `KnowledgeBaseService`）。
   将来把某个域拆成独立服务，只需把函数调用换成 RPC。
3. **租户上下文统一注入**。`tenant/middleware.py` 解析 JWT → `core/database.py`
   把 tenant_id 注入 PG 会话变量 → RLS 自动过滤。业务代码永远不写
   `WHERE tenant_id`，也不手填 tenant_id。

## 关于"工作流"的两层，别混

- **agent 推理循环**（模型自主调工具）→ LangGraph，跑在请求里或 worker 里
  （`listing/agent.py`）。
- **业务长流程**（采集、刊登，要持久化/重试/恢复）→ Temporal，只在 worker 里。
  当前已闭环的是 `sourcing/workflows.py` 的 `CollectWorkflow`（采集插件人在环 +
  最长 1h 等待 + activity 自动重试）；`publishing/workflows.py` 是占位。

⚠️ Temporal workflow 跨进程运行，租户上下文不能靠 ContextVar 传，
必须把 `tenant_id` 作为显式参数贯穿 workflow 和 activity。

## 何时才把某个域拆成独立服务

出现这些信号再拆，一次只拆一块：扩缩容节奏差异大（如向量检索吃内存/GPU）、
故障会拖垮全局、团队多组互相等部署、独立合规/数据驻留要求（如欧盟数据留欧盟）。
信号出现前拆 = 过早优化。

## 依赖

Python 依赖用 **uv** 管理（`pyproject.toml` + `uv.lock`）。前端用 **pnpm**。

```
fastapi uvicorn sqlalchemy[asyncio] asyncpg pgvector
pydantic-settings pyjwt httpx langgraph temporalio
alembic psycopg[binary]            # 迁移工具
litellm                            # LLM SDK（进程内，多 provider + 工具归一，接百炼 qwen）
```

## 本地启动

### 前置
- **uv**（Python 依赖/运行）、**Docker**（起 Postgres + Temporal，见 `compose.yaml`）、
  **Node + pnpm**（前端）
- 依赖容器化、零自建：`compose.yaml` 用官方镜像 `pgvector/pgvector:pg17`（含 pgvector 扩展）
  + `temporalio/temporal`（dev server，内存态，自带 UI）。无需本机装 PG / Temporal。

### 1. 配置
```bash
cp .env.example .env
# .env 已默认指向 compose：db 在宿主 5433，temporal_host=localhost:7233。
# 真实 chat 需填 DASHSCOPE_API_KEY（见下 §3），其余开箱即用。
```

### 2. 起依赖 + 迁移 + 后端进程
```bash
uv sync                              # 装 Python 依赖（含 dev）
docker compose up -d                 # 起 db(5433) + temporal(7233, UI 8233)
uv run alembic upgrade head          # 跑 0001-0003 迁移（pgvector 镜像自带扩展，直接到 head）

uv run uvicorn app.main:app --reload --port 8000   # ① API 进程
uv run python -m app.workers.main                  # ② Worker 进程（注册 CollectWorkflow）
```
说明：
- **Worker 必起**才有非降级采集闭环（collect→workflow→signal→completed）。
  不起 Temporal/worker 时 sourcing 自动走 **degraded**（API 直驱 DB 状态机，happy path 可用，
  但无 durable 等待/重试/崩溃恢复）。
- 重置库：`docker compose down -v && docker compose up -d && uv run alembic upgrade head`。
  Temporal dev 本就内存态，重启即清。
- 非 Docker 路径（本机原生 PG）：改用 `uv run python scripts/db_bootstrap.py` 建库，
  并把 `.env` 的 5433 改回你的端口；不推荐，与 CI/teammate 不一致。

### 3. LLM（gateway 内 LiteLLM SDK 进程内调用，**无独立网关服务**）

chat 调用在后端进程内经 LiteLLM SDK 直打 DashScope 兼容端点。无需起代理。

**真实模型 —— 阿里百炼 qwen**：`.env` 填
```
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1   # 国际区 dashscope-intl
DASHSCOPE_API_KEY=<百炼控制台 Key>
```
app 默认调 `qwen-plus`（`chat_model` 可改）。chat 需真实 Key，不提供 mock 端点。

### 4. 前端
```bash
cd web
pnpm install
pnpm dev                             # http://localhost:5173
# 后端地址默认 http://localhost:8000，可用 VITE_API_BASE 覆盖
```

### 5. 登录（demo 账号）
内置「一租户两员工」：账号 **alice** / **bob**，密码均 **demo**（同租户，互不可见对方会话）。

## 数据库迁移（Alembic，类 rails db:migrate）

```bash
uv run alembic upgrade head          # 升到最新
uv run alembic downgrade -1          # 回退一步
uv run alembic current               # 当前版本
uv run alembic history               # 迁移历史
uv run alembic revision -m "xxx"     # 新建迁移（手写 op.execute；RLS 不能 autogenerate）
```

迁移用 admin 连接（`database_admin_url`，超级用户）跑；运行期业务用受 RLS 约束的
`app` 角色（`database_url`）。RLS / `current_setting` 默认值 / 策略均手写在版本文件里。

## 测试

```bash
uv run pytest -q
```
覆盖：规则检索契约、chat 路由全链路（单测内 monkeypatch gateway 作替身）、/auth、
sourcing 三级降级 + CollectWorkflow（用 Temporal time-skipping 测试环境，无需起真集群）、
真 HTTP e2e（打真 PG + RLS + 跨租户隔离，PG 不可达则自动 skip）。
LLM 是 e2e 里唯一的替身，其余（路由 / RLS / DB / SSE / workflow 编排）都跑真的。
