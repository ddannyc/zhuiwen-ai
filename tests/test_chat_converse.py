"""ChatService.converse / converse_stream 全链路测试。

用假 LLM（mock gateway.chat_with_tools）+ 真 RulesKbService（读 jsonl）+ 内存假 repo，
验证编排管线：消息进 → 模型路由到工具 → 执行工具(真查库) → 回灌 → 落库 → 返回。
产出对齐前端 contract.ts 的结构化 ChatAction + SSE 事件流。
"""
import json
import types

import pytest

import app.domains.chat.service as service_mod
from app.domains.chat.service import ChatService


class FakeRepo:
    def __init__(self, session):
        self.messages: list = []

    async def create_conversation(self, user_id, title="新对话"):
        return types.SimpleNamespace(id="conv-1", user_id=user_id, title=title, created_at=None)

    async def add_message(self, conversation_id, role, content, action=None):
        m = types.SimpleNamespace(
            id=f"m{len(self.messages)+1}", conversation_id=conversation_id,
            role=role, content=content, action=action, created_at=None,
        )
        self.messages.append(m)
        return m

    async def list_messages(self, conversation_id, limit=50):
        return [m for m in self.messages if m.role in ("user", "assistant")][:limit]

    async def recent_messages(self, conversation_id, limit):
        return [m for m in self.messages if m.role in ("user", "assistant")][-limit:]

    async def update_conversation_title(self, conversation_id, title):
        self.title = title


def _tool_call(name, args):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-1", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}


def _answer_call():
    """路由到 answer 标记工具（方案B）：表示"直接答"，终答由 chat 生成。"""
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-a", "type": "function",
         "function": {"name": "answer", "arguments": "{}"}}]}


@pytest.fixture
def captured(monkeypatch):
    from app.domains.chat.prompts import _TITLE_SYS

    calls: list[list[dict]] = []       # 每次 chat_with_tools(路由) 的 messages
    responses: list[dict] = []         # 路由决策序列（_tool_call/_answer_call）；耗尽则默认 answer
    choices: list = []                 # 每次路由的 tool_choice（验合规召回闸强制）
    chat_answers: list[str] = []       # 终答 chat 生成的文本队列（耗尽给默认）
    gen_calls: list[list[dict]] = []   # 终答生成（chat/chat_stream）收到的 messages（验工具结果回灌）

    async def fake_cwt(messages, tools, model="x", tool_choice=None, **kw):
        calls.append([dict(m) for m in messages])
        choices.append(tool_choice)
        i = len(calls) - 1
        return responses[i] if i < len(responses) else _answer_call()

    async def fake_chat(messages, model="x", **kw):
        sys = messages[0].get("content", "") if messages else ""
        if sys == _TITLE_SYS:           # 标题生成
            return "测试标题"
        gen_calls.append([dict(m) for m in messages])
        return chat_answers.pop(0) if chat_answers else "（生成的终答）"  # 终答生成（非流式/降级）

    async def fake_chat_stream(messages, model="x", **kw):  # 终答真流式
        gen_calls.append([dict(m) for m in messages])
        text = chat_answers.pop(0) if chat_answers else "（生成的终答）"
        for i in range(0, len(text), 8):  # 切小块模拟逐 token 到达
            yield text[i:i + 8]

    monkeypatch.setattr("app.domains.chat.agent.chat_with_tools", fake_cwt)
    monkeypatch.setattr(service_mod, "chat", fake_chat)
    monkeypatch.setattr(service_mod, "chat_stream", fake_chat_stream)
    monkeypatch.setattr(service_mod, "ChatRepository", FakeRepo)
    return types.SimpleNamespace(calls=calls, responses=responses, choices=choices,
                                 chat_answers=chat_answers, gen_calls=gen_calls)


def _service():
    return ChatService(session=object(), model="test-model")


# ---- converse：结构化 action ----
async def test_converse_rules_search_structured_action(captured):
    captured.responses.append(_tool_call("rules_search", {"query": "取消率封号", "platform": "ozon"}))

    out = await _service().converse("conv-1", "Ozon 取消率多少会被封号")

    assert out["action"]["type"] == "rules_search"
    assert out["action"]["empty"] is False
    cites = out["action"]["cites"]
    assert cites and cites[0]["source_url"].startswith("https://") and cites[0]["version"]
    # 工具结果真回灌给终答生成（gen_messages）
    tool_msgs = [m for m in captured.gen_calls[0] if m.get("role") == "tool"]
    assert "来源" in tool_msgs[0]["content"]


