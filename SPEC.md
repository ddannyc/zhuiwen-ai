# SPEC：chat 真实流式输出

> 目标：把 chat SSE 的**假流式**（生成完整 reply 再 `_chunk`+`sleep` 假打字）换成
> **真流式**（LLM 边生成边吐 token）。状态：待确认，批准后进 /plan。
> （上一份 SPEC——rules_kb pgvector 升级——已完成并合入 main，设计存 git 历史。）

---

## 0. 决策（已定）

- **范围**：全部文本回复走真流式（answer / analyze / 非空 rules_search 综述）。
- **守卫调和**：**预判 + 安全才流** —— 能事前定的守卫（空检索）不流；可能触发文本擦除的
  守卫（假引用 / 泄露）改为**流式中增量拦截**，不事后覆盖。
- **降级**：流式调用异常 → 回退现有非流式 `chat()` 一次性生成（不让对话挂）。

---

## 1. 现状（要替换的）

`chat/service.converse_stream`：
1. `_run` 跑完整 LangGraph agent → 完整 `reply`（`gateway.chat` / `chat_with_tools` 均非流式）。
2. 全部守卫（空检索覆盖 / 假引用擦除 / 泄露擦除）应用在**完整 reply** 上。
3. `for delta in _chunk(reply, 10): yield token; sleep(0.02)` —— **假打字**。

终答从哪来：`route` 节点 `chat_with_tools`；首轮若返回 tool_call → `tool_exec` 跑工具
（rules_search 等）→ 回灌结果 → route 第 2 轮 `chat_with_tools` 出 `msg.content` = 用户可见终答。
**只有这最后一次内容生成需要流式**；工具路由/执行不流。

---

## 2. 目标架构

```
converse_stream:
  ① tool_running 占位（同现在）
  ② 跑 agent 到「工具执行完、待生成终答」——非流式（路由+工具）
  ③ 终答生成：调 gateway 新增 chat_stream（litellm stream=True）→ 真 token 流
     - 边收 delta 边过【流式守卫】（见 §3）→ 安全则 yield token
     - 异常 → 回退非流式 chat() 一次拿全 + 直接发（降级）
  ④ payload(action) + done（同现在）
```

**gateway 新增**：`chat_stream(messages, model) -> AsyncIterator[str]`（litellm
`acompletion(stream=True)`，逐 chunk 取 `choices[0].delta.content`）。

**agent**：终答生成抽出可流式调用。方案二选一（/plan 定）：
- A. LangGraph `astream_events` 捕获最终 LLM 节点的 token 事件。
- B. agent 只跑到「工具结果就绪」，终答生成移到 service 用 `chat_stream` 直调（绕开最后一步图执行）。
  B 更直接、好控守卫，倾向 B。

---

## 3. 守卫调和（核心）

现 3 守卫（`chat/service._run`）与真流式的关系：

| 守卫 | 触发 | 可事前定？ | 真流式处理 |
|---|---|---|---|
| 空检索覆盖（rules_search empty → 安全话术） | 检索返回空 | ✅ 工具执行后、生成前就知 | **不流**：直接发 payload，前端 RuleCiteCard 渲空文案（已实现） |
| 假引用擦除（`_VERIFY_CLAIM_RE`：非检索却称"据规则库/官方"） | 终答文本含虚假背书 | ❌ 需看文本 | **流式增量拦截**：累积 buffer 持续匹配；命中 → 停流 + correction（换 `_FALSE_CITE_FALLBACK`） |
| 泄露擦除（`_LEAK_RE`：吐工具名/参数/计划） | 终答文本含内部编排 | ❌ 需看文本 | 同上：增量匹配，命中即停流 + correction（`_LEAK_FALLBACK`） |

**流式守卫机制**：
- 维护 running buffer，每收到 delta 追加后跑 `_LEAK_RE` /（非检索路径才）`_VERIFY_CLAIM_RE`。
- 命中 → **立即停止继续吐 token**，发 `replace` 事件让前端用 fallback 文案替换已显示内容，
  再 payload+done。（仅"出事才覆盖"，正常流不覆盖——区别于旧的每次假打字。）
