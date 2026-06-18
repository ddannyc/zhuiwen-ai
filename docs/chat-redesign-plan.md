# Chat 对话功能 — 在 XBorder AI 新架构上的实现方案

## 背景 Context

旧的 `tmp/zhuiwen_web.py`（单文件 Flask 风格 HTTP server）把对话功能写成一坨：纯问答、自然语言指令路由、视觉理解、6 类分析、飞书接入、采集任务队列全挤在一起，状态全是**进程内存数组**（重启即丢、多副本不共享、无租户隔离）。业务逻辑已反推到 `docs/chat-business-logic.md`。

新骨架 `app/` 是多租户模块化单体：域内 `router→service→repository` 分层、RLS 数据库层强制租户隔离、LLM 统一经 `shared/llm/gateway`、agent 推理循环用 LangGraph、业务长流程用 Temporal。目标：把旧 chat 业务逻辑**忠实重建为一个标准业务域** `app/domains/chat/`，消除内存态、纳入租户隔离与计费归因，同时守住三条架构纪律。

### 锁定的假设（可推翻）
1. **范围**：完整建 chat 域；下游 box/采集/上架经**域 service 接口**调用，缺失域给最小桩，不在本期重写。
2. **auto_collect** → **Temporal 工作流 + 浏览器桥**（替代旧内存队列，durable/可重试）。
3. **会话历史** → **Postgres 持久化 + RLS**（服务端管理，弃用前端携带）。
4. **飞书/多渠道** → 本期**只留适配器接缝**，不实现。
5. **规则类问答必须接知识库溯源**（见《规则知识库设计.md》）：chat agent 新增 `rules_search` 工具走 `rules_kb` 域 RAG，**禁止自由生成合规规则**。营销/选品建议仍可自由生成。本期 `rules_kb` 域可给最小桩，但路由与溯源契约定死。
6. **用户与租户**：`user_id` 为用户唯一标识，每用户归属一个 `tenant`；`tenant` 为组织隔离边界，**可含多员工**（将来一租户多员工，本期前端 mock 以一租户两账号演示）。归属/唯一条件用 `user_id`，**不用 `tenant_id`**。会话按 `user_id` 归属、按 `tenant_id`（RLS）隔离 —— 同租户不同员工互不可见对方会话。

## 旧逻辑 → 新架构 映射

| 旧 (`zhuiwen_web.py`) | 新落点 | 纪律 |
|---|---|---|
| `chat()` 纯问答 + 前端携带 history | `ChatService.ask()` + `conversations`/`messages` 表(RLS) | 历史入库，租户隔离 |
| `agent_act()` + `_ACT_SYSTEM` JSON 路由 + 正则短路 | `chat/agent.py` LangGraph 工具调用循环 | 模型自主调工具，弃用脆弱正则 |
| `_ali_chat()` DashScope 直连 | `shared/llm/gateway.chat()` (+ 新增 `chat_with_tools`) | LLM 唯一出口、按租户计费 |
| `analyze()` + `ANALYSIS_PROMPTS`(6类) | `ChatService.analyze()` 工具 + 端点 | 请求内同步 LLM 调用（仅营销/选品，可自由生成） |
| （旧版无）规则/合规类问答 | agent `rules_search` 工具 → `rules_kb` 域 RAG | **强制 metadata 过滤 + 溯源(source_url+version) + 检索不到说"不知道"**，杜绝幻觉 |
| `chat_vision()` Qwen-VL | `ChatService.vision()` 多模态消息走 gateway | 复用 gateway |
| `box.*` / `pipeline` 动作 | agent 工具 → `BoxService` 接口(桩) | 跨域调 service，不读表 |
| `auto_collect` + 内存 `_COLLECT_JOBS` + poll/done | `sourcing` 域 Temporal `CollectWorkflow` + `collect_jobs` 表(RLS) + 浏览器桥端点 | 长流程 Temporal，tenant_id 显式贯穿 |
| 飞书 webhook 复用 agent | `chat/channels/` 适配器接缝（本期空壳） | 渠道→租户映射后续 |
| `_usage_bump` 内存计数 | gateway 的 `metadata.tenant_id` → LiteLLM/Langfuse 归因 | 计费下沉基础设施 |

## 新增/改动文件