async def test_converse_rules_search_empty_no_hallucination(captured):
    captured.responses.append(_tool_call("rules_search", {"query": "玩具含磁铁", "platform": "amazon"}))

    out = await _service().converse("conv-1", "亚马逊美国站玩具能卖含磁铁吗")

    assert out["action"]["type"] == "rules_search"
    assert out["action"]["empty"] is True
    assert out["action"]["cites"] == []
    # 空检索事前定：不生成终答，确定性覆盖为安全话术（不臆测）。
    from app.domains.chat.agent import EMPTY_RULES_FALLBACK
    assert out["reply"] == EMPTY_RULES_FALLBACK


async def test_converse_box_list_payload(captured):
    captured.responses.append(_tool_call("box_list", {"limit": 10}))

    out = await _service().converse("conv-1", "列出采集箱前10个")
    assert out["action"]["type"] == "box_list"
    assert "rows" in out["action"]  # 桩返回空 rows，但结构在


async def test_converse_collect_products_job_id(captured):
    captured.responses.append(_tool_call("collect_products", {"keywords": ["杯子"], "perKw": 20}))

    out = await _service().converse("conv-1", "按马来西亚蓝海自动采集每词20个")
    assert out["action"]["type"] == "collect_products"
    assert out["action"]["job_id"]


async def test_converse_scrubs_leaked_tool_plan(captured):
    # 模型把内部工具名/参数/调用计划当回复吐出 → 必须被兜底替换，不泄露给用户。
    captured.chat_answers.append(
        '请立即调用 rules_search 工具，"query": "玩具 磁铁 认证"，是否需要我帮你发起该查询？')
    out = await _service().converse("conv-1", "玩具磁铁能卖吗")
    assert "rules_search" not in out["reply"]
    assert '"query"' not in out["reply"]
    assert "发起该查询" not in out["reply"]
    assert "抱歉" in out["reply"]


async def test_compliance_query_forces_rules_search(captured):
    # 合规召回闸：含合规信号的问题，首轮强制 tool_choice=rules_search（不让模型漏路由）。
    captured.responses.append(_tool_call("rules_search", {"query": "促销规则", "platform": "ozon"}))
    await _service().converse("conv-1", "ozon 促销有什么规则要注意")
    assert captured.choices[0] == {"type": "function", "function": {"name": "rules_search"}}


async def test_false_citation_scrubbed(captured):
    # 假引用闸：模型没真检索（action=answer）却声称"官方规则库" → 替换为安全话术。
    captured.chat_answers.append("根据 Ozon 官方规则库，促销折扣不得低于 85%。")
    out = await _service().converse("conv-1", "随便聊聊天气")  # 无合规词 → 不强制
    assert out["action"] == {"type": "answer"}
    assert "官方规则库" not in out["reply"]
    assert "未能从平台规则库取证" in out["reply"]


async def test_converse_clean_reply_not_scrubbed(captured):
    captured.chat_answers.append("美国市场宠物用品蓝海明显，建议聚焦智能喂食器。")
    out = await _service().converse("conv-1", "选品建议")
    assert out["reply"] == "美国市场宠物用品蓝海明显，建议聚焦智能喂食器。"


async def test_converse_plain_answer(captured):
    captured.chat_answers.append("选品思路……")
    out = await _service().converse("conv-1", "给点选品思路")
    assert out["action"] == {"type": "answer"}
    assert out["reply"] == "选品思路……"


async def test_first_message_generates_title(captured):
    svc = _service()
    await svc.converse("conv-1", "第一条消息")
    assert svc.repo.title == "测试标题"  # 首条触发 LLM 标题


async def test_second_message_does_not_regenerate_title(captured):
    svc = _service()
    await svc.converse("conv-1", "第一条")
    svc.repo.title = "SENTINEL"          # 标记，第二条不应覆盖
    await svc.converse("conv-1", "第二条")
    assert svc.repo.title == "SENTINEL"  # 非首条不再生成标题


