"""TikTok 上架编排（精简忠实移植自旧 zhuiwen_web.tk_list_items）。

四阶段：① 认领平台采集箱（公共ID→TK_ID）② 认领到店铺 ③ 预填（必填字段补全 + 可选
AI 选类目）④ 可选发布。全经 miaoshou.tkcall 原语；妙手未绑定店铺则报错。

精简（相对旧实现）：略去类目必填属性补全(_tk_fill_category_attrs)与用量统计；AI 选类目
为可注入钩子 pick_category（默认不选，依赖外部 LLM + 类目树）。这些不影响最小可上架闭环，
真实联调时再补（需妙手类目/物流模板对齐）。
"""
import re
from typing import Awaitable, Callable

# 公共采集箱 → 平台采集箱认领接口（妙手 open API 全路径，对齐旧 _CLAIMED_PATH）。
CLAIMED_PATH = "/open/v1/product/common_collect_box/common_collect_box/claimed"

# pick_category(title, cate_tree) -> cid | None（异步，注入；默认不选类目）。
PickCategory = Callable[[str, dict], Awaitable[str | None]]


def _pos(*vals) -> float | None:
    for v in vals:
        try:
            f = float(v)
            if f > 0:
                return round(f, 2)
        except (ValueError, TypeError):
            continue
    return None


def fill_required(sci: dict, logi: dict) -> dict:
    """上架保存前补齐必填（缺失/为0补默认），避免重量/包裹尺寸/配送等"必填"报错。
    移植自旧 _tk_fill_required。"""
    if isinstance(sci.get("imgUrls"), list) and len(sci["imgUrls"]) > 15:
        sci["imgUrls"] = sci["imgUrls"][:15]  # TK 主图上限 15
    notes = sci.get("notes")  # 详情描述图上限 30
    if isinstance(notes, str):
        imgs = list(re.finditer(r"<img\b[^>]*>", notes, re.I))
        if len(imgs) > 30:
            for m in reversed(imgs[30:]):
                notes = notes[:m.start()] + notes[m.end():]
            sci["notes"] = notes
    sci["weight"] = _pos(sci.get("weight"), logi.get("weight_default")) or 0.1
    sci["packageLength"] = _pos(sci.get("packageLength"), logi.get("package_l")) or 10.0
    sci["packageWidth"] = _pos(sci.get("packageWidth"), logi.get("package_w")) or 10.0
    sci["packageHeight"] = _pos(sci.get("packageHeight"), logi.get("package_h")) or 10.0
    if not sci.get("deliveryOptionSetType"):
        sci["deliveryOptionSetType"] = "default"
    if not sci.get("isCodOpen"):
        sci["isCodOpen"] = "0"
    sc = sci.get("sizeChart")
    if isinstance(sc, str) and re.search(r"\.(jpg|jpeg|png)(\?|$)", sc, re.I):
        sci["sizeChartType"] = "image"
    else:
        sci["sizeChart"] = ""
        sci["sizeChartType"] = ""
    return sci


def _summary(results: list[dict], total: int, skipped: int) -> dict:
    return {
        "total": total,
        "skipped": skipped,
        "prepared": sum(1 for r in results if r["status"] == "prepared"),
        "published": sum(1 for r in results if r["status"] == "published"),
        "failed": sum(1 for r in results if "fail" in r["status"]),
    }


