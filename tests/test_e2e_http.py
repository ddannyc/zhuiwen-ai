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
from app.shared.auth.jwt import issue_token


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

    # 终答生成（chat_stream / chat）也 mock —— 否则 converse_stream 综述会真打 DashScope
    # （review #2：e2e 不应依赖真 key / 真网络）。
    import app.domains.chat.service as chat_service_mod

    async def fake_chat(messages, model="x", **kw):
        return "依据知识库给出回答。"

    async def fake_chat_stream(messages, model="x", **kw):
        yield "依据知识库给出回答。"

    monkeypatch.setattr(agent_mod, "chat_with_tools", fake_cwt)
    monkeypatch.setattr(chat_service_mod, "chat", fake_chat)
    monkeypatch.setattr(chat_service_mod, "chat_stream", fake_chat_stream)


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


async def test_sourcing_lifecycle_and_rls():
    """sourcing 采集任务全生命周期（真 PG + RLS，强制降级直写库）：
    collect→pending→poll 认领(collecting)→done(collected+result)。poll 走
    claim_next + _serialize，正是 func.now() MissingGreenlet 回归点。
    跨租户：另一租户 poll 取不到、GET 他人任务 404。"""
    # 每次用全新随机租户，避免历史残留 pending 被 FIFO poll 误认领（测试隔离）。
    alice = {"Authorization": f"Bearer {_token()}"}
    other = {"Authorization": f"Bearer {_token()}"}
    async with _client() as c:
        # 下发（Temporal 不可达 → mode degraded，pending 行落库）
        j = (await c.post("/sourcing/collect", headers=alice,
                          json={"keywords": ["杯子", "水壶"], "per_kw": 15, "market": "my"})).json()
        jid = j["job_id"]
        assert j["mode"] == "ok"
        assert (await c.get(f"/sourcing/jobs/{jid}", headers=alice)).json()["status"] == "pending"

        # 插件认领：claim_next 置 collecting 并序列化返回（func.now 回归点）
        poll = (await c.post("/sourcing/jobs/poll", headers=alice)).json()
        assert poll["job"] is not None and poll["job"]["id"] == jid
        assert poll["job"]["status"] == "collecting"

        # 回结果：降级直接标 collected + 落 result
        done = (await c.post(f"/sourcing/jobs/{jid}/done", headers=alice,
                             json={"result": {"items": [{"t": "A"}, {"t": "B"}]}})).json()
        assert done["ok"] is True
        got = (await c.get(f"/sourcing/jobs/{jid}", headers=alice)).json()
        assert got["status"] == "collected"
        assert got["result"]["items"] == [{"t": "A"}, {"t": "B"}]

        # 跨租户隔离：tenant-B 认领不到 A 的任务、GET A 的任务 404
        # 先给 A 造一个新 pending，确保队列里有 A 的任务可被（错误地）取到
        await c.post("/sourcing/collect", headers=alice, json={"keywords": ["A私有"], "per_kw": 5})
        assert (await c.post("/sourcing/jobs/poll", headers=other)).json()["job"] is None
        assert (await c.get(f"/sourcing/jobs/{jid}", headers=other)).status_code == 404


async def test_sourcing_done_foreign_job_404_no_mutation():
    # 评审 blocker 回归（跨租户写路径）：租户B 不能对租户A 的 job /done。
    # 既挡 Temporal 直签 IDOR，也证 RLS 写路径不漏。断言 A 的 job 未被改动。
    alice = {"Authorization": f"Bearer {_token()}"}
    bob = {"Authorization": f"Bearer {_token()}"}
    async with _client() as c:
        j = (await c.post("/sourcing/collect", headers=alice,
                          json={"keywords": ["私有"], "per_kw": 5})).json()
        jid = j["job_id"]
        # B 拿到 A 的 job_id 回结果 → 404，且不得写入
        r = await c.post(f"/sourcing/jobs/{jid}/done", headers=bob,
                         json={"result": {"items": [{"evil": 1}]}})
        assert r.status_code == 404
        got = (await c.get(f"/sourcing/jobs/{jid}", headers=alice)).json()
        assert got["status"] == "pending"      # 未被推进
        assert got["result"] is None           # 未被注入伪造结果


async def test_sourcing_malformed_job_id_returns_404():
    # 评审 #2：非法 uuid 路径参数应 404（资源不存在），不得 500（未捕异常）。
    h = {"Authorization": f"Bearer {_token()}"}
    async with _client() as c:
        assert (await c.get("/sourcing/jobs/not-a-uuid", headers=h)).status_code == 404
        r = await c.post("/sourcing/jobs/not-a-uuid/done", headers=h, json={"result": {}})
        assert r.status_code == 404


def _token() -> str:
    # 全新随机租户/用户（RLS 列是 uuid）。每调一次即一个干净隔离的租户。
    import uuid
    return issue_token(user_id=str(uuid.uuid4()), tenant_id=str(uuid.uuid4()))
