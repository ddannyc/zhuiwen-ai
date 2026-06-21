"""rules_kb 数据访问层——全局共享表，无 RLS、无 tenant 过滤。

platform/site 是业务硬隔离（非 RLS），必须显式写进 WHERE（查 amazon 绝不串 ozon）。
corpus 小，取回过滤全集（附 cosine 距离），由 service 做向量+词法 RRF 融合。
"""
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.rules_kb.models import RulesKbRow

# 取回 service 融合/打分/投影所需列：_RETURN_FIELDS ∪ 词法打分字段(content/tags)。
# 不取 embedding（大且无用），省带宽。
_COLS = (
    RulesKbRow.rule_id, RulesKbRow.title, RulesKbRow.summary, RulesKbRow.content,
    RulesKbRow.tags, RulesKbRow.source_url, RulesKbRow.version, RulesKbRow.confidence,
    RulesKbRow.last_verified_at, RulesKbRow.platform, RulesKbRow.site,
    RulesKbRow.rule_domain, RulesKbRow.verification_status,
)


class RulesKbRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def search_filtered(
        self, query_emb: list[float], *,
        platform: Optional[str] = None, site: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """按 platform/site 硬过滤取回全集，附向量 cosine 距离（dist，越小越近）。

        site：精确匹配 OR GLOBAL（GLOBAL 规则适用所有站点，对齐 jsonl 逻辑）。
        """
        dist = RulesKbRow.embedding.cosine_distance(query_emb).label("dist")
        stmt = select(*_COLS, dist)
        if platform:
            stmt = stmt.where(func.lower(RulesKbRow.platform) == platform.strip().lower())
        if site:
            st = site.strip().lower()
            stmt = stmt.where(
                or_(func.lower(RulesKbRow.site) == st, func.lower(RulesKbRow.site) == "global")
            )
        stmt = stmt.order_by(dist)
        res = await self.session.execute(stmt)
        return [dict(r._mapping) for r in res]

    async def is_empty(self) -> bool:
        res = await self.session.execute(select(RulesKbRow.rule_id).limit(1))
        return res.first() is None
