"""JWT 编解码。鉴权细节集中在 auth 域，其他模块只消费解出来的 claims。"""
import jwt

from app.core.config import get_settings

settings = get_settings()


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def issue_token(*, user_id: str, tenant_id: str, extra: dict | None = None) -> str:
    payload = {"sub": user_id, "tenant_id": tenant_id, **(extra or {})}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
