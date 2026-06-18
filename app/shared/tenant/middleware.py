"""租户上下文中间件。

全应用唯一负责"解析租户身份"的地方。它从 JWT 里取出 tenant_id，
塞进 ContextVar，后续所有 DB 会话自动据此应用 RLS。
业务模块永远不需要关心租户是怎么来的。
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.database import current_tenant_id
from app.shared.auth.jwt import decode_token

# 不需要租户上下文的公开路径
PUBLIC_PATHS = ("/health", "/docs", "/openapi.json", "/auth/login")


class TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path.startswith(PUBLIC_PATHS):
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"detail": "missing bearer token"}, status_code=401)

        try:
            claims = decode_token(auth.removeprefix("Bearer ").strip())
        except Exception:
            return JSONResponse({"detail": "invalid token"}, status_code=401)

        tenant_id = claims.get("tenant_id")
        if not tenant_id:
            return JSONResponse({"detail": "token missing tenant_id"}, status_code=403)

        # 设置上下文。token 设回去保证请求结束后不污染其他请求。
        token = current_tenant_id.set(tenant_id)
        request.state.tenant_id = tenant_id
        request.state.user_id = claims.get("sub")
        try:
            return await call_next(request)
        finally:
            current_tenant_id.reset(token)
