"""Phase2 T2.3：评分 + 违禁词清洗（纯逻辑，无 DB，注入 fake llm_json）。

移植自旧 zhuiwen_web._score_candidates / _clean_title。
"""
import pytest

from app.domains.sourcing.ingest import clean_title, score_candidates


def test_clean_title_strips_banwords():
    assert clean_title("蓝牙耳机 工厂直销 1688 批发") == "蓝牙耳机"
    assert clean_title("Amazon 同款 数据线") == "同款 数据线"


async def test_score_candidates_threshold_and_clean():
    cands = [
        {"id": "1", "title": "蓝牙耳机 工厂直销 批发", "price_cny": 50, "source_url": "u1"},
        {"id": "2", "title": "数据线", "price_cny": 5, "source_url": "u2"},
    ]

    async def fake_llm(system, user):
        return [
            {"i": 0, "score": 85, "title_en": "BT Earbuds", "reason": "hot", "category": "Elec"},
            {"i": 1, "score": 40, "title_en": "Cable", "reason": "low", "category": "Elec"},
        ]

    res = await score_candidates(cands, threshold=70, top_n=0, llm_json=fake_llm)
    assert res["count"] == 2
    assert res["passed"] == 1
    s0 = next(s for s in res["scores"] if s["id"] == "1")
    assert s0["pass"] is True
    assert s0["title"] == "蓝牙耳机"  # 违禁词 工厂直销/批发 清掉
    assert s0["title_en"] == "BT Earbuds"
    s1 = next(s for s in res["scores"] if s["id"] == "2")
    assert s1["pass"] is False


async def test_score_candidates_top_n_overrides_threshold():
    cands = [
        {"id": "1", "title": "A", "price_cny": 1, "source_url": "u"},
        {"id": "2", "title": "B", "price_cny": 1, "source_url": "u"},
    ]

    async def fake_llm(system, user):
        return [{"i": 0, "score": 90}, {"i": 1, "score": 80}]

    res = await score_candidates(cands, threshold=70, top_n=1, llm_json=fake_llm)
    passed = [s for s in res["scores"] if s["pass"]]
    assert len(passed) == 1
    assert passed[0]["id"] == "1"  # 只留最高分


async def test_score_candidates_empty():
    async def fake_llm(system, user):
        return []

    res = await score_candidates([], threshold=70, top_n=0, llm_json=fake_llm)
    assert res == {"count": 0, "passed": 0, "scores": []}
