"""登录换 token（POST /auth/token）。对齐前端 contract.ts 的 ChatApi.login。

本期 demo 鉴权：内置账号表演示「一租户两员工」（计划假设 6）——
alice / bob 同属一个 tenant，互不可见对方会话（靠 user_id 归属 + RLS）。
租户由账号在服务端决定写进 JWT，前端不传也不选 tenant_id。

真实鉴权（密码哈希、用户表、刷新）后续替换，签名不变。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.shared.auth.jwt import issue_token

router = APIRouter(prefix="/auth", tags=["auth"])

# demo 账号 → (user_id, tenant_id)。两个账号同租户，演示组织内多员工隔离。
_DEMO_TENANT = "11111111-1111-1111-1111-111111111111"
_DEMO_ACCOUNTS: dict[str, dict[str, str]] = {
    "alice": {"user_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "tenant_id": _DEMO_TENANT},
    "bob":   {"user_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "tenant_id": _DEMO_TENANT},
}
_DEMO_PASSWORD = "demo"  # 仅演示


class LoginRequest(BaseModel):
    account: str
    password: str


@router.post("/token")
async def token(body: LoginRequest):
    acc = _DEMO_ACCOUNTS.get(body.account)
    if not acc or body.password != _DEMO_PASSWORD:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    jwt = issue_token(user_id=acc["user_id"], tenant_id=acc["tenant_id"])
    return {"token": jwt, "tenant_id": acc["tenant_id"], "user_id": acc["user_id"]}
