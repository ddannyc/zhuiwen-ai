"""知识库数据访问层。

关键点：这里没有任何 `WHERE tenant_id = ...`。RLS 已经在数据库层
保证了只能看到当前租户的行。代码更干净，也更安全（不会漏写）。
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.knowledge_base.models import Chunk, Document


class KnowledgeBaseRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add_document(self, title: str, source_uri: str) -> Document:
        # 不传 tenant_id —— 由表的 DEFAULT current_setting('app.current_tenant') 填充
        doc = Document(title=title, source_uri=source_uri)
        self.session.add(doc)
        await self.session.flush()
        return doc

    async def add_chunks(self, chunks: list[Chunk]) -> None:
        self.session.add_all(chunks)
        await self.session.flush()

    async def search(self, query_embedding: list[float], limit: int = 5) -> list[Chunk]:
        # 向量相似度检索。RLS 保证只在当前租户的 chunks 里搜。
        stmt = (
            select(Chunk)
            .order_by(Chunk.embedding.cosine_distance(query_embedding))
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
