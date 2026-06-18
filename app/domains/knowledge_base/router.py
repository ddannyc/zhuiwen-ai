"""知识库 HTTP 路由。router 只做参数校验和调 service，不写业务逻辑。"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.domains.knowledge_base.service import KnowledgeBaseService

router = APIRouter(prefix="/knowledge-base", tags=["knowledge_base"])


class IngestRequest(BaseModel):
    title: str
    source_uri: str
    text: str


class SearchRequest(BaseModel):
    query: str
    limit: int = 5


@router.post("/documents")
async def ingest(body: IngestRequest, db: AsyncSession = Depends(get_db)):
    await KnowledgeBaseService(db).ingest(body.title, body.source_uri, body.text)
    return {"status": "ok"}


@router.post("/search")
async def search(body: SearchRequest, db: AsyncSession = Depends(get_db)):
    results = await KnowledgeBaseService(db).retrieve(body.query, body.limit)
    return {"results": results}
