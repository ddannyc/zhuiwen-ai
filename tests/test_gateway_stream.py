"""T0.1：gateway.chat_stream —— litellm 流式，逐 delta 产出（空块跳过）。"""
from app.shared.llm import gateway


class _Delta:
    def __init__(self, c):
        self.content = c


class _Choice:
    def __init__(self, c):
        self.delta = _Delta(c)


class _Chunk:
    def __init__(self, c):
        self.choices = [_Choice(c)]


async def test_chat_stream_yields_deltas(monkeypatch):
    seen_kwargs = {}

    async def fake_acompletion(**kw):
        seen_kwargs.update(kw)

        async def gen():
            for c in ["你好", "，", "世界", None, ""]:  # None/"" 空块应跳过
                yield _Chunk(c)

        return gen()

    monkeypatch.setattr(gateway.litellm, "acompletion", fake_acompletion)

    out = [d async for d in gateway.chat_stream([{"role": "user", "content": "hi"}])]

    assert seen_kwargs.get("stream") is True
    assert out == ["你好", "，", "世界"]
    assert "".join(out) == "你好，世界"


async def test_chat_stream_handles_empty_choices(monkeypatch):
    async def fake_acompletion(**kw):
        async def gen():
            c = _Chunk("ok")
            empty = _Chunk("x")
            empty.choices = []  # 某些 chunk 无 choices
            yield empty
            yield c

        return gen()

    monkeypatch.setattr(gateway.litellm, "acompletion", fake_acompletion)
    out = [d async for d in gateway.chat_stream([{"role": "user", "content": "hi"}])]
    assert out == ["ok"]
