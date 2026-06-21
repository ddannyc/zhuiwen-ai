"""真·HTTP e2e：login → 建会话 → 发消息(SSE) → 取历史，打真 Postgres + RLS。

仅 LLM 经 gateway.chat_with_tools mock（LiteLLM 未起）；其余全真：
FastAPI 路由、中间件解租户、app 角色 + RLS 隔离、会话/消息落库。
DB 连不上则整文件 skip（CI 无 PG 时不挂）。

验证 docs/chat-redesign-plan.md 验证清单：rules_search 路由 + 结构化 payload + 落库 +
同租户不同员工按 user_id 归属隔离。
"""
import json

import httpx
import psycopg
import pytest

import app.domains.chat.agent as agent_mod
from app.core.config import get_settings
from app.main import app


def _db_reachable() -> bool:
    try:
        url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
        with psycopg.connect(url, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_reachable(), reason="本地 Postgres(xborder) 不可达")


@pytest.fixture(autouse=True)
async def _fresh_engine():
    """每个测试前 dispose 全局 async engine：asyncpg 连接与事件循环绑定，
    pytest 每个测试用新 loop，复用旧 loop 的池连接会 RuntimeError。dispose 强制重建。"""
    from app.core.database import engine
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
def mock_llm(monkeypatch):
    """假 LLM：见到含"佣金/费用/规则"的用户消息 → 路由 rules_search；
    工具结果回灌后（出现 tool 消息）→ 出最终答。无关键词 → 直接答。"""
    async def fake_cwt(messages, tools, model="x", **kw):
        if any(m.get("role") == "tool" for m in messages):
            return {"role": "assistant", "content": "依据知识库给出回答。", "tool_calls": []}
        user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        if any(k in user for k in ("佣金", "费用", "规则")):
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "rules_search",
                             "arguments": json.dumps({"query": user, "platform": "ozon"})}}]}
        return {"role": "assistant", "content": "普通回答。", "tool_calls": []}

    monkeypatch.setattr(agent_mod, "chat_with_tools", fake_cwt)


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _login(c, account):
    r = await c.post("/auth/token", json={"account": account, "password": "demo"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _parse_sse(text: str) -> list[dict]:
    events = []
    for frame in text.split("\n\n"):
        ev, data = None, None
        for line in frame.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if ev:
            events.append({"event": ev, "data": data})
    return events


async def test_full_chat_flow_rules_search(mock_llm):
    async with _client() as c:
        token = await _login(c, "alice")
        h = {"Authorization": f"Bearer {token}"}

        # 建会话
        conv = (await c.post("/chat/conversations", json={"title": "e2e"}, headers=h)).json()
        cid = conv["id"]
        assert conv["user_id"] and conv["created_at"]

        # 发消息 → SSE
        r = await c.post(f"/chat/conversations/{cid}/messages",
                         json={"message": "Ozon 佣金费用怎么算"}, headers=h)
        assert r.status_code == 200
        events = _parse_sse(r.text)
        names = [e["event"] for e in events]
        # 两段式：首事件是占位 tool_running，随后 action…done
        assert names[0] == "tool_running" and "action" in names and names[-1] == "done"
        payload = next(e["data"] for e in events if e["event"] == "payload")
        assert payload["type"] == "rules_search"
        # 真查了 jsonl 知识库，命中带溯源
        assert payload["empty"] is False
        assert payload["cites"][0]["source_url"].startswith("https://")

        # 取历史：user + assistant 两条，assistant 带结构化 action（落库验证）
        msgs = (await c.get(f"/chat/conversations/{cid}/messages", headers=h)).json()["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[1]["action"]["type"] == "rules_search"


async def test_idor_same_tenant_messages_blocked(mock_llm):
    # alice 建会话并发消息；bob（同租户）拿到 id 也不能读/写 → 404（不泄露存在性）。
    async with _client() as c:
        ta = await _login(c, "alice")
        ha = {"Authorization": f"Bearer {ta}"}
        conv = (await c.post("/chat/conversations", json={"title": "私有"}, headers=ha)).json()
        cid = conv["id"]
        await c.post(f"/chat/conversations/{cid}/messages", json={"message": "你好"}, headers=ha)

        tb = await _login(c, "bob")
        hb = {"Authorization": f"Bearer {tb}"}
        # bob 读 alice 会话消息 → 404
        assert (await c.get(f"/chat/conversations/{cid}/messages", headers=hb)).status_code == 404
        # bob 往 alice 会话发消息 → 404
        r = await c.post(f"/chat/conversations/{cid}/messages", json={"message": "侵入"}, headers=hb)
        assert r.status_code == 404
        # alice 本人仍可读
        assert (await c.get(f"/chat/conversations/{cid}/messages", headers=ha)).status_code == 200


async def test_ownership_isolation_same_tenant(mock_llm):
    # alice / bob 同租户。alice 建会话，bob 列表里看不到（按 user_id 归属过滤）。
    async with _client() as c:
        ta = await _login(c, "alice")
        await c.post("/chat/conversations", json={"title": "alice私有"},
                     headers={"Authorization": f"Bearer {ta}"})
        tb = await _login(c, "bob")
        bob_list = (await c.get("/chat/conversations",
                                headers={"Authorization": f"Bearer {tb}"})).json()
        assert all(conv["title"] != "alice私有" for conv in bob_list)
