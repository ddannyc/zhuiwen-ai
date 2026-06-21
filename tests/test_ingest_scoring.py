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


async def test_score_candidates_handles_non_list_llm():
    """模型返回非数组（异常）→ 不崩，全部 score 0、不达标。"""
    async def bad_llm(system, user):
        return {"oops": 1}

    res = await score_candidates(
        [{"id": "1", "title": "x", "price_cny": 1, "source_url": "u"}],
        threshold=70, top_n=0, llm_json=bad_llm,
    )
    assert res["scores"][0]["score"] == 0.0
    assert res["scores"][0]["pass"] is False


async def test_score_candidates_partial_scores_default_zero():
    """模型只评了部分候选 → 缺的按 0 分处理。"""
    cands = [
        {"id": "1", "title": "a", "price_cny": 1, "source_url": "u"},
        {"id": "2", "title": "b", "price_cny": 1, "source_url": "u"},
    ]

    async def partial(system, user):
        return [{"i": 0, "score": 90}]  # 只评了第 0 个

    res = await score_candidates(cands, threshold=70, top_n=0, llm_json=partial)
    s2 = next(s for s in res["scores"] if s["id"] == "2")
    assert s2["score"] == 0.0 and s2["pass"] is False


def test_loose_json_array_variants():
    from app.domains.sourcing.ingest import loose_json_array

    assert loose_json_array('[{"i": 0}]') == [{"i": 0}]
    assert loose_json_array('```json\n[{"i": 1}]\n```') == [{"i": 1}]
    assert loose_json_array("结果如下：[{\"i\": 2}] 完") == [{"i": 2}]
    assert loose_json_array("") == []
    assert loose_json_array("not json") == []
    assert loose_json_array('{"i": 0}') == []  # 对象不是数组