async def publish_to_tiktok(
    miaoshou,
    box_ids: list,
    *,
    shop_id: int | None = None,
    site: str = "MY",
    auto: bool = False,
    logistics: dict | None = None,
    pick_category: PickCategory | None = None,
) -> dict:
    ids = [int(d) for d in box_ids if str(d).strip().isdigit()]
    if not ids:
        return {"ok": False, "error": "无可上架商品", "summary": _summary([], 0, 0)}

    if not shop_id:
        shops = miaoshou.shops()
        shop_id = int(shops[0]["shopId"]) if shops else None
    if not shop_id:
        return {"ok": False, "error": "妙手未绑定 TikTok 店铺，请先在妙手绑定店铺"}
    shop_id = int(shop_id)
    logistics = logistics or {}

    # ① 认领到平台采集箱：公共ID → TK_ID
    cd = miaoshou.tkcall(
        CLAIMED_PATH,
        {"detailSerialNumberPlatformList":
         [{"detailId": d, "platform": "tiktok", "serialNumber": 1} for d in ids]},
    )
    if not cd.get("ok"):
        return {"ok": False, "error": "认领平台采集箱失败：" + (cd.get("error") or "")}
    raw = (cd.get("data") or {}).get("platformCollectBoxDetailIdMap") or {}
    idmap = raw.get("tiktok") if isinstance(raw.get("tiktok"), dict) else None
    if idmap is None:
        idmap = next((v for v in raw.values() if isinstance(v, dict)), raw)
    pairs = [(d, int(idmap.get(str(d)) or idmap.get(d)))
             for d in ids if (idmap.get(str(d)) or idmap.get(d))]
    if not pairs:
        return {"ok": False, "error": "认领未返回 TK 商品ID（可能已认领或不支持该平台）"}
    tk_ids = [tk for _, tk in pairs]

    # ② 认领到预发布店铺
    cl = miaoshou.tkcall("claim_to_shop", {"shopIds": [shop_id], "detailIds": tk_ids})
    if not cl.get("ok"):
        return {"ok": False, "error": "认领到店铺失败：" + (cl.get("error") or "")}

    cate_tree: dict = {}
    if pick_category:
        ct = miaoshou.tkcall("get_category_tree_by_site", {"site": site})
        cate_tree = (ct.get("data") or {}).get("cateTree") or {} if ct.get("ok") else {}

    # ③ 逐条预填
    results: list[dict] = []
    for common_id, tkid in pairs:
        r = {"id": common_id, "tkId": tkid, "status": "fail"}
        info = miaoshou.tkcall("get_shop_collect_item_info", {"detailId": tkid, "shopId": shop_id})
        if not info.get("ok"):
            r["error"] = "读取上架信息失败：" + (info.get("error") or "")
            results.append(r)
            continue
        data = info.get("data") or {}
        oss = data.get("ossMd5")
        sci = data.get("shopCollectItemInfo") or {}
        sci["detailId"] = tkid
        sci["shopId"] = shop_id
        sci.setdefault("editModel", data.get("editModel") or "shop")
        r["title"] = (sci.get("title") or sci.get("oriTitle") or "")[:38]
        if pick_category and not sci.get("cid") and cate_tree:
            cid = await pick_category(sci.get("title") or sci.get("oriTitle") or "", cate_tree)
            if cid:
                sci["cid"] = str(cid)
        fill_required(sci, logistics)
        sv = miaoshou.tkcall(
            "save_shop_collect_item_info",
            {"ossMd5": oss, "detailId": tkid, "shopId": shop_id, "shopCollectItemInfo": sci},
        )
        if not sv.get("ok"):
            r["status"] = "prefill_fail"
            r["error"] = "预填保存失败：" + (sv.get("error") or "")
            results.append(r)
            continue
        r["status"] = "prepared"
        results.append(r)

    # ④ 可选发布
    prepared = [r["tkId"] for r in results if r["status"] == "prepared"]
    if auto and prepared:
        pub = miaoshou.tkcall("save_move_collect_task", {"shopIds": [shop_id], "detailIds": prepared})
        for r in results:
            if r.get("tkId") in prepared:
                r["status"] = "published" if pub.get("ok") else "publish_fail"
                if not pub.get("ok"):
                    r["error"] = "发布失败：" + (pub.get("error") or "")

    return {"ok": True, "auto": auto, "results": results,
            "summary": _summary(results, len(ids), 0)}
