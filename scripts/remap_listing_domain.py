#!/usr/bin/env python3
"""把被默认灌进 listing 域的规则按 source_url 路径段重映射到正确 rule_domain。

背景：LLM 抽取时 listing 被当兜底域，policies/promotion/work-with-customers 等
非 products 页的规则被错塞进 listing（约 76 条）。本脚本按 source 路径段做保守重映射。

红线：
- 只改 rule_domain=='listing' 的记录；products/prices 页是合法 listing，不碰。
- policies 页内容跨多域（禁售/IP/税务/刊登），无法靠路径单判 → 不动，留人工审。
- 目标域全部校验在受控词表内；越界则跳过并告警。
- 不改 verification_status（仍 needs_review，人工审最终拍板）。
- 默认 dry-run 只打印；--apply 才写回，并先备份 .bak。

用法：
  python scripts/remap_listing_domain.py                 # 预览
  python scripts/remap_listing_domain.py --apply         # 写回（含备份）
"""
import argparse
import collections
import json
import os
import sys
from urllib.parse import urlparse

DEFAULT_PATH = "data/rules_kb/ozon_rules.jsonl"

# 受控 rule_domain 词表（与 ozon_crawler.py RULE_DOMAINS 对齐，校验用）
RULE_DOMAINS = {
    "listing", "category_qualification", "prohibited_products", "intellectual_property",
    "logistics_fulfillment", "returns_refunds", "after_sales", "account_health",
    "penalties", "fees", "advertising", "tax_compliance", "payments",
}

# source 路径段 → 目标域（仅 1:1 明确者）。products/prices 不列入=保留 listing。
# policies 故意不列=跨多域，留人工。
SEGMENT_TO_DOMAIN = {
    "work-with-customers": "after_sales",
    "promotion":           "advertising",
    "ratings":             "account_health",
    "personal-account":    "account_health",
    "analytics":           "account_health",
    "brand-account":       "intellectual_property",
    "accounting":          "payments",
    "fulfillment":         "logistics_fulfillment",
    "returns":             "returns_refunds",
}

# 保留 listing 不动的路径段（合法 listing 来源）
KEEP_LISTING = {"products", "prices"}
# 明确留给人工、不自动改的段
LEAVE_FOR_HUMAN = {"policies"}


def seg_of(url: str) -> str:
    path = urlparse(url).path
    parts = path.split("/en/")
    tail = parts[-1] if len(parts) > 1 else path
    return tail.split("/")[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=DEFAULT_PATH, help="jsonl 路径")
    ap.add_argument("--apply", action="store_true", help="写回（默认仅预览）")
    args = ap.parse_args()

    if not os.path.exists(args.path):
        print("✗ 找不到文件:", args.path, file=sys.stderr)
        return 1

    rows = []
    with open(args.path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    changes = []          # (title, seg, old, new)
    left_human = collections.Counter()
    kept = collections.Counter()
    skipped_badtarget = []

    for r in rows:
        if r.get("rule_domain") != "listing":
            continue
        seg = seg_of(r.get("source_url", ""))
        if seg in KEEP_LISTING:
            kept[seg] += 1
            continue
        if seg in LEAVE_FOR_HUMAN:
            left_human[seg] += 1
            continue
        target = SEGMENT_TO_DOMAIN.get(seg)
        if not target:
            left_human[seg] += 1  # 未知段也留人工
            continue
        if target not in RULE_DOMAINS:
            skipped_badtarget.append((r.get("title"), target))
            continue
        changes.append((r.get("title", ""), seg, "listing", target))
        if args.apply:
            r["rule_domain"] = target

    # 报告
    print("=== 重映射预览 ===" if not args.apply else "=== 已重映射 ===")
    by_target = collections.Counter(c[3] for c in changes)
    for tgt, n in by_target.most_common():
        segs = sorted({c[1] for c in changes if c[3] == tgt})
        print("  listing → %-22s %3d 条  (来自: %s)" % (tgt, n, ", ".join(segs)))
    print("  合计改动:", len(changes))
    print("--- 保留 listing（合法来源）---")
    for s, n in kept.most_common():
        print("  %-22s %3d 条" % (s, n))
    print("--- 留人工审（policies/未知段，未动）---")
    for s, n in left_human.most_common():
        print("  %-22s %3d 条" % (s, n))
    if skipped_badtarget:
        print("⚠ 目标域越界，已跳过:", skipped_badtarget)

    if not args.apply:
        print("\n预览模式。确认无误后加 --apply 写回。")
        return 0

    bak = args.path + ".bak"
    if not os.path.exists(bak):  # 只备份一次原始，防覆盖丢失原文
        with open(bak, "w", encoding="utf-8") as f:
            for line in open(args.path, encoding="utf-8"):
                f.write(line)
        print("已备份原文 →", bak)
    with open(args.path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("✅ 写回 %s，改动 %d 条（仍全部 needs_review）" % (args.path, len(changes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
