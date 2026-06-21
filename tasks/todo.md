# TODO：chat 真实流式输出

> 配套 `tasks/plan.md`。检查点(C*)是阶段闸门。

## Phase 0 — gateway 流式原语
- [x] T0.1 `gateway.chat_stream`（litellm stream=True → AsyncIterator[str]）
- [x] **✅ C0**：逐 delta 产出、拼接完整（mock litellm）

## Phase 1 — agent 拆 prepare()
- [x] T1.1 `agent.prepare()`：路由/工具 与 终答生成解耦；`_run` 复用之
- [x] **✅ C1**：prepare 分流正确（需生成 vs 卡片/模板）；既有 chat 测试全绿

## Phase 2 — converse_stream 真流式 + 降级 ★MVP
- [x] T2.1 真 token 流：needs_gen → chat_stream 逐 delta yield；删 _chunk+sleep
- [x] T2.2 降级：chat_stream 出错 → 回退非流式 chat()
- [x] **✅ C2**：真 token 随 LLM 到达；空检索不流；降级出全文 ← 可演示里程碑

## Phase 3 — 流式守卫
- [x] T3.1 `stream_guard.py`：buffer 增量跑 _LEAK_RE/_VERIFY_CLAIM_RE（复用常量）
- [x] T3.2 converse_stream 接守卫：命中→停流+replace 事件+落库 fallback
- [x] **✅ C3**：泄露/假引用流式拦截；守卫后文本落库

## Phase 4 — 前端
- [x] T4.1 ChatPane 真 token + replace 事件处理；contract.ts +replace 类型
- [x] **✅ C4**：浏览器真打字 + replace 呈现

## Phase 5 — 清理 + 测试硬化
- [ ] T5.1 删 _chunk 假打字残留；全套 pytest + 前端 build
- [ ] **✅ C5**：全绿；真流式唯一路径（降级除外）

## 待决
- [x] 终答流式化 → **锁 B**（service 直调 chat_stream，agent prepare() 出 gen_messages）
- [ ] 守卫粒度（每 delta / 每句）—— 先每 delta
- [ ] replace 前端呈现（直接换 / 渐隐）—— P4 定

## 执行顺序
P0 → P1(C1) → P2(C2 MVP) → P3 → P4 → P5
