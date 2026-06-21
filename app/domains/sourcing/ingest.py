"""sourcing 评分 + 违禁词清洗（纯逻辑，移植自旧 zhuiwen_web）。

score_candidates 用注入的 llm_json（async，喂候选 → 返回每条 {i,score,...}）评分，
按 threshold 或 top_n 标 pass；clean_title 删平台名/批发/工厂等违禁与营销词。
LLM 注入便于单测；真实调用经 gateway（见 tasks._default_llm_json）。
"""
import json
import re
from typing import Awaitable, Callable

# llm_json(system, user) -> 评分数组 [{i, score, title_en, reason, category}, ...]
LlmJson = Callable[[str, str], Awaitable[list]]

_SYSTEM = "你是 TikTok Shop 跨境选品专家。严格只返回一个 JSON 数组，不要解释、不要 markdown。"

# 违禁词（妙手词库 + 常见货源营销词）：标题里出现会导致平台发布失败或不专业，自动清掉。
_BANWORDS = [
    "淘宝", "天猫", "Taobao", "京东", "JD.com", "拼多多", "Pinduoduo", "Temu", "唯品会", "Vipshop",
    "苏宁易购", "苏宁", "Suning", "亚马逊", "Amazon", "eBay", "易贝", "沃尔玛", "Walmart", "Wayfair",
    "Etsy", "Shopee", "TikTok", "抖音", "Zalando", "Lazada", "来赞达", "Wish", "Shopify", "Ozon",
    "AliExpress", "速卖通", "Coupang", "Alibaba", "阿里巴巴", "Daraz", "JOOM", "Allegro", "Qoo10",
    "SHOPLINE", "SHEIN", "Miravia", "JUMIA", "eMAG", "1688", "义乌", "PingPong", "LianLian",
    "跨境寻源通", "货源网", "供应链", "一件代发", "代发", "厂家直销", "厂家", "工厂", "源头工厂",
    "源头", "批发", "批发价", "清仓", "清仓价", "跨境专供", "外贸", "特价", "促销", "秒杀", "包邮",
    "正品", "专柜", "旗舰店", "官方", "直销", "礼品伞广告伞", "可印logo", "印logo", "定制",
]
_BAN_RE = re.compile(
    "|".join(re.escape(w) for w in sorted(set(_BANWORDS), key=len, reverse=True)),
    re.IGNORECASE,
)


def clean_title(t: str) -> str:
    """删标题里的平台名/批发/工厂等违禁与营销词，整理空白与多余标点。"""
    s = _BAN_RE.sub("", t or "")
    s = re.sub(r"[【】\[\]（）()]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ，,、。.-—|/·　")
    return s


def loose_json_array(text: str) -> list:
    """从可能裹了 markdown/解释的模型输出里抠出 JSON 数组。"""
    s = (text or "").strip()
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        pass
    m = re.search(r"\[.*\]", s, re.DOTALL)
    if m:
        try:
            v = json.loads(m.group(0))
            return v if isinstance(v, list) else []
        except Exception:
            return []
    return []


async def score_candidates(
    cands: list[dict], threshold: int = 70, top_n: int = 0, *, llm_json: LlmJson
) -> dict:
    """评分候选：llm 给每条 score → 按 threshold（或 top_n 覆盖）标 pass。
    返回 {count, passed, scores:[{id, score, pass, title(清洗), title_en, reason, category, source_url}]}。"""
    if not cands:
        return {"count": 0, "passed": 0, "scores": []}

    payload = [
        {"i": i, "title": c.get("title", ""), "price_cny": c.get("price_cny", 0)}
        for i, c in enumerate(cands)
    ]
    user = (
        "为以下 %d 个 1688 货源候选打分（0-100，TikTok Shop 跨境选品视角），"
        "只评估给出的商品、不要编造。每条返回 {i, score, title_en, reason, category}。\n候选：\n%s"
        % (len(cands), json.dumps(payload, ensure_ascii=False))
    )
    scored = await llm_json(_SYSTEM, user)
    by_i: dict[int, dict] = {}
    for it in scored if isinstance(scored, list) else []:
        if isinstance(it, dict) and "i" in it:
            try:
                by_i[int(it["i"])] = it
            except (ValueError, TypeError):
                pass

    rows = []
    for i, c in enumerate(cands):
        s = by_i.get(i, {})
        try:
            sc = float(s.get("score") or 0)
        except (ValueError, TypeError):
            sc = 0.0
        rows.append({"c": c, "score": sc, "title_en": str(s.get("title_en", "")),
                     "reason": str(s.get("reason", "")), "category": str(s.get("category", ""))})
    rows.sort(key=lambda r: r["score"], reverse=True)

    if top_n and top_n > 0:
        # top_n 覆盖阈值：只留分数最高的 N 个为 pass。
        keep = {id(r) for r in rows[:top_n]}
        for r in rows:
            r["pass"] = id(r) in keep
    else:
        for r in rows:
            r["pass"] = r["score"] >= threshold

    scores = [
        {
            "id": r["c"].get("id"),
            "score": r["score"],
            "pass": bool(r["pass"]),
            "title": clean_title(r["c"].get("title", "")),
            "title_en": r["title_en"],
            "reason": r["reason"],
            "category": r["category"],
            "source_url": r["c"].get("source_url", ""),
        }
        for r in rows
    ]
    return {"count": len(cands), "passed": sum(1 for s in scores if s["pass"]), "scores": scores}
