"""规则知识库域 —— 对外唯一接口。

定位见 docs/chat-redesign-plan.md §6 与《规则知识库设计.md》：
chat agent 的 rules_search 工具只调本 service，绝不直接读规则库表/文件。

本期实现：读 data/rules_kb/ 下全部 *_rules.jsonl（多平台 needs_review 规则语料：
ozon/amazon/tiktok/temu/shein/mercadolibre…），做 metadata 硬过滤（platform/site，
杜绝串台）+ 中文 bigram 词法打分检索。
后续升级 Postgres + pgvector + BM25 混合检索时只换实现、不改签名（契约定死）。

返回每条 hit 的字段（与 jsonl schema 对齐，chat agent _format_rules 依赖）：
  summary, source_url, version, confidence, last_verified_at,
  platform, site, rule_domain, verification_status
"""
import json
import os
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings

# 返回给上层的契约字段（只暴露这些，不泄露 content 全文等）
_RETURN_FIELDS = (
    "summary", "source_url", "version", "confidence", "last_verified_at",
    "platform", "site", "rule_domain", "verification_status", "title",
)

# 进程内缓存：path -> (mtime, rows)。开发期改文件自动失效重载。
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _repo_root() -> Path:
    # app/domains/rules_kb/service.py → 上溯 4 层到仓库根
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
    """加载规则语料。

    path 为目录：合并其下全部 *_rules.jsonl（多平台语料），靠 glob 后缀天然排除
      *.process_*.jsonl / raw/ 等中间产物；按 rule_id 去重（防同条多文件重复）。
    path 为单文件：只读该文件（向后兼容旧的单 jsonl 配置）。
    每个文件独立走 _load 的 mtime 缓存，单文件改动只失效自身。
    """
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


class RulesKbService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def search(
        self,
        query: str,
        *,
        platform: Optional[str] = None,
        site: Optional[str] = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """检索平台规则。

        platform/site 做大小写无关的硬过滤 —— metadata 隔离是设计红线，
        查 amazon 绝不会串到 ozon 的规则；查不到就返回 []（上层据此回"不知道"）。
        """
        rows = _load_corpus(_resolve_path(get_settings().rules_kb_path))

        # 1) metadata 硬过滤（杜绝跨平台/跨站串台）
        if platform:
            pf = platform.strip().lower()
            rows = [r for r in rows if str(r.get("platform", "")).lower() == pf]
        if site:
            st = site.strip().lower()
            # GLOBAL 规则适用所有站点，故匹配任意 site 查询；否则按 site 精确过滤。
            # （模型常把 Ozon 猜成 site=RU，而数据是 GLOBAL —— 不放宽会假性 0 命中。）
            rows = [r for r in rows if str(r.get("site", "")).lower() in (st, "global")]

        # 2) 词法打分 + 取 top-N（score>0 才算命中）
        qg = _bigrams(query)
        scored = [(s, r) for r in rows if (s := _score(qg, r)) > 0]
        scored.sort(key=lambda x: x[0], reverse=True)

        return [{k: r.get(k) for k in _RETURN_FIELDS} for _, r in scored[:limit]]
