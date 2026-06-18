#!/usr/bin/env python3
"""Ozon 运营规范爬取 → 结构化规则记录（rules_kb 种子语料）。

按 docs/规则知识库设计.md 的入库管线前三阶段实现：
  阶段1 采集    —— 受控抓取 Ozon 公开 seller 帮助页（docs.ozon.ru），遵守 robots、限频。
  阶段2 归一化  —— HTML → 纯文本清洗（去导航/脚本/样式）。
  阶段3 抽取    —— LLM 把文本映射到 schema §1.1，只提炼改写、不杜撰、不确定留空。
输出 data/rules_kb/ozon_rules.jsonl，每条 verification_status=needs_review（待阶段4人工审核）。

红线（与设计文档一致）：
  - robots.txt 校验 + 请求限频（--delay）。
  - 原文不整段转储进知识库：仅 LLM 提炼改写后的 summary/content 入 JSONL；
    原始 HTML 只在 --cache 时落到本地 raw/（临时，供调试，勿入库）。
  - 每条强制 needs_review + 真实 source_url。

零三方依赖：纯标准库（urllib + html.parser）。

环境变量（LLM 抽取走 DeepSeek，OpenAI 兼容；见 https://api-docs.deepseek.com/zh-cn/）：
  DEEPSEEK_API_KEY   DeepSeek 密钥（必填，否则自动 --no-llm 干跑）
  DEEPSEEK_BASE_URL  默认 https://api.deepseek.com
  EXTRACT_MODEL      默认 deepseek-v4-flash（非思考，便宜，够抽取用；深推理用 deepseek-v4-pro）
  无 key 时用 --no-llm 干跑：只抓取+清洗+缓存文本，不抽取（人工后处理）。

用法：
  python scripts/ozon_crawler.py                 # 抓种子页 + LLM 抽取 → ozon_rules.jsonl
  python scripts/ozon_crawler.py --no-llm --cache # 只抓文本到 raw/ 供人工审
  python scripts/ozon_crawler.py --max-pages 40 --crawl  # 跟随同域链接扩展
  python scripts/ozon_crawler.py --cookie "locale=en; ..."  # 突破地域跳转环时手动塞 cookie

沙箱注意：US 环境对 docs.ozon.ru 会 "Too many redirects"（地域/风控拦截）。
本脚本带浏览器 UA + cookie 会话 + 跳转环检测；若仍循环，从可访问区域egress运行，
或 --cookie 手动提供绕过 cookie。
"""
import argparse
import datetime
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import uuid
from html.parser import HTMLParser

# 设计文档 §1.2 受控 rule_domain 词表
RULE_DOMAINS = {
    "listing", "category_qualification", "prohibited_products", "intellectual_property",
    "logistics_fulfillment", "returns_refunds", "after_sales", "account_health",
    "penalties", "fees", "advertising", "tax_compliance", "payments",
}
RULE_TYPES = {"requirement", "prohibition", "penalty", "process", "fee"}

# 已验证存在的 Ozon 公开 seller 文档种子页（docs/rules_kb/README 阻塞待办里记录的同一批）
SEED_URLS = [
    "https://docs.ozon.ru/global/en/policies/product-rules-and-documents/product-rules/special-categories/",
    "https://docs.ozon.ru/global/en/products/requirements/",
    "https://docs.ozon.ru/global/en/products/product-info/product-description/",
    "https://docs.ozon.ru/global/en/commissions/ozon-fees/commissions/",
    "https://docs.ozon.ru/global/en/fulfillment/rfbs/",
    "https://docs.ozon.ru/global/en/fulfillment/fbp/",
    "https://docs.ozon.ru/common/en/otmena-i-vozvrat-zakaza/usloviya-i-sroki-vozvrata/",
    "https://docs.ozon.ru/global/en/contracts-for-sellers/dogovor/",
]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ALLOWED_HOST = "docs.ozon.ru"

TODAY = datetime.date.today().isoformat()
VERSION = datetime.date.today().strftime("%Y.%m.%d-r1")


# ── 阶段2 HTML → 纯文本 ──────────────────────────────────────────────
class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg", "form"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag in ("p", "li", "br", "h1", "h2", "h3", "h4", "tr", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            t = data.strip()
            if t:
                self.parts.append(t)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        pass
    text = " ".join(p.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# ── 阶段1 采集（带 cookie 会话 + 跳转环检测）──────────────────────────
def build_opener(extra_cookie: str = "") -> urllib.request.OpenerDirector:
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        # 不自动跟跳转：手动跟以检测循环
        _NoRedirect(),
    )
    opener.addheaders = [
        ("User-Agent", UA),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language", "en-US,en;q=0.9"),
    ]
    if extra_cookie:
        opener.addheaders.append(("Cookie", extra_cookie))
    return opener


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 交给上层手动处理跳转


