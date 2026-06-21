"""sourcing 域 HTTP 请求体。"""
from pydantic import BaseModel


class StartCollectRequest(BaseModel):
    keywords: list[str] = []
    per_kw: int = 10
    market: str | None = None


class JobDoneRequest(BaseModel):
    # 采集插件回传的抓取结果。约定形如 {"items": [...]}，由下游后处理消费。
    result: dict = {}