### 1. chat 域（核心，新建 `app/domains/chat/`）
- **`models.py`** — `Conversation`(id, tenant_id, user_id, title, created_at)、`Message`(id, tenant_id, conversation_id, role, content, action, created_at)。镜像 `knowledge_base/models.py`：`tenant_id` 不由应用填，靠 DB DEFAULT + RLS；`user_id` 为归属人，由应用按登录态填（会话归属边界，见假设 6）。
- **`repository.py`** — `ChatRepository`：`create_conversation(user_id)`、`add_message`、`list_messages(conversation_id, limit)`、`list_conversations(user_id, limit)`、`get_conversation`。无 `WHERE tenant_id`（租户隔离交 RLS 兜底）；但 `list_conversations` 显式 `WHERE user_id`（归属过滤，非租户键，不违背纪律），镜像 `knowledge_base/repository.py`。
- **`prompts.py`** — 迁移 `_CHAT_SYS`、`_ACT_SYSTEM`（重写为工具描述）、`ANALYSIS_PROMPTS` 6 类模板。纯常量。
- **`agent.py`** — LangGraph 路由 agent（对标 `listing/agent.py` 写法）。`build_chat_agent(session)` 编译 StateGraph：节点 `route`（模型决定调哪个工具）→ `tool_exec` → 循环/`END`。工具集：`box_list/box_count/box_delete/box_translate/box_list_tiktok`、`analyze`(营销/选品建议，可自由生成)、`rules_search`(平台规则/合规问答 → `rules_kb` 域 RAG，强制溯源)、`collect_products`(触发 Temporal)、回退 `answer`。**意图区分纪律**：凡涉及平台规则/类目准入/禁限售/处罚/费用/合规，必须走 `rules_search`，不得用 `analyze`/`answer` 自由编。工具实现调对应域 service。
- **`service.py`** — `ChatService`（域唯一公开接口）：
  - `converse(conversation_id, user_message) -> {reply, action}`：写 user message → 跑 agent → 写 assistant message + action → 返回。**对标 `/api/agent/act`**。
  - `ask(messages) -> str`：纯问答（对标旧 `chat()`），取最近 N 条历史拼接。
  - `analyze(keyword, atype) -> str`：6 类分析（对标旧 `analyze()`）。
  - `vision(message, images) -> str`：构造多模态 messages 走 gateway（对标 `chat_vision()`）。
- **`schemas.py`** — Pydantic 请求体。
- **`router.py`** — `APIRouter(prefix="/chat")`，`Depends(get_db)`：
  - `POST /chat/conversations` 建会话
  - `GET /chat/conversations/{id}/messages` 取历史
  - `POST /chat/conversations/{id}/messages` 发消息→`converse`（主入口）
  - `POST /chat/vision`、`POST /chat/analyze`
- **`channels/__init__.py`** — 渠道适配器接缝（docstring 说明飞书后续接入：解析 app/chat_id→tenant→复用 `ChatService.converse`），本期不实现。

### 2. LLM 网关扩展（`app/shared/llm/gateway.py`）
新增 `chat_with_tools(messages, tools, model) -> dict`：透传 `tools` 给 LiteLLM（OpenAI 兼容），返回完整 `message`（含 `tool_calls`）。保留 `metadata.tenant_id` 注入。**不引入 langchain-openai**，agent.py 在 LangGraph 节点内手动跑工具循环，守住「gateway 是 LLM 唯一出口」。现有 `chat()` 不动。

### 3. sourcing 域（auto_collect 长流程，新建 `app/domains/sourcing/`）
- **`workflows.py`** — Temporal `CollectWorkflow`：签名 `run(tenant_id, params)`，**tenant_id 显式贯穿**（README §"关于工作流"警告：跨进程不能用 ContextVar）。步骤：`enqueue_browser_task`(activity 写 `collect_jobs` 表) → `workflow.wait_condition` 等浏览器完成信号 → `score`/`translate`/`publish` activities 调下游域 service。浏览器实际抓取的 activity 标为集成桩。
- **`activities.py`** — activity 内 `current_tenant_id.set(tenant_id)` 后再开 `get_session()`，使 RLS 生效。
- **`models.py`** / **`router.py`** — `collect_jobs` 表(RLS) + 浏览器桥端点 `POST /sourcing/jobs/poll`（插件取任务，租户经 JWT）、`POST /sourcing/jobs/{id}/done`（回结果→ Temporal signal）。对标旧 `collect-job/poll|done`，但持久化 + 租户隔离。

### 4. 迁移（`migrations/`，镜像 `001_rls_setup.sql`）
- **`002_chat_setup.sql`** — `conversations`/`messages` 建表 + `tenant_id` DEFAULT `current_setting('app.current_tenant')` + `ENABLE/FORCE ROW LEVEL SECURITY` + `tenant_isolation` 策略。`conversations` 另加 `user_id` 列（应用填，归属人；非租户键，RLS 不管它）。
- **`003_sourcing_setup.sql`** — `collect_jobs` 同上 RLS 套路。