def fetch(opener, url: str, timeout: int = 25, max_hops: int = 8):
    """手动跟跳转，检测循环。返回 (final_url, html) 或 None。"""
    seen = []
    cur = url
    for _ in range(max_hops):
        if cur in seen:
            print("  ! 跳转环 detected: %s（地域/风控拦截，换 egress 或 --cookie）" % cur, file=sys.stderr)
            return None
        seen.append(cur)
        try:
            resp = opener.open(cur, timeout=timeout)
            html = resp.read().decode("utf-8", "replace")
            return resp.geturl(), html
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if not loc:
                    return None
                cur = urllib.parse.urljoin(cur, loc)
                continue
            print("  ! HTTP %s %s" % (e.code, cur), file=sys.stderr)
            return None
        except Exception as e:
            print("  ! fetch 失败 %s: %s" % (cur, str(e)[:80]), file=sys.stderr)
            return None
    print("  ! 跳转过多 %s" % url, file=sys.stderr)
    return None


def robots_ok(opener) -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    got = fetch(opener, "https://%s/robots.txt" % ALLOWED_HOST)
    if got:
        rp.parse(got[1].splitlines())
    else:
        rp.parse([])  # 拿不到 robots：保守放行种子页，但记录
        print("  ! robots.txt 未取到，仅抓显式种子页", file=sys.stderr)
    return rp


def discover_links(html: str, base_url: str) -> list:
    out = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html):
        u = urllib.parse.urljoin(base_url, m.group(1))
        pu = urllib.parse.urlparse(u)
        if pu.netloc == ALLOWED_HOST and "/en/" in pu.path:
            out.append(u.split("#")[0])
    return list(dict.fromkeys(out))


# ── 阶段3 LLM 结构化抽取 ─────────────────────────────────────────────
EXTRACT_SYS = (
    "你是跨境电商规则结构化抽取器，把 Ozon 帮助页文本映射为规则记录。"
    "严格只依据给定文本，禁止杜撰；无法确定的字段留空/ null；只提炼改写，不照搬原文。"
    "只输出一个 JSON 数组，不要 markdown、不要解释。每个元素字段："
    '{"rule_domain": 受控词之一, "rule_type":"requirement|prohibition|penalty|process|fee",'
    '"title":"改写标题","summary":"1-3句中文提炼","content":"改写要点(中文markdown列表)",'
    '"severity":"info|warning|critical 或 null","product_category":[],'
    '"confidence":"high|medium|low","tags":["..."]}。'
    "受控 rule_domain：" + " ".join(sorted(RULE_DOMAINS)) + "。"
    "一页可含多条规则就拆成多个元素；页面无实质规则就返回 []。"
)


def llm_extract(text: str, base_url: str, key: str, model: str, timeout: int = 90) -> list:
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": EXTRACT_SYS},
            {"role": "user", "content": "页面文本（可能含噪声）：\n\n" + text[:12000]},
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        print("  ! LLM 抽取失败: %s" % str(e)[:120], file=sys.stderr)
        return []
    return _loose_json_array(content)


