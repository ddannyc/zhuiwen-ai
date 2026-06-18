"""采集箱（box）域 —— 对外唯一接口（本期最小桩）。

对标旧 zhuiwen_web.py 的 box.* 动作（见 docs/chat-business-logic.md §5.3）。
chat agent 的 box_* 工具只调本 service，不直接读采集箱表。

本期桩：返回可预测的占位数据，让 chat agent 的工具路由可端到端跑通。
真实实现接入采集箱仓储后替换，签名不变。
"""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class BoxService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def count(self) -> int:
        """采集箱商品数。对标 box.count。"""
        return 0

    async def list(self, *, keyword: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        """列采集箱（可按 keyword 过滤，最多 30 条）。对标 box.list。"""
        return []

    async def delete(self, *, scope: str = "chinese") -> int:
        """删除商品。scope=chinese 仅删未翻译中文标题；scope=all 清空。

        对标 box.delete_chinese / box.delete_all。返回删除条数。
        """
        return 0

    async def translate(self, *, scope: str = "all", lang: str = "en", images: bool = False) -> dict[str, Any]:
        """翻译标题/图片并写回。对标 box.translate。"""
        return {"translated": 0, "scope": scope, "lang": lang, "images": images}

    async def list_tiktok(self, *, scope: str = "all", auto: bool = False) -> dict[str, Any]:
        """采集箱商品上架 TikTok。对标 box.list_tiktok。auto=是否直接发布。"""
        return {"prefilled": 0, "published": 0, "failed": 0, "total": 0, "auto": auto}
