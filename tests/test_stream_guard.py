"""T3.1：流式增量守卫 StreamGuard + guard_text（防幻觉单一来源）。"""
from app.domains.chat.stream_guard import StreamGuard, guard_text


def test_guard_text_leak_scrubbed():
    out = guard_text('请立即调用 rules_search 工具，"query": "x"', {"type": "answer"})
    assert "rules_search" not in out and "抱歉" in out


def test_guard_text_false_cite_scrubbed():
    out = guard_text("根据 Ozon 官方规则库，折扣不得低于 85%。", {"type": "answer"})
    assert "官方规则库" not in out and "未能从平台规则库取证" in out


def test_guard_text_clean_passthrough():
    out = guard_text("美国宠物用品蓝海明显。", {"type": "answer"})
    assert out == "美国宠物用品蓝海明显。"


def test_guard_text_rules_search_allows_cite_words():
    # rules_search 路径本就带官方/规则库措辞（真检索）→ 不误杀。
    out = guard_text("依据官方政策，取消率超 40% 封 3 天。", {"type": "rules_search"})
    assert out == "依据官方政策，取消率超 40% 封 3 天。"


def test_stream_guard_clean_deltas_no_hit():
    g = StreamGuard({"type": "answer"})
    for d in ["美国", "宠物", "蓝海"]:
        assert g.feed(d) is None
    assert g.text == "美国宠物蓝海"


def test_stream_guard_detects_leak_midstream():
    g = StreamGuard({"type": "answer"})
    assert g.feed("我先") is None
    assert g.feed("调用 rules") is None        # 还没凑出完整模式
    fb = g.feed("_search 工具")                # 此刻 buffer 含 rules_search → 命中
    assert fb is not None and "抱歉" in fb


def test_stream_guard_false_cite_only_when_not_rules():
    # 非检索路径命中假引用
    g = StreamGuard({"type": "answer"})
    assert g.feed("根据官方规则库") is not None
    # 检索路径不查假引用（真检索带官方措辞正常）
    g2 = StreamGuard({"type": "rules_search"})
    assert g2.feed("根据官方规则库") is None
