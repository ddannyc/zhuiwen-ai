# 实施计划：chat 真实流式输出

> 源 `SPEC.md`。垂直切片 + 阶段检查点。待评审，批准后逐阶段执行。
> （上一份 plan——rules_kb pgvector——已完成合入 main，存 git 历史。）

## 0. 关键前提
- 假流式 = `_run` 出完整 reply → `_chunk`+`sleep` 假打字。真流式 = LLM 边生成边吐 token。
- 终答来自 agent `route` 节点的内容生成（直接答 or 工具结果回灌后第2轮）。
- 守卫：空检索**事前定**（不流，已实现）；假引用/泄露**流式中增量拦截**（不事后覆盖）。
- 方案 B（已倾向）：agent 拆出 `prepare()`（跑路由+工具，返回「是否需 LLM 生成 + 生成用 messages」），
  service 对需生成的路径用 `chat_stream` 真流式。

## 1. 依赖图
```
P0 gateway.chat_stream ──┐
                         ▼
P1 agent.prepare() 拆分（路由/工具 与 终答生成解耦）
                         ▼
P2 converse_stream 真流式 + 降级（端到端真 token）★MVP
                         ▼
P3 流式守卫（增量拦截 + replace 事件）
                         ▼
P4 前端（真 token 渲染 + replace 处理）
                         ▼
P5 清理 + 测试硬化（删 _chunk）
```
切片纪律：每阶段一条端到端可验路径，非按层堆。

## 2. 阶段与任务

### Phase 0 — gateway 流式原语
**T0.1 `gateway.chat_stream`**
- 目标：`chat_stream(messages, model) -> AsyncIterator[str]`，litellm `acompletion(stream=True)`，逐 chunk 取 `choices[0].delta.content`（空块跳过）。
- 验收：mock litellm 流 → 逐 delta 产出；拼接 == 完整文本。
- 验证：`pytest tests/test_gateway_stream.py`。
- **✅ C0**：chat_stream 单测过；gateway 仍是 LLM 唯一出口。

### Phase 1 — agent 拆 prepare()
**T1.1 `prepare()`：路由/工具 与 终答生成解耦**
- 目标：新增 `agent.prepare(...) -> {action, needs_gen, gen_messages|static_reply, tools_used}`。跑 route + tool_exec，返回：
  - 需 LLM 生成（answer / analyze / 非空 rules_search 综述）→ `needs_gen=True` + `gen_messages`（history+工具结果，供流式终答）。
  - 模板/卡片（box_list / collect_products / 空 rules_search）→ `needs_gen=False` + `static_reply`。
- `_run` 改为 `prepare()` +（非流式时）`chat()` 生成，保持现有 `converse` 行为不变。
- 验收：各路径 prepare 返回正确 shape；现有 chat 测试（converse/事件/守卫/落库）全绿。
- **✅ C1**：prepare 分流正确；既有非流式路径回归绿。

### Phase 2 — converse_stream 真流式 + 降级 ★MVP
**T2.1 真 token 流**
- 目标：`converse_stream` → `prepare()` → `needs_gen` 时 `chat_stream(gen_messages)` 逐 delta `yield token`；
  否则发 `static_reply`（卡片路径，不流）。空检索仍不流（回归）。删 `_chunk`+`sleep`。
- 验收：mock chat_stream 产 N 段 → 恰 N 个 token 事件（非定长 `_chunk`）；事件序 tool_running→action→token*→payload→done。
**T2.2 降级**
- 目标：`chat_stream` 抛错 → 回退非流式 `chat()` 一次拿全 + 整段发（仍 token 事件 + done）。
- 验收：mock chat_stream 抛错 → 仍出完整回复 + done，不挂。
- **✅ C2**（端到端 MVP）：真实 API 问 Ozon 佣金 → token 随 LLM 节奏到达（curl 观察）；空检索无 token；流式出错降级出全文。

### Phase 3 — 流式守卫
**T3.1 `stream_guard.py` 增量守卫**
- 目标：维护 running buffer，每 delta 后跑 `_LEAK_RE` /（非检索路径）`_VERIFY_CLAIM_RE`（复用现有常量，单一来源）。命中 → 停流。
**T3.2 converse_stream 接守卫 + replace**
- 目标：守卫命中 → 停 token、发 `replace` 事件（fallback：`_LEAK_FALLBACK`/`_FALSE_CITE_FALLBACK`）→ payload+done；落库为 fallback。正常路径无 replace。
- 验收：mock 终答流出含泄露片段 → 中途停 + 发 replace(fallback) + 落库 fallback；干净文本无 replace。
- **✅ C3**：泄露/假引用流式拦截过；用户/落库均守卫后文本。

### Phase 4 — 前端
**T4.1 真 token + replace**
- 目标：`ChatPane` token 累积（已有）；加 `replace` 事件（清空当前流式 content 换 fallback）；`contract.ts` + replace 事件类型。
- 验收：浏览器真打字（token 逐字，非整段跳）；触发泄露 → 可见纠正为 fallback。
- **✅ C4**：前端真流式手验 + replace 呈现。

### Phase 5 — 清理 + 测试硬化
**T5.1 清理 + 全套**
- 目标：删 `_chunk` 假打字残留 + `asyncio.sleep`；确认无路径回退假流式（除降级）。
- 验收：`grep _chunk` 空（或仅历史）；`pytest -q` 全绿（gateway流/真流式/守卫拦截/降级/空检索/既有）；前端 `pnpm build`。
- **✅ C5**：全绿；真流式为唯一路径（降级除外）。

## 3. 检查点汇总
| CP | 关口 | 判据 |
|---|---|---|
| C0 | 流式原语 | chat_stream 逐 delta、拼接完整 |
| C1 | prepare 拆分 | 分流正确、既有回归绿 |
| C2 | 真流式 MVP | 真 token 随 LLM 到达、空检索不流、降级出全文 |
| C3 | 流式守卫 | 泄露/假引用拦截、守卫后文本落库 |
| C4 | 前端 | 真打字 + replace |
| C5 | 硬化 | 全绿、无假流式残留 |

## 4. 风险/回滚
- agent 拆 prepare 动核心路径 → C1 强制既有回归绿兜底。
- 流式守卫及时性 vs 性能（每 delta 跑正则）→ 粒度可调，先每 delta，C3 看开销。
- 守卫命中 replace = 受控可见纠正（非旧的每次覆盖），仅出事触发。
- DashScope 流式不稳 → 降级非流式兜底（C2）。
- 改的是 chat 流式输出层（增 chat_stream + 重构 converse_stream），git 可回退。

## 5. 执行顺序
P0 → P1（C1 回归绿）→ P2（C2 MVP 可演示）→ P3 → P4 → P5。C2 是第一个可演示里程碑（真打字）。
