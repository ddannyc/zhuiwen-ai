"""/auth/token 端点 HTTP 测试（httpx ASGITransport，无需 DB）。

验证前端 contract.ts 的 ChatApi.login 接缝：账号密码 → {token, tenant_id, user_id}，
且 token 能被中间件解出租户（带它访问受保护端点不再 401-missing-token）。
"""
import httpx
import pytest

from app.main import app
from app.shared.auth.jwt import decode_token


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_login_returns_session_shape():
    async with _client() as c:
        r = await c.post("/auth/token", json={"account": "alice", "password": "demo"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"token", "tenant_id", "user_id"}
    claims = decode_token(body["token"])
    assert claims["sub"] == body["user_id"]
    assert claims["tenant_id"] == body["tenant_id"]


async def test_two_demo_accounts_share_tenant():
    async with _client() as c:
        a = (await c.post("/auth/token", json={"account": "alice", "password": "demo"})).json()
        b = (await c.post("/auth/token", json={"account": "bob", "password": "demo"})).json()
    # 一租户两员工：同 tenant_id，不同 user_id（演示组织内归属隔离）
    assert a["tenant_id"] == b["tenant_id"]
    assert a["user_id"] != b["user_id"]


async def test_bad_password_401():
    async with _client() as c:
        r = await c.post("/auth/token", json={"account": "alice", "password": "wrong"})
    assert r.status_code == 401


async def test_protected_endpoint_requires_token():
    async with _client() as c:
        r = await c.get("/chat/conversations")
    assert r.status_code == 401  # 无 Bearer → 中间件拦截
