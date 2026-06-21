"""Phase3 T3.2：TikTok 上架编排（精简忠实移植，mock 妙手 tkcall by endpoint）。

最小可上架闭环：认领平台采集箱 → 认领店铺 → 预填(必填补全) → 可选发布。
AI 选类目、类目属性补全为可选钩子（外部依赖，默认跳过）。
"""
import pytest

from app.domains.sourcing.publish import CLAIMED_PATH, fill_required, publish_to_tiktok


class FakeMS:
    def __init__(self):
        self.calls: list = []

    def shops(self):
        return [{"shopId": 77}]

    def tkcall(self, endpoint, body):
        self.calls.append((endpoint, body))
        if endpoint == CLAIMED_PATH:
            return {"ok": True, "data": {"platformCollectBoxDetailIdMap": {"tiktok": {"1": 1001, "2": 1002}}}}
        if endpoint == "claim_to_shop":
            return {"ok": True, "data": {}}
        if endpoint == "get_shop_collect_item_info":
            return {"ok": True, "data": {"ossMd5": "md5", "shopCollectItemInfo": {"title": "X"}}}
        if endpoint == "save_shop_collect_item_info":
            return {"ok": True}
        if endpoint == "save_move_collect_task":
            return {"ok": True}
        return {"ok": False, "error": "unknown endpoint"}


async def test_publish_prepares_and_publishes():
    ms = FakeMS()
    res = await publish_to_tiktok(ms, ["1", "2"], auto=True)
    assert res["ok"] is True
    assert res["summary"]["published"] == 2
    assert res["summary"]["failed"] == 0
    eps = [e for e, _ in ms.calls]
    assert CLAIMED_PATH in eps
    assert "claim_to_shop" in eps
    assert "save_move_collect_task" in eps  # auto 发布


async def test_publish_prepare_only_without_auto():
    ms = FakeMS()
    res = await publish_to_tiktok(ms, ["1", "2"], auto=False)
    assert res["summary"]["prepared"] == 2
    assert res["summary"]["published"] == 0
    assert "save_move_collect_task" not in [e for e, _ in ms.calls]


async def test_publish_no_shop_errors():
    class NoShop(FakeMS):
        def shops(self):
            return []

    res = await publish_to_tiktok(NoShop(), ["1"])
    assert res["ok"] is False
    assert "店铺" in res["error"]


async def test_publish_empty_ids_errors():
    res = await publish_to_tiktok(FakeMS(), [])
    assert res["ok"] is False


def test_fill_required_fills_defaults():
    sci: dict = {}
    fill_required(sci, {})
    assert sci["weight"] > 0
    assert sci["packageLength"] > 0
    assert sci["deliveryOptionSetType"] == "default"
