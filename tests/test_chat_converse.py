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
        return [m for m in self.messages if m.role in ("user", "assistant")][-limit:]

    async def update_conversation_title(self, conversation_id, title):
        self.title = title


def _tool_call(name, args):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-1", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}


def _final(text):
    return {"role": "assistant", "content": text, "tool_calls": []}


@pytest.fixture
def captured(monkeypatch):
    calls: list[list[dict]] = []
    responses: list[dict] = []
    choices: list = []  # 每次调用的 tool_choice（验合规召回闸强制）

    async def fake_cwt(messages, tools, model="x", tool_choice=None, **kw):
        calls.append([dict(m) for m in messages])
        choices.append(tool_choice)
        return responses[len(calls) - 1]

    async def fake_chat(messages, model="x", **kw):  # _gen_title 用，避免真打网络
        return "测试标题"

    monkeypatch.setattr("app.domains.chat.agent.chat_with_tools", fake_cwt)
    monkeypatch.setattr(service_mod, "chat", fake_chat)
    monkeypatch.setattr(service_mod, "ChatRepository", FakeRepo)
    return types.SimpleNamespace(calls=calls, responses=responses, choices=choices)


def _service():
    return ChatService(session=object(), model="test-model")


# ---- converse：结构化 action ----
async def test_converse_rules_search_structured_action(captured):
    captured.responses.append(_tool_call("rules_search", {"query": "取消率封号", "platform": "ozon"}))
    captured.responses.append(_final("根据知识库，取消率超 40% 会被封 3 天。"))

    out = await _service().converse("conv-1", "Ozon 取消率多少会被封号")

    assert out["action"]["type"] == "rules_search"
    assert out["action"]["empty"] is False
    cites = out["action"]["cites"]
    assert cites and cites[0]["source_url"].startswith("https://") and cites[0]["version"]
    # 工具结果真回灌给第 2 轮 LLM
    tool_msgs = [m for m in captured.calls[1] if m.get("role") == "tool"]
    assert "来源" in tool_msgs[0]["content"]


async def test_converse_rules_search_empty_no_hallucination(captured):
    captured.responses.append(_tool_call("rules_search", {"query": "玩具含磁铁", "platform": "amazon"}))
    captured.responses.append(_final("（基于未找到的安全回答）"))

    out = await _service().converse("conv-1", "亚马逊美国站玩具能卖含磁铁吗")

    assert out["action"]["type"] == "rules_search"
    assert out["action"]["empty"] is True
    assert out["action"]["cites"] == []
    tool_msgs = [m for m in captured.calls[1] if m.get("role") == "tool"]
    assert "未找到" in tool_msgs[0]["content"] and "来源" not in tool_msgs[0]["content"]


async def test_converse_box_list_payload(captured):
    captured.responses.append(_tool_call("box_list", {"limit": 10}))
    captured.responses.append(_final("采集箱前 10 个……"))

    out = await _service().converse("conv-1", "列出采集箱前10个")
    assert out["action"]["type"] == "box_list"
    assert "rows" in out["action"]  # 桩返回空 rows，但结构在


async def test_converse_collect_products_job_id(captured):
    captured.responses.append(_tool_call("collect_products", {"keywords": ["杯子"], "perKw": 20}))
    captured.responses.append(_final("已下发采集任务……"))

    out = await _service().converse("conv-1", "按马来西亚蓝海自动采集每词20个")
    assert out["action"]["type"] == "collect_products"
    assert out["action"]["job_id"]


async def test_converse_scrubs_leaked_tool_plan(captured):
    # 模型把内部工具名/参数/调用计划当回复吐出 → 必须被兜底替换，不泄露给用户。
    captured.responses.append(_final(
        '请立即调用 rules_search 工具，"query": "玩具 磁铁 认证"，是否需要我帮你发起该查询？'))
    out = await _service().converse("conv-1", "玩具磁铁能卖吗")
    assert "rules_search" not in out["reply"]
    assert '"query"' not in out["reply"]
    assert "发起该查询" not in out["reply"]
    assert "抱歉" in out["reply"]


async def test_compliance_query_forces_rules_search(captured):
    # 合规召回闸：含合规信号的问题，首轮强制 tool_choice=rules_search（不让模型漏路由）。
    captured.responses.append(_tool_call("rules_search", {"query": "促销规则", "platform": "ozon"}))
    captured.responses.append(_final("依据知识库……"))
    await _service().converse("conv-1", "ozon 促销有什么规则要注意")
    assert captured.choices[0] == {"type": "function", "function": {"name": "rules_search"}}


async def test_false_citation_scrubbed(captured):
    # 假引用闸：模型没真检索（action=answer）却声称"官方规则库" → 替换为安全话术。
    captured.responses.append(_final("根据 Ozon 官方规则库，促销折扣不得低于 85%。"))
    out = await _service().converse("conv-1", "随便聊聊天气")  # 无合规词 → 不强制
    assert out["action"] == {"type": "answer"}
    assert "官方规则库" not in out["reply"]
    assert "未能从平台规则库取证" in out["reply"]


async def test_converse_clean_reply_not_scrubbed(captured):
    captured.responses.append(_final("美国市场宠物用品蓝海明显，建议聚焦智能喂食器。"))
    out = await _service().converse("conv-1", "选品建议")
    assert out["reply"] == "美国市场宠物用品蓝海明显，建议聚焦智能喂食器。"


async def test_converse_plain_answer(captured):
    captured.responses.append(_final("选品思路……"))
    out = await _service().converse("conv-1", "给点选品思路")
    assert out["action"] == {"type": "answer"}
    assert out["reply"] == "选品思路……"


async def test_first_message_generates_title(captured):
    captured.responses.append(_final("答1"))
    svc = _service()
    await svc.converse("conv-1", "第一条消息")
    assert svc.repo.title == "测试标题"  # 首条触发 LLM 标题


async def test_second_message_does_not_regenerate_title(captured):
    captured.responses.append(_final("答1"))
    captured.responses.append(_final("答2"))
    svc = _service()
    await svc.converse("conv-1", "第一条")
    svc.repo.title = "SENTINEL"          # 标记，第二条不应覆盖
    await svc.converse("conv-1", "第二条")
    assert svc.repo.title == "SENTINEL"  # 非首条不再生成标题


async def test_converse_persists_user_and_assistant(captured):
    captured.responses.append(_tool_call("rules_search", {"query": "费用", "platform": "ozon"}))
    captured.responses.append(_final("依据知识库……"))

    svc = _service()
    await svc.converse("conv-1", "Ozon 佣金多少")

    msgs = svc.repo.messages
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].action["type"] == "rules_search"  # 结构化 action 落库


# ---- converse_stream：SSE 事件序列 ----
async def test_converse_stream_event_order(captured):
    captured.responses.append(_tool_call("rules_search", {"query": "费用", "platform": "ozon"}))
    captured.responses.append(_final("依据知识库……" * 3))

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


async def test_converse_stream_token_reassembles_reply(captured):
    full = "这是一段较长的回复用于验证 token 切块能拼回原文。" * 2
    captured.responses.append(_final(full))

    events = [ev async for ev in _service().converse_stream("conv-1", "随便聊")]
    tokens = "".join(e["data"]["delta"] for e in events if e["event"] == "token")
    assert tokens == full  # 切块无损
