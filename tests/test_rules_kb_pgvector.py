"""rules_kb pgvector 混合检索 DB 集成测试（需 Postgres + 已灌库 + DASHSCOPE_API_KEY）。

无 DB / 无 key → 自动 skip。依赖 scripts/load_rules_kb.py 已灌入语料（524 条/6 平台）。
固化 SPEC §1 不变量在 DB 主路径上同样成立：metadata 硬隔离 + 语义召回 + 无关→空 +
契约字段 + 优雅回退。
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.domains.rules_kb.repository import RulesKbRepository
from app.domains.rules_kb.service import RulesKbService

_RETURN_FIELDS = {
    "summary", "source_url", "version", "confidence", "last_verified_at",
    "platform", "site", "rule_domain", "verification_status", "title",
}

needs_key = pytest.mark.skipif(
    not get_settings().dashscope_api_key, reason="需 DASHSCOPE_API_KEY"
)


@pytest_asyncio.fixture
async def session():
    eng = create_async_engine(get_settings().database_admin_url)
    sm = async_sessionmaker(eng, class_=AsyncSession)
    try:
        async with sm() as s:
            # 探活 + 须已灌库
            if await RulesKbRepository(s).is_empty():
                pytest.skip("rules_kb 表空，先跑 scripts/load_rules_kb.py")
            yield s
    except Exception as e:
        pytest.skip(f"无 DB：{e}")
    finally:
        await eng.dispose()


@needs_key
async def test_semantic_recall_paraphrase(session):
    # 换词召回：语料用"封号"，查"店铺被关停"——纯词法漏，向量应命中。
    hits = await RulesKbService(session).search("店铺被关停怎么办", platform="ozon")
    assert hits, "语义近义查询应靠向量召回"
    assert all(h["platform"] == "ozon" for h in hits)


@needs_key
async def test_platform_isolation_db_path(session):
    # SQL 路径 platform 硬隔离：amazon 查询绝不串 ozon。
    hits = await RulesKbService(session).search("知识产权侵权投诉", platform="amazon")
    assert hits
    assert all(h["platform"] == "amazon" for h in hits)


@needs_key
async def test_absent_platform_empty(session):
    # 不存在平台 → 空（隔离 → 触发"不知道"）。
    assert await RulesKbService(session).search("禁售商品", platform="ebay") == []


@needs_key
async def test_nonsense_returns_empty(session):
    # 无关查询 → 空。纯向量永返最近邻，候选闸(_VEC_DIST_MAX)守住反幻觉。
    assert await RulesKbService(session).search("量子物理薛定谔的猫", platform="ozon") == []


@needs_key
async def test_global_rules_match_any_site(session):
    # GLOBAL 规则适用任意 site 查询（模型常猜错 site，不放宽会假性 0 命中）。
    hits = await RulesKbService(session).search("佣金费用", platform="ozon", site="US")
    assert hits
    assert all(h["site"].lower() in ("us", "global") for h in hits)


@needs_key
async def test_return_fields_contract(session):
    hits = await RulesKbService(session).search("取消率", platform="ozon")
    assert hits
    assert set(hits[0].keys()) == _RETURN_FIELDS, "DB 路径须严守 _RETURN_FIELDS 契约"


@needs_key
async def test_limit_respected(session):
    hits = await RulesKbService(session).search("取消", platform="ozon", limit=3)
    assert len(hits) <= 3


# ---- 优雅回退（不需 DB/key）----

async def test_db_error_falls_back_to_jsonl():
    """session 存在但 DB 调用抛错 → 回退 jsonl，不向上抛、仍出结果。"""
    class _BoomSession:
        async def execute(self, *a, **k):
            raise RuntimeError("boom")

    hits = await RulesKbService(_BoomSession()).search("取消率", platform="ozon")
    assert hits, "DB 异常应优雅回退 jsonl 词法路径"
    assert all(h["platform"] == "ozon" for h in hits)


async def test_empty_table_falls_back_to_jsonl(monkeypatch):
    """DB 可用但表空 → 回退 jsonl（区别于'语义无关'的正确空）。"""
    async def _empty(self, *a, **k):
        return []

    async def _is_empty(self):
        return True

    monkeypatch.setattr(RulesKbRepository, "search_filtered", _empty)
    monkeypatch.setattr(RulesKbRepository, "is_empty", _is_empty)

    class _Sess:
        async def execute(self, *a, **k):
            raise AssertionError("不应触达真实 execute")

    # _search_db 内会先 embed（无 key 则抛→走 except 回退）；有 key 则 repo 被 patch 返空。
    hits = await RulesKbService(_Sess()).search("取消率", platform="ozon")
    assert hits, "表空应回退 jsonl"
