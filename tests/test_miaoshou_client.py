"""Phase1 T1.A：妙手 CLI 客户端封装（纯单测，fake runner，无需 DB/真妙手）。

验收（tasks/plan.md T1.A）：fake SELECT_CMD → client 解析正确；超时/非零退出/解析失败
→ 结构化错误（MiaoshouError）。各 mode 参数构造对齐旧 zhuiwen_web 调用点。
"""
import json

import pytest

from app.domains.sourcing.miaoshou import MiaoshouClient, MiaoshouError

_OFFER = "https://detail.1688.com/offer/1.html"


def _client(returns, capture=None):
    def fake_runner(cmd, timeout, env):
        if capture is not None:
            capture.append((cmd, timeout))
        return returns

    return MiaoshouClient(cmd=["select"], env={}, runner=fake_runner)


def test_url_fetch_parses_and_builds_args():
    cap = []
    c = _client((0, json.dumps([{"id": "1", "title": "x"}]), ""), cap)
    out = c.url_fetch([_OFFER])
    assert out == [{"id": "1", "title": "x"}]
    cmd = cap[0][0]
    assert cmd[:3] == ["select", "--mode", "url"]
    assert "--urls" in cmd and _OFFER in cmd


def test_url_fetch_empty_raises():
    with pytest.raises(MiaoshouError):
        _client((0, "[]", "")).url_fetch([])


def test_edit_builds_changes_json():
    cap = []
    c = _client((0, '{"ok":true}', ""), cap)
    c.edit("123", {"title": "新标题"})
    cmd = cap[0][0]
    assert cmd[1:5] == ["--mode", "edit", "--id", "123"]
    i = cmd.index("--changes")
    assert json.loads(cmd[i + 1]) == {"title": "新标题"}


def test_delete_builds_ids():
    cap = []
    c = _client((0, '{"deleted":2}', ""), cap)
    assert c.delete(["1", "2"]) == {"deleted": 2}
    cmd = cap[0][0]
    assert cmd[1:3] == ["--mode", "delete"]
    assert "1" in cmd and "2" in cmd


def test_delete_empty_noop():
    assert _client((0, "{}", "")).delete([]) == {"deleted": 0}


def test_shops_parses_array():
    assert _client((0, '[{"shopId":9}]', "")).shops() == [{"shopId": 9}]


def test_tkcall_builds_body():
    cap = []
    c = _client((0, '{"ok":1}', ""), cap)
    c.tkcall("/category/get", {"x": 1})
    cmd = cap[0][0]
    assert cmd[1:5] == ["--mode", "tkcall", "--tk", "/category/get"]
    i = cmd.index("--body")
    assert json.loads(cmd[i + 1]) == {"x": 1}


def test_nonzero_exit_raises_with_stderr_tail():
    c = _client((1, "", "前面噪声\n凭证无效"))
    with pytest.raises(MiaoshouError) as e:
        c.shops()
    assert "凭证无效" in str(e.value)


def test_timeout_raises():
    with pytest.raises(MiaoshouError):
        _client((124, "", "命令超时")).url_fetch([_OFFER])


def test_bad_json_raises():
    with pytest.raises(MiaoshouError):
        _client((0, "not json", "")).shops()