async def test_history_uses_recent_not_earliest(captured):
    # 长对话（>HISTORY_LIMIT）历史必须取最近、含最新用户消息，否则模型反复重问。
    from app.domains.chat.prompts import HISTORY_LIMIT
    svc = _service()
    for i in range(6):
        await svc.repo.add_message("c", "user", f"u{i}")
        await svc.repo.add_message("c", "assistant", f"a{i}")
    hist = await svc._build_llm_history("c")
    contents = [h["content"] for h in hist]
    assert len(hist) <= HISTORY_LIMIT
    assert "u5" in contents      # 最新用户消息在
    assert "u0" not in contents  # 最早的被挤出（取最近而非最早）


async def test_converse_persists_user_and_assistant(captured):
    captured.responses.append(_tool_call("rules_search", {"query": "费用", "platform": "ozon"}))

    svc = _service()
    await svc.converse("conv-1", "Ozon 佣金多少")

    msgs = svc.repo.messages
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].action["type"] == "rules_search"  # 结构化 action 落库


# ---- converse_stream：SSE 事件序列 ----
async def test_converse_stream_empty_rules_no_token(captured):
    """空检索：流式不发 token——守卫文案由前端 RuleCiteCard(empty) 渲，避免与流式文本
    两条「未找到」重叠/覆盖（用户报的 bug）。"""
    captured.responses.append(_tool_call("rules_search", {"query": "玩具含磁铁", "platform": "amazon"}))

    events = [ev async for ev in _service().converse_stream("conv-1", "亚马逊玩具含磁铁能卖吗")]
    names = [e["event"] for e in events]
    payload = [e for e in events if e["event"] == "payload"][0]

    assert payload["data"]["empty"] is True
    assert "token" not in names, "空检索不应流式 token（前端卡片渲空文案）"
    assert "payload" in names and names[-1] == "done"


async def test_converse_stream_event_order(captured):
    captured.responses.append(_tool_call("rules_search", {"query": "费用", "platform": "ozon"}))

    events = [ev async for ev in _service().converse_stream("conv-1", "Ozon 佣金")]
    names = [e["event"] for e in events]

    # 两段式顺序：tool_running(占位) → action → tool_running* → token* → payload → done
    assert names[0] == "tool_running"  # 立即占位反馈（消除空白）
    assert names[-1] == "done"
    assert names[-2] == "payload"
    assert "action" in names and "token" in names
    # 占位 tool_running 在 action 与 token 之前
    assert names.index("tool_running") < names.index("action") < names.index("token")
    # payload 携带完整结构化 action
    payload = [e for e in events if e["event"] == "payload"][0]
    assert payload["data"]["type"] == "rules_search"
    # done 带落库 message_id
    assert events[-1]["data"]["message_id"]


async def test_converse_stream_real_tokens_from_chat_stream(captured):
    """T2：token 来自 chat_stream（真流式增量），且滑动尾巴下长回复分多段发出、可拼回原文。"""
    full = "一" * 200  # 远超 GUARD_TAIL(48) → 安全前缀分多段发，留尾巴不发
    captured.chat_answers.append(full)
    events = [ev async for ev in _service().converse_stream("conv-1", "随便聊")]
    tokens = [e["data"]["delta"] for e in events if e["event"] == "token"]
    assert len(tokens) > 1               # 增量多段（非一次性整段）
    assert "".join(tokens) == full       # 拼回无损（含末尾尾巴）


async def test_converse_stream_fallback_on_stream_error(captured, monkeypatch):
    """T2.2：chat_stream 抛错 → 降级非流式 chat() 一次拿全 + 整段发，仍 done。"""
    async def boom(messages, model="x", **kw):
        raise RuntimeError("stream down")
        yield  # pragma: no cover —— 使其成为 async generator

    monkeypatch.setattr(service_mod, "chat_stream", boom)
    captured.chat_answers.append("降级生成的完整回复")
    events = [ev async for ev in _service().converse_stream("conv-1", "随便聊")]
    names = [e["event"] for e in events]
    tokens = "".join(e["data"]["delta"] for e in events if e["event"] == "token")
    assert tokens == "降级生成的完整回复"   # 来自降级 chat()
    assert names[-1] == "done" and "payload" in names


