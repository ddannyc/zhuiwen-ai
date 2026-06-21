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
│   ├── knowledge_base/        #   知识库（pgvector）— 完整示范一个域的分层
│   ├── listing/agent.py       #   listing 本地化 agent（LangGraph）
│   ├── publishing/workflows.py#   多平台批量刊登（Temporal durable workflow）
│   └── customer_service/      #   智能客服（示范跨域调用纪律）
└── workers/main.py            # ② Worker 进程入口  (python -m app.workers.main)
migrations/                    # Alembic 迁移（版本化，up/down）
├── env.py                     #   连 admin 连接；URL 取自 config
└── versions/
    ├── 0001_chat_setup.py     #   chat 域表 + RLS（基线，无 pgvector 依赖）
    └── 0002_knowledge_base.py #   pgvector + kb 表 + RLS（需装 pgvector）
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
- **业务长流程**（多平台刊登、要持久化/重试/恢复）→ Temporal，只在 worker 里
  （`publishing/workflows.py`）。

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
- **uv**（Python 依赖/运行）、**Postgres 14+**、**Node + pnpm**（前端）
- 可选：**pgvector** 扩展（仅 `knowledge_base` 域需要，chat 不需要）；
  **LiteLLM** 网关（真实 LLM 回答需要，否则路由/检索仍可测）

### 1. 配置
```bash
cp .env.example .env
# 编辑 .env：把 database_admin_url 的 <superuser> 改成本机 PG 超级用户
# （macOS Homebrew/Postgres.app 通常是你的 OS 用户名，本地多为 trust 认证）
```

### 2. 后端
```bash
uv sync                              # 装依赖（含 dev）
uv run python scripts/db_bootstrap.py  # 建库 xborder + app 角色 + 授权（幂等，对标 rails db:create）
uv run alembic upgrade 0001_chat     # 迁移到 chat 基线（不需 pgvector）
# 装了 pgvector 才跑全量： uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
```

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
含规则检索契约、chat 路由全链路（单测内 monkeypatch gateway 作替身）、/auth、
真 HTTP e2e（打真 PG + RLS，PG 不可达则自动 skip）。
