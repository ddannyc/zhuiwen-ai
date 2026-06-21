"""RulesKbService.search 溯源契约回归测试（读真实多平台 jsonl 语料，不 mock）。

固化 docs/chat-redesign-plan.md §6 的硬约束：metadata 隔离 + 溯源字段 +
空检索不编造。数据是 needs_review 的多平台种子语料
（ozon/amazon/tiktok/temu/shein/mercadolibre，data/rules_kb/*_rules.jsonl）。
"""
from app.domains.rules_kb.service import RulesKbService, _fuse

svc = RulesKbService(session=None)  # search 不碰 session


def test_fuse_lexical_exact_ranks_above_pure_semantic():
    """RRF 融合：词法精确命中的规则，应排在向量更近但无词法命中的规则之上。"""
    rows = [
        # b 向量更近(dist 0.30)但与 query 无词法重叠
        {"rule_id": "b", "title": "退货政策说明", "summary": "", "content": "",
         "tags": [], "dist": 0.30},
        # a 向量稍远(dist 0.45)但词法精确命中"取消率"
        {"rule_id": "a", "title": "取消率超标会被封号", "summary": "", "content": "",
         "tags": [], "dist": 0.45},
    ]
    out = _fuse(rows, "取消率", limit=5)
    assert out, "两条 dist 均在阈值内，应有候选"
    assert out[0]["title"] == "取消率超标会被封号", "词法精确命中应被 RRF 抬到首位"


async def test_ozon_hit_carries_sourcing():
    hits = await svc.search("取消率多少会被封号", platform="ozon")
    assert hits, "Ozon 取消率查询应命中"
    top = hits[0]
    # 契约字段齐全且非空
    assert top["source_url"].startswith("https://")
    assert top["version"]
    assert top["last_verified_at"]
    assert top["platform"] == "ozon"


async def test_amazon_now_in_corpus():
    # 多平台语料：amazon 已入库，相关查询应命中且 platform 严格隔离为 amazon
    hits = await svc.search("账号健康指标 退货", platform="amazon")
    assert hits, "Amazon 查询应命中（语料已含 amazon_rules + seed）"
    assert all(h["platform"] == "amazon" for h in hits)
    assert hits[0]["source_url"].startswith("https://")


async def test_platform_isolation_absent_platform_empty():
    # 查语料里不存在的平台必须 0 命中（杜绝串台 → 触发"不知道"路径）
    assert await svc.search("玩具类目能卖含磁铁的吗", platform="ebay") == []


async def test_multi_platform_isolation_no_crosstalk():
    # 同一 query 在不同平台只返回各自平台的规则，绝不串台
    for pf in ("ozon", "amazon", "tiktok", "temu", "shein", "mercadolibre"):
        hits = await svc.search("禁售 商品", platform=pf)
        assert all(h["platform"] == pf for h in hits), f"{pf} 串台"


async def test_global_rules_match_any_site():
    # GLOBAL 规则适用所有站点：查 ozon + site=US 仍命中 GLOBAL 数据（模型常猜错 site，
    # 不放宽会假性 0 命中）。隔离靠 platform（见下条 amazon 测试），不靠 site 卡死。
    hits = await svc.search("佣金费用", platform="ozon", site="US")
    assert hits
    assert all(h["site"] == "GLOBAL" for h in hits)


async def test_fees_query_hits_fee_domain():
    hits = await svc.search("佣金和费用怎么算", platform="ozon", site="GLOBAL")
    assert hits
    assert any(h["rule_domain"] == "fees" for h in hits)


async def test_nonsense_query_returns_empty():
    # 无关查询 → 空 → 上层回"未找到"，不编造
    assert await svc.search("量子物理薛定谔的猫", platform="ozon") == []


async def test_all_hits_are_needs_review():
    # 当前种子全 needs_review；上层据此加"待核验"警示（README 红线）
    hits = await svc.search("取消", platform="ozon", limit=5)
    assert hits
    assert all(h["verification_status"] == "needs_review" for h in hits)


async def test_limit_respected():
    hits = await svc.search("取消", platform="ozon", limit=3)
    assert len(hits) <= 3
