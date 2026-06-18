"""Listing 多语言生成 agent（LangGraph 示例）。

演示两件事：
  1. agent 推理循环用 LangGraph 编排；
  2. 它通过 KnowledgeBaseService（域的公开接口）取知识，
     而不是直接碰知识库的表 —— 守住"禁止跨模块读表"的纪律。

注意：这是"模型自主调工具"那一层。真正的长流程业务编排
（多平台批量刊登、断点恢复、审批）应放到 workers/ 里用 Temporal 跑。
"""
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.knowledge_base.service import KnowledgeBaseService
from app.shared.llm.gateway import chat


class ListingState(TypedDict):
    product_id: str
    target_locale: str            # 如 "de-DE"、"ja-JP"
    brand_context: str            # 从知识库取到的品牌/合规资料
    draft: str
    messages: Annotated[list, add_messages]


def build_listing_agent(session: AsyncSession):
    kb = KnowledgeBaseService(session)

    async def gather_context(state: ListingState) -> ListingState:
        # 通过公开接口检索品牌词、违禁词、平台规则等
        snippets = await kb.retrieve(
            f"{state['target_locale']} listing 规范 品牌调性 违禁词", limit=5
        )
        return {**state, "brand_context": "\n".join(snippets)}

    async def generate(state: ListingState) -> ListingState:
        draft = await chat(
            messages=[
                {"role": "system", "content": "你是跨境电商 listing 本地化专家。"},
                {
                    "role": "user",
                    "content": (
                        f"目标市场：{state['target_locale']}\n"
                        f"参考资料：{state['brand_context']}\n"
                        f"请为商品 {state['product_id']} 生成本地化 listing。"
                    ),
                },
            ],
            model="gpt-4o",
        )
        return {**state, "draft": draft}

    graph = StateGraph(ListingState)
    graph.add_node("gather_context", gather_context)
    graph.add_node("generate", generate)
    graph.add_edge(START, "gather_context")
    graph.add_edge("gather_context", "generate")
    graph.add_edge("generate", END)
    return graph.compile()
