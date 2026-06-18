"""Chat 域 HTTP 路由。只做参数校验 + 取登录态 + 调 service，不写业务逻辑。

user_id 取自 request.state（TenantMiddleware 从 JWT sub 解出）；
tenant_id 不在这里碰，RLS 自动隔离。

发消息端点返回 SSE（text/event-stream），对齐前端 contract.ts。
"""
import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.domains.chat.schemas import (
    AnalyzeRequest,
    CreateConversationRequest,
    SendMessageRequest,
    VisionRequest,
)
from app.domains.chat.service import ChatService

router = APIRouter(prefix="/chat", tags=["chat"])


@router.get("/conversations")
async def list_conversations(request: Request, db: AsyncSession = Depends(get_db)):
    return await ChatService(db).list_conversations(request.state.user_id)


@router.post("/conversations")
async def create_conversation(
    body: CreateConversationRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    return await ChatService(db).create_conversation(request.state.user_id, body.title)


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    svc = ChatService(db)
    # 归属校验：非本人会话一律 404（不泄露存在性）。RLS 只挡跨租户，同租户归属靠这里。
    if not await svc.user_owns_conversation(request.state.user_id, conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"messages": await svc.list_messages(conversation_id)}


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str, body: SendMessageRequest, request: Request,
    db: AsyncSession = Depends(get_db),
):
    """主入口：自然语言 → 路由 → 执行 → SSE 事件流。"""
    svc = ChatService(db)
    # 归属校验：非本人会话一律 404（防止往他人会话写消息/触发 agent）。
    if not await svc.user_owns_conversation(request.state.user_id, conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found")

    async def gen() -> AsyncIterator[str]:
        async for ev in svc.converse_stream(conversation_id, body.message):
            yield _sse(ev["event"], ev["data"])

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/vision")
async def vision(body: VisionRequest, db: AsyncSession = Depends(get_db)):
    return {"reply": await ChatService(db).vision(body.message, body.images)}


@router.post("/analyze")
async def analyze(body: AnalyzeRequest, db: AsyncSession = Depends(get_db)):
    return {"reply": await ChatService(db).analyze(body.keyword, body.type)}