async def test_converse_stream_leak_replace_midstream(captured, monkeypatch):
    """T3.2：流式终答中途吐泄露 → 停流 + 发 replace(fallback)，落库为 fallback，不泄露。"""
    async def leaky_stream(messages, model="x", **kw):
        for d in ["我先", "调用 rules", "_search 工具帮你查"]:  # 凑出 rules_search → 命中
            yield d

    monkeypatch.setattr(service_mod, "chat_stream", leaky_stream)
    svc = _service()
    events = [ev async for ev in svc.converse_stream("conv-1", "随便聊")]
    names = [e["event"] for e in events]
    replace = [e for e in events if e["event"] == "replace"]

    assert replace and "抱歉" in replace[0]["data"]["text"]
    # 泄露片段不应作为最终用户文本：落库为 fallback，不含 rules_search
    assistant = [m for m in svc.repo.messages if m.role == "assistant"][0]
    assert "rules_search" not in assistant.content and "抱歉" in assistant.content
    assert names[-1] == "done"


async def test_converse_stream_clean_no_replace(captured):
    """正常回复不发 replace（仅出事才可见纠正）。"""
    captured.chat_answers.append("美国宠物用品蓝海明显，建议聚焦智能喂食器。")
    events = [ev async for ev in _service().converse_stream("conv-1", "选品建议")]
    assert not any(e["event"] == "replace" for e in events)


async def test_converse_stream_double_failure_emits_error(captured, monkeypatch):
    """review #4：流式 + 非流式降级都挂 → 发 error 事件，仍正常 payload+done，不留半截。"""
    async def boom_stream(messages, model="x", **kw):
        raise RuntimeError("stream down")
        yield  # pragma: no cover

    async def boom_chat(messages, model="x", **kw):
        raise RuntimeError("chat down")

    monkeypatch.setattr(service_mod, "chat_stream", boom_stream)
    monkeypatch.setattr(service_mod, "chat", boom_chat)
    events = [ev async for ev in _service().converse_stream("conv-1", "随便聊")]
    names = [e["event"] for e in events]
    assert "error" in names
    assert "payload" in names and names[-1] == "done"


async def test_converse_stream_clean_persists_full_streamed_text(captured):
    """干净流式：落库 == 完整流式文本（guard.text）。防静默丢库（test-engineer #2）。"""
    full = "美国宠物用品蓝海明显，建议聚焦智能喂食器，毛利空间可观。" * 3
    captured.chat_answers.append(full)
    svc = _service()
    _ = [ev async for ev in svc.converse_stream("conv-1", "选品建议")]
    assistant = [m for m in svc.repo.messages if m.role == "assistant"][0]
    assert assistant.content == full


async def test_converse_stream_degrade_after_partial_uses_replace(captured, monkeypatch):
    """Blocker#1：流式已吐部分 token 后才报错 → 降级用 replace 覆盖（非 token 追加，免重复）。"""
    async def partial_then_boom(messages, model="x", **kw):
        for d in ["一" * 60, "二" * 60]:  # 先吐够（过 GUARD_TAIL）让 emitted>0
            yield d
        raise RuntimeError("mid-stream drop")

    monkeypatch.setattr(service_mod, "chat_stream", partial_then_boom)
    captured.chat_answers.append("降级后的完整正文")  # chat() 降级返回
    events = [ev async for ev in _service().converse_stream("conv-1", "随便聊")]
    assert any(e["event"] == "token" for e in events)      # 已发过部分
    assert any(e["event"] == "replace" for e in events)    # 降级用 replace 覆盖
    assert not any(e["event"] == "token" and "降级" in e["data"]["delta"] for e in events)  # 降级不走 token


async def test_converse_stream_leak_never_emitted_as_token(captured, monkeypatch):
    """H1：泄露内容在守卫看清前不传输——token 里绝不出现泄露片段，只有 replace。"""
    async def leaky(messages, model="x", **kw):
        # 干净前缀（过 GUARD_TAIL 才会发），随后凑出泄露
        yield "安全前缀" * 20
        yield "，接着我会调用 rules"
        yield "_search 工具查询"

    monkeypatch.setattr(service_mod, "chat_stream", leaky)
    events = [ev async for ev in _service().converse_stream("conv-1", "随便聊")]
    tokens = "".join(e["data"]["delta"] for e in events if e["event"] == "token")
    assert "rules_search" not in tokens and "rules" not in tokens  # 泄露片段从未作 token 发出
    assert any(e["event"] == "replace" for e in events)


