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
migrations/
└── 001_rls_setup.sql          # ★ 数据库层强制租户隔离（RLS 策略）
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

## 依赖（示意）

```
fastapi uvicorn sqlalchemy[asyncio] asyncpg pgvector
pydantic-settings pyjwt httpx redis
langgraph temporalio langfuse
```
