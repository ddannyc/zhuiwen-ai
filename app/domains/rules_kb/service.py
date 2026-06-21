"""规则知识库域 —— 对外唯一接口。

定位见 docs/chat-redesign-plan.md §6 与《规则知识库设计.md》：
chat agent 的 rules_search 工具只调本 service，绝不直接读规则库表/文件。

检索实现（两路，签名/契约不变）：
  - session 有效 → 【主路径】pgvector 向量 + 中文 bigram 词法，RRF 融合（见 _search_db）。
  - session 为 None / 表空 / DB 异常 → 【回退】jsonl 词法（_search_jsonl，离线可跑、优雅降级）。

无论走哪路，都守住计划 §6 硬约束：metadata 硬过滤（platform/site，杜绝串台）+
每条附溯源字段 + 无关查询返回 []（上层据此回"不知道"，不编造）。

返回每条 hit 的字段（与 jsonl schema 对齐，chat agent _format_rules 依赖）：
  summary, source_url, version, confidence, last_verified_at,
  platform, site, rule_domain, verification_status, title
"""
import json
import os
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domains.rules_kb.repository import RulesKbRepository
from app.shared.llm.embeddings import embed_text

# 对外契约字段（只暴露这些，不泄 content 全文等）
_RETURN_FIELDS = (
    "summary", "source_url", "version", "confidence", "last_verified_at",
    "platform", "site", "rule_domain", "verification_status", "title",
)

# RRF 融合常数（标准默认）。
_RRF_K = 60
# 向量候选闸：cosine 距离 ≤ 此值才算"语义足够近"。纯向量对无关 query 也永远返最近邻，
# 故必须设阈值，否则"无关→空"反幻觉契约被击穿。0.55 经真实语料校准：相关 query
# min 距离 0.23~0.50、无关 query min 0.60~0.66，0.55 落在间隙（见 tests/test_rules_kb_pgvector）。
_VEC_DIST_MAX = 0.55

# 进程内缓存：path -> (mtime, rows)。开发期改文件自动失效重载（jsonl 回退用）。
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else _repo_root() / path


def _load(path: Path) -> list[dict[str, Any]]:
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return []
    cached = _CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    _CACHE[key] = (mtime, rows)
    return rows


def _load_corpus(path: Path) -> list[dict[str, Any]]:
    """加载规则语料。目录→合并全部 *_rules.jsonl 并按 rule_id 去重；单文件→只读该文件。"""
    if path.is_dir():
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for fp in sorted(path.glob("*_rules.jsonl")):
            for r in _load(fp):
                rid = r.get("rule_id")
                if rid and rid in seen:
                    continue
                if rid:
                    seen.add(rid)
                rows.append(r)
        return rows
    return _load(path)


def _bigrams(s: str) -> set[str]:
    s = "".join(s.lower().split())
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def _haystack(r: dict[str, Any], field: str) -> str:
    v = r.get(field)
    if isinstance(v, list):
        return " ".join(map(str, v))
    return str(v or "")


def _score(query_grams: set[str], r: dict[str, Any]) -> int:
    """中文友好的词法打分：title 权重最高，summary 次之，content/tags 兜底。"""
    if not query_grams:
        return 0
    return (
        len(query_grams & _bigrams(_haystack(r, "title"))) * 3
        + len(query_grams & _bigrams(_haystack(r, "summary"))) * 2
        + len(query_grams & _bigrams(_haystack(r, "content")))
        + len(query_grams & _bigrams(_haystack(r, "tags")))
    )


def _apply_filters(
    rows: list[dict[str, Any]], platform: Optional[str], site: Optional[str]
) -> list[dict[str, Any]]:
    """platform/site 硬过滤（杜绝跨平台/跨站串台）。GLOBAL 规则适用任意 site 查询。"""
    if platform:
        pf = platform.strip().lower()
        rows = [r for r in rows if str(r.get("platform", "")).lower() == pf]
    if site:
        st = site.strip().lower()
        rows = [r for r in rows if str(r.get("site", "")).lower() in (st, "global")]
    return rows


def _project(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r.get(k) for k in _RETURN_FIELDS}


def _search_jsonl(
    query: str, *, platform: Optional[str], site: Optional[str], limit: int
) -> list[dict[str, Any]]:
    """回退路径：读 jsonl 语料，metadata 硬过滤 + bigram 词法打分（score>0 才算命中）。"""
    rows = _load_corpus(_resolve_path(get_settings().rules_kb_path))
    rows = _apply_filters(rows, platform, site)
    qg = _bigrams(query)
    scored = [(s, r) for r in rows if (s := _score(qg, r)) > 0]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [_project(r) for _, r in scored[:limit]]


def _fuse(
    rows: list[dict[str, Any]], query: str, limit: int
) -> list[dict[str, Any]]:
    """向量+词法 RRF 融合。rows 须带 'dist'（向量距离）。

    候选闸：词法命中(score>0) 或 向量足够近(dist≤阈值)。无候选→[]（守"无关→空"）。
    排名：RRF 合并"向量序"与"词法序"两个排名表，取 top-N。
    """
    qg = _bigrams(query)
    for r in rows:
        r["_lex"] = _score(qg, r)

    pool = [r for r in rows if r["_lex"] > 0 or r["dist"] <= _VEC_DIST_MAX]
    if not pool:
        return []

    # 两个排名表（用 id() 作 key，本次调用内 dict 对象唯一）。
    vec_rank = {id(r): i for i, r in enumerate(sorted(rows, key=lambda r: r["dist"]))}
    lex_sorted = sorted([r for r in rows if r["_lex"] > 0], key=lambda r: -r["_lex"])
    lex_rank = {id(r): i for i, r in enumerate(lex_sorted)}

    def rrf(r: dict[str, Any]) -> float:
        s = 1.0 / (_RRF_K + vec_rank[id(r)])
        if id(r) in lex_rank:
            s += 1.0 / (_RRF_K + lex_rank[id(r)])
        return s

    pool.sort(key=rrf, reverse=True)
    return [_project(r) for r in pool[:limit]]


class RulesKbService:
    def __init__(self, session: Optional[AsyncSession] = None):
        self.session = session

    async def search(
        self, query: str, *,
        platform: Optional[str] = None, site: Optional[str] = None, limit: int = 5,
    ) -> list[dict[str, Any]]:
        """检索平台规则。

        platform/site 大小写无关硬过滤——metadata 隔离是设计红线，查 amazon 绝不串
        ozon；查不到返 []（上层据此回"不知道"）。session 缺失/DB 异常优雅回退 jsonl。
        """
        if self.session is None:
            return _search_jsonl(query, platform=platform, site=site, limit=limit)
        try:
            rows = await self._search_db(query, platform=platform, site=site, limit=limit)
        except Exception:
            return _search_jsonl(query, platform=platform, site=site, limit=limit)
        # 空结果：区分"表空"（→回退 jsonl）与"语义无关"（→正确返空，守反幻觉）。
        if not rows and await RulesKbRepository(self.session).is_empty():
            return _search_jsonl(query, platform=platform, site=site, limit=limit)
        return rows

    async def _search_db(
        self, query: str, *, platform: Optional[str], site: Optional[str], limit: int
    ) -> list[dict[str, Any]]:
        [query_emb] = await embed_text([query])
        rows = await RulesKbRepository(self.session).search_filtered(
            query_emb, platform=platform, site=site
        )
        if not rows:
            return []
        return _fuse(rows, query, limit)
