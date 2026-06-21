"""sourcing 域 HTTP 请求体。"""
from pydantic import BaseModel, field_validator


class StartCollectRequest(BaseModel):
    keywords: list[str] = []
    per_kw: int = 10
    market: str | None = None


class JobDoneRequest(BaseModel):
    # 采集插件回传的抓取结果。约定形如 {"items": [...]}，由下游后处理消费。
    result: dict = {}


class IngestOptions(BaseModel):
    """后处理选项，对齐旧 ingest_1688_urls 语义。"""
    threshold: int = 70
    top_n: int = 0
    translate: bool = False
    lang: str = ""
    list_tiktok: bool = False
    tk_auto: bool = False
    optimize: bool = False
    platform: str = "tiktok"


class IngestRequest(BaseModel):
    """扩展回传：登录态采集的 1688 offer URL 批（非完整商品，ADR-002）。"""
    market: str = "1688"
    urls: list[str] = []
    options: IngestOptions = IngestOptions()

    @field_validator("urls")
    @classmethod
    def _clean_urls(cls, v: list[str]) -> list[str]:
        # 只收 1688 offer 链接，去重保序，上限 200（对齐旧 ingest_1688_urls）。
        seen: list[str] = []
        for u in v:
            u = (u or "").strip()
            if "1688.com/offer/" in u and u not in seen:
                seen.append(u)
        if not seen:
            raise ValueError("无有效 1688 offer 链接")
        return seen[:200]