### 5. 进程接线
- **`app/main.py`** — 挂载 `chat_router`、`sourcing_router`（仿现有 `kb_router` 写法，取消注释式新增）。
- **`app/workers/main.py`** — 注册 `CollectWorkflow` + activities 到 Temporal worker（填充现有桩）。

### 6. 规则知识库集成（消除与《规则知识库设计.md》冲突①）
旧 chat 把所有电商问题都丢给模型自由生成；规则知识库要求规则类问答**强制 RAG + 溯源 + 时效守卫 + 检索不到说"不知道"**。两者直接冲突——chat 的自由问答会架空规则库的低幻觉设计。裁决：把规则库做成 chat agent 的一个工具域，按意图分流。
- **独立新域 `app/domains/rules_kb/`**，**不挤现有通用 `knowledge_base`**（其 `Document/Chunk`+纯 cosine 检索满足不了规则库的富 metadata + 混合检索 + 版本溯源需求，详见《规则知识库设计.md》第一/二部分）。本期可只建 `RulesKbService.search(query, filters) -> [{summary, source_url, version, confidence, last_verified_at}]` 接口 + 最小桩，schema/双索引/人工审核管线后续按那份设计落地。
- **chat agent 的 `rules_search` 工具**调 `RulesKbService.search`，并在生成回答时执行规则库的硬约束：① metadata 过滤（platform/site，杜绝跨平台串台）；② 每条结论附 `source_url`+`version`；③ 检索为空 → 回 "未找到相关规则，请以平台最新公告为准"，**不编**；④ 命中规则超 `last_verified_at` 阈值或 `confidence=low` → 回答附时效/非官方提示。
- **意图分流**（`agent.py` 路由节点的判定纪律）：合规规则（类目准入/禁限售/知识产权/处罚/费用/税务合规）→ `rules_search`；营销文案/选品蓝海/定价测算 → `analyze`/`answer` 自由生成。两类语气与免责声明不同。
- **命名隔离**：`sourcing` 域 = 商品采集（1688 选品）；`rules_kb` 的 connector = 规则文档抓取。两个"采集"语义不同，禁止合并。合规边界写明：商品采集限本店授权数据，规则采集遵守《规则知识库设计.md》红线（禁登录态抓后台、原文不整段转储）。

## 关键设计取舍
- **弃用旧正则短路**（`agent_act` 里 `re.search(...)` 启发式）：改用模型原生 tool-calling 判定意图，更准更简。代价：依赖 LiteLLM 后端模型支持 tools。
- **范围克制**：box/listing/TikTok 工具先给 `BoxService` 接口 + 最小桩，agent 工具签名定死，下游域真身后续接入即可，不在本期铺开。
- **采集仍是异步外包**：服务端只编排，真正抓取由浏览器插件经桥端点驱动；Temporal 提供持久化/重试/恢复，替代内存队列。

## 验证

**单测（新增 `tests/`，缺测试框架需先加 pytest + pytest-asyncio）**
- `ChatService.converse`：mock gateway 返回指定 tool_call，断言路由到对应工具 + 落库 user/assistant 两条 message + action 字段正确。
- 各 analyze 类型 → 断言取对了 prompt 模板。
- 租户隔离：两租户上下文各建会话，断言互不可见（RLS）。

**端到端（本地起 postgres + LiteLLM）**
1. admin 连接跑 `001/002/003` 迁移。
2. `uvicorn app.main:app`。
3. `issue_token(user_id, tenant_id)`（`app/shared/auth/jwt.py`）造 JWT。
4. `curl POST /chat/conversations` 建会话 → `POST /chat/conversations/{id}/messages {"message":"美国市场蓝海选品建议"}`，断言 `reply` 非空、`action=="answer"`。
5. 发 `"列出采集箱前10个"`，断言 `action=="box_list"` 命中工具路径。
   - 发 `"亚马逊美国站玩具类目能卖含磁铁的吗"`，断言 `action=="rules_search"`、回答带 `source_url`+`version`；规则库置空时断言回 "未找到…"、不编造（消除冲突①的回归测试）。
6. 发 `"按马来西亚蓝海自动采集每词20个"`，断言触发 `collect_products` → Temporal 启动 → `collect_jobs` 表有 pending 行。
7. `python -m app.workers.main` 起 worker；模拟插件 `POST /sourcing/jobs/poll` 取任务、`POST /sourcing/jobs/{id}/done` 回结果，断言 workflow 推进。
8. `POST /chat/vision` 传 base64 图，断言返回理解文本。

**纪律自查**：grep 确认 chat/sourcing 业务代码无 `WHERE tenant_id`、无 `current_tenant_id.set` 散落在 service 层（只许 middleware 与 Temporal activity 设置）、无跨域 import 对方 `repository`。