- 这是「预判+安全才流」：守卫**全程在线**，不是事后补。绝大多数回复不触发，流畅；少数触发即拦。
- 落库的 assistant 文本用守卫后的最终文本（命中则 fallback）。

**前端**：新增处理 `replace` 事件（清空当前流式 content 换 fallback）；正常路径无 replace。

---

## 4. 范围

**In**
- `gateway.chat_stream`（litellm 流式）。
- `converse_stream` 改真流式 + 流式守卫 + 降级。
- 终答生成可流式化（agent 方案 B 倾向）。
- 前端 ChatPane 处理真 token + `replace` 事件。
- 删 `_chunk` 假打字 + `sleep`。

**Out**
- 结构化 action（box_list / collect_products / 空 rules_search）不流（短/卡片）。
- 工具路由/执行流式（无意义）。
- 改动检索/采集/rules_kb 逻辑。

---

## 5. 命令

```bash
docker compose up -d && uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000   # 真流式 SSE
uv run python -m app.workers.main
uv run pytest -q                                   # 含流式单测
cd web && pnpm dev                                 # 前端验真打字
```

---

## 6. 项目结构

```
app/shared/llm/gateway.py        # + chat_stream(messages, model) -> AsyncIterator[str]
app/domains/chat/service.py      # converse_stream 改真流式 + 流式守卫 + 降级；删 _chunk
app/domains/chat/agent.py        # 终答生成可流式化（方案 B：暴露「工具就绪」中间态）
app/domains/chat/stream_guard.py # ★ 新：增量守卫（buffer + _LEAK_RE/_VERIFY_CLAIM_RE）
web/src/components/ChatPane.tsx   # 处理真 token + replace 事件
web/src/lib/contract.ts          # + replace 事件类型
```

---

## 7. 代码风格

- gateway 仍是 LLM 唯一出口；service/agent 不直接调 litellm。
- 流式守卫与现 `_run` 守卫**同源**（复用 `_LEAK_RE`/`_VERIFY_CLAIM_RE`/fallback 常量），
  不另写一套规则，防漂移。
- SSE 事件对齐 `contract.ts`；新增 `replace` 事件，不破坏现有 token/payload/done。
- async generator 逐 delta yield，背压自然。

---

## 8. 测试策略

- **gateway.chat_stream**：mock litellm streaming → 断言逐 delta 产出 + 拼接完整。
- **converse_stream 真流式**：mock chat_stream 产 N 段 → 断言 yield N 个 token（非 `_chunk` 定长）。
- **流式守卫拦截**：mock 终答流出含泄露/假引用片段 → 断言中途停流 + 发 `replace`(fallback) + 落库为 fallback。
- **空检索仍不流**（回归现已修）：empty rules_search → 无 token，只 payload。
- **降级**：mock chat_stream 抛错 → 回退非流式 chat()，仍出完整回复 + done。
- 既有 chat 测试（事件顺序/守卫/落库）全绿。
- 前端：手验真打字（token 逐字到达，非整段跳出）+ 触发泄露时 replace。

---

## 9. 边界

**Always**
- 用户最终看到/落库的必须是**守卫后**文本（流式守卫命中即停+替换）。
- 流式异常必降级，绝不让对话卡死或半截无终止。
- gateway 唯一 LLM 出口；守卫规则单一来源。

**Ask first**
- 是否给 `replace` 事件加动画/提示（UX）。
- 流式守卫 buffer 检查粒度（每 delta vs 每句）——性能/拦截及时性权衡。

**Never**
- 不在前端先渲未守卫文本再无声替换成"正常"内容（只允许"出事→fallback"的可见纠正）。
- 不流结构化卡片 action 的内部数据。
- 不绕 gateway 直调 provider。

---

## 10. 待决（/plan 时定）
- agent 终答流式化走 A（astream_events）还是 B（service 直调 chat_stream）。倾向 B。
- 流式守卫粒度（每 delta / 每句 / 每 N 字）。
- `replace` 事件前端呈现（直接换 vs 渐隐）。

---

*确认本 SPEC 后进 /plan 做任务分解。*
