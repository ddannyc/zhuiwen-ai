"""RulesKbService.search 溯源契约回归测试（读真实 299 条 jsonl，不 mock）。

固化 docs/chat-redesign-plan.md §6 的硬约束：metadata 隔离 + 溯源字段 +
空检索不编造。数据是 needs_review 的 Ozon 种子语料。
"""
from app.domains.rules_kb.service import RulesKbService

svc = RulesKbService(session=None)  # search 不碰 session


async def test_ozon_hit_carries_sourcing():
    hits = await svc.search("取消率多少会被封号", platform="ozon")
    assert hits, "Ozon 取消率查询应命中"
    top = hits[0]
    # 契约字段齐全且非空
    assert top["source_url"].startswith("https://")
    assert top["version"]
    assert top["last_verified_at"]
    assert top["platform"] == "ozon"


async def test_platform_isolation_amazon_empty():
    # KB 只有 ozon，查 amazon 必须 0 命中（杜绝串台 → 触发"不知道"路径）
    assert await svc.search("玩具类目能卖含磁铁的吗", platform="amazon") == []


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
