"""知识库业务逻辑层。

★ 这是 knowledge_base 域对外暴露的唯一入口。
其他域（如 customer_service）需要检索知识时，调用这里的方法，
绝不允许去 import KnowledgeBaseRepository 或直接读 kb_chunks 表。
将来若把 knowledge_base 拆成独立服务，只需把这些方法换成 RPC 调用。
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.knowledge_base.models import Chunk
from app.domains.knowledge_base.repository import KnowledgeBaseRepository
from app.shared.llm.embeddings import embed_text


class KnowledgeBaseService:
    def __init__(self, session: AsyncSession):
        self.repo = KnowledgeBaseRepository(session)

    async def ingest(self, title: str, source_uri: str, text: str) -> None:
        # ⚠ embed_text 现默认 DashScope text-embedding-v3（原 bge-m3 已弃）。kb_chunks
        # 入库与检索必须同一模型——不同模型向量空间不可比，混入同表会让 cosine 相似度失真。
        # 本域当前未启用、kb_chunks 为空；启用前若已有旧 bge-m3 向量须全量重灌。
        doc = await self.repo.add_document(title, source_uri)
        pieces = _split(text)
        embeddings = await embed_text(pieces)
        chunks = [
            Chunk(document_id=doc.id, content=p, embedding=e)
            for p, e in zip(pieces, embeddings)
        ]
        await self.repo.add_chunks(chunks)

    async def retrieve(self, query: str, limit: int = 5) -> list[str]:
        """供 RAG / agent 调用：返回与 query 最相关的文本片段。"""
        [query_emb] = await embed_text([query])
        chunks = await self.repo.search(query_emb, limit=limit)
        return [c.content for c in chunks]


def _split(text: str, size: int = 800) -> list[str]:
    # 占位：实际用 unstructured / 语义分块。这里只演示结构。
    return [text[i : i + size] for i in range(0, len(text), size)] or [text]