async def test_prepare_multiple_tool_calls_one_round(captured):
    """一轮内多 tool_call：全部执行、保序、action 取首个。"""
    captured.responses.append({"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "box_count", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "box_list", "arguments": "{}"}}]})
    await _service().converse("conv-1", "采集箱多少个并列出")
    # 一轮内两个 tool_call 都执行 → 两条工具结果回灌终答生成（保序）。
    tool_msgs = [m for m in captured.gen_calls[0] if m.get("role") == "tool"]
    assert [m["name"] for m in tool_msgs] == ["box_count", "box_list"]


async def test_prepare_provider_ignores_required_returns_content(captured):
    """provider 不认 tool_choice=required，返回纯 content 无 tool_calls → 优雅当直接答。"""
    captured.responses.append({"role": "assistant", "content": "我直接回答", "tool_calls": None})
    captured.chat_answers.append("终答")
    out = await _service().converse("conv-1", "随便聊")
    assert out["action"] == {"type": "answer"}
    assert out["reply"] == "终答"


async def test_prepare_rules_search_missing_query_falls_back(captured):
    """rules_search 漏传 query → 回退用户消息检索，不假"未找到"。"""
    captured.responses.append(_tool_call("rules_search", {"platform": "ozon"}))  # 无 query
    out = await _service().converse("conv-1", "ozon 佣金费用规则")
    assert out["action"]["type"] == "rules_search"  # 跑了检索（用 user_message）


async def test_prepare_malformed_tool_args_no_crash(captured):
    """工具参数非法 JSON → args={}，不崩。"""
    captured.responses.append({"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "box_list", "arguments": "{not json"}}]})
    out = await _service().converse("conv-1", "列出采集箱")
    assert out["action"]["type"] == "box_list"


async def test_converse_stream_token_reassembles_reply(captured):
    full = "这是一段较长的回复用于验证 token 切块能拼回原文。" * 2
    captured.chat_answers.append(full)

    events = [ev async for ev in _service().converse_stream("conv-1", "随便聊")]
    tokens = "".join(e["data"]["delta"] for e in events if e["event"] == "token")
    assert tokens == full  # 切块无损


# ---- collect_products 工具 schema↔impl 契约（评审 #1）----
def _collect_schema() -> dict:
    from app.domains.chat.agent import TOOLS
    return next(t["function"] for t in TOOLS if t["function"]["name"] == "collect_products")


def test_collect_products_schema_only_advertises_wired_params():
    # schema 不得声明 impl 未消费的参数：否则模型会"设置"翻译/上架等实际不发生的动作，
    # 违反项目反幻觉纪律。t_collect_products 只消费 keywords / perKw / market。
    props = set(_collect_schema()["parameters"]["properties"])
    assert props == {"keywords", "perKw", "market"}, f"schema 暴露了未接线参数: {props}"


def test_collect_products_schema_exposes_market():
    # market 被 impl 消费 → 必须在 schema 里，否则模型永远填不进，market 列恒 None。
    assert "market" in _collect_schema()["parameters"]["properties"]


async def test_collect_products_passes_market_through(captured):
    # 行为契约：模型给的 market 透传到 SourcingService.start_collect，并进结构化 action。
    import types
    import app.domains.chat.agent as agent_mod

    recorded = {}

    async def fake_start_collect(*, tenant_id, keywords, per_kw, market):
        recorded.update(tenant_id=tenant_id, keywords=keywords, per_kw=per_kw, market=market)
        return {"job_id": "job-xyz", "mode": "ok"}

    captured.responses.append(_tool_call(
        "collect_products", {"keywords": ["杯子"], "perKw": 20, "market": "my"}))

    # 用假 SourcingService 注入，避免真连 Temporal/DB
    monkey = types.SimpleNamespace(start_collect=fake_start_collect)
    orig = agent_mod.SourcingService
    agent_mod.SourcingService = lambda session: monkey
    try:
        out = await _service().converse("conv-1", "按马来西亚采集每词20个")
    finally:
        agent_mod.SourcingService = orig

    assert out["action"]["type"] == "collect_products"
    assert out["action"]["job_id"] == "job-xyz"
    assert recorded["keywords"] == ["杯子"]
    assert recorded["per_kw"] == 20
    assert recorded["market"] == "my"  # market 真透传
