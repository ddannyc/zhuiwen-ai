"""智能客服域（骨架）。

放这里是为了演示跨域调用纪律：客服回复需要查知识库时，
注入 KnowledgeBaseService，调它的 retrieve()，
绝不 import 知识库的 repository 或直接查 kb_chunks 表。
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.knowledge_base.service import KnowledgeBaseService
from app.shared.llm.gateway import chat


class CustomerServiceService:
    def __init__(self, session: AsyncSession):
        self.kb = KnowledgeBaseService(session)  # ← 通过公开接口依赖，不碰对方的表

    async def reply(self, customer_message: str, locale: str) -> str:
        context = await self.kb.retrieve(customer_message, limit=3)
        return await chat(
            messages=[
                {"role": "system", "content": f"你是跨境电商客服，用 {locale} 回复。"},
                {"role": "user", "content": f"参考：{context}\n\n顾客：{customer_message}"},
            ]
        )