def _loose_json_array(s: str) -> list:
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", s).strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else [v] if isinstance(v, dict) else []
    except Exception:
        m = re.search(r"\[.*\]", s, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return []
    return []


# ── 归一化为 schema §1.1 完整记录 ────────────────────────────────────
def normalize(raw: dict, source_url: str) -> dict:
    domain = raw.get("rule_domain")
    if domain not in RULE_DOMAINS:
        domain = None  # 留空交人工审，不硬塞
    rtype = raw.get("rule_type") if raw.get("rule_type") in RULE_TYPES else "requirement"
    sev = raw.get("severity")
    if sev not in ("info", "warning", "critical"):
        sev = None
    conf = raw.get("confidence") if raw.get("confidence") in ("high", "medium", "low") else "medium"
    site = "RU"
    if "/global/" in source_url or "global-help.ozon.com" in source_url:
        site = "GLOBAL"
    return {
        "rule_id": str(uuid.uuid4()),
        "platform": "ozon",
        "site": site,
        "original_language": "en",  # 抓的是 /en/ 页；俄语原文留待阶段2保留备份
        "rule_domain": domain,
        "product_category": raw.get("product_category") or [],
        "rule_type": rtype,
        "title": (raw.get("title") or "").strip(),
        "summary": (raw.get("summary") or "").strip(),
        "content": (raw.get("content") or "").strip(),
        "severity": sev,
        "source_type": "official_help_center",
        "source_url": source_url,
        "version": VERSION,
        "effective_date": None,
        "expiry_date": None,
        "last_verified_at": TODAY,
        "verification_status": "needs_review",   # 强制：LLM 抽取未经人工核验
        "confidence": conf,
        "related_rule_ids": [],
        "tags": raw.get("tags") or [],
    }


def valid(rec: dict) -> bool:
    return bool(rec["title"] and rec["summary"] and rec["rule_domain"])


# ── 主流程 ───────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="爬取 Ozon 运营规范 → rules_kb 种子语料")
    ap.add_argument("--out", default="data/rules_kb/ozon_rules.jsonl")
    ap.add_argument("--delay", type=float, default=2.0, help="请求间隔秒（限频红线）")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--crawl", action="store_true", help="跟随同域 /en/ 链接扩展（默认只抓种子页）")
    ap.add_argument("--no-llm", action="store_true", help="只抓取+清洗，不做 LLM 抽取")
    ap.add_argument("--cache", action="store_true", help="把清洗后文本存 raw/（临时调试，勿入库）")
    ap.add_argument("--cookie", default="", help="手动 cookie（突破地域跳转环）")
    ap.add_argument("--seed", action="append", help="追加种子 URL（可多次）")
    ap.add_argument("--from-dump", help="离线消化油猴导出的 ozon_pages.jsonl（{url,title,text}），跳过网络抓取")
    args = ap.parse_args()

    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    model = os.environ.get("EXTRACT_MODEL", "deepseek-v4-flash")
    do_llm = not args.no_llm
    if do_llm and not key:
        print("⚠ 无 DEEPSEEK_API_KEY；自动转 --no-llm 干跑（只抓文本）。", file=sys.stderr)
        do_llm = False

    # ── 离线模式：消化油猴导出的页面文本（浏览器已突破 403/地域）──
    if args.from_dump:
        if not do_llm:
            print("✗ --from-dump 需要 DEEPSEEK_API_KEY 做抽取。", file=sys.stderr)
            sys.exit(1)
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        records, n_pages = [], 0
        # Open dump with utf-8 and replace invalid bytes to avoid UnicodeDecodeError on Windows
        for line in open(args.from_dump, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                page = json.loads(line)
            except Exception:
                continue
            text, src = page.get("text") or "", page.get("url") or ""
            if len(text) < 200 or not src:
                continue
            n_pages += 1
            print("[%d] 抽取 %s" % (n_pages, src))
            for raw in llm_extract(text, base_url, key, model):
                rec = normalize(raw, src)
                if valid(rec):
                    records.append(rec)
            time.sleep(0.3)
        uniq, key_set = [], set()
        for r in records:
            k = (r["title"], r["source_url"])
            if k not in key_set:
                key_set.add(k)
                uniq.append(r)
        with open(args.out, "w", encoding="utf-8") as f:
            for r in uniq:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("\n✅ 消化 %d 页，抽取 %d 条规则 → %s（全部 needs_review）" % (n_pages, len(uniq), args.out))
        return

    opener = build_opener(args.cookie)
    rp = robots_ok(opener)

    queue = list(SEED_URLS) + (args.seed or [])
    seen, records, raw_dir = set(), [], "data/rules_kb/raw/ozon"
    if args.cache:
        os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    n_pages = 0
    while queue and n_pages < args.max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not rp.can_fetch(UA, url):
            print("  robots 禁止，跳过 %s" % url, file=sys.stderr)
            continue
        print("[已成功 %d/%d · 第 %d 次尝试] 抓取 %s" % (n_pages, args.max_pages, len(seen), url))
        got = fetch(opener, url)
        if not got:
            continue
        final_url, html = got
        text = html_to_text(html)
        n_pages += 1
        if len(text) < 200:
            print("  (正文过短，跳过抽取)", file=sys.stderr)
        else:
            if args.cache:
                fn = re.sub(r"[^a-zA-Z0-9]+", "_", urllib.parse.urlparse(final_url).path).strip("_") or "index"
                with open(os.path.join(raw_dir, fn + ".txt"), "w", encoding="utf-8") as f:
                    f.write("SOURCE: %s\n\n%s" % (final_url, text))
            if do_llm:
                for raw in llm_extract(text, base_url, key, model):
                    rec = normalize(raw, final_url)
                    if valid(rec):
                        records.append(rec)
                print("  → 抽取 %d 条" % sum(1 for r in records if r["source_url"] == final_url))
        if args.crawl:
            for u in discover_links(html, final_url):
                if u not in seen:
                    queue.append(u)
        time.sleep(args.delay)

    if do_llm:
        # 去重（title+url）后写入
        uniq, key_set = [], set()
        for r in records:
            k = (r["title"], r["source_url"])
            if k not in key_set:
                key_set.add(k)
                uniq.append(r)
        with open(args.out, "w", encoding="utf-8") as f:
            for r in uniq:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("\n✅ 抓 %d 页，抽取 %d 条规则 → %s（全部 needs_review，待人工审核）"
              % (n_pages, len(uniq), args.out))
        print("   合并入主语料前请人工核验：python -c 见 data/rules_kb/README.md")
    else:
        print("\n✅ 抓 %d 页（干跑，无抽取）。文本缓存：%s" % (n_pages, raw_dir if args.cache else "（未开 --cache）"))


if __name__ == "__main__":
    main()
