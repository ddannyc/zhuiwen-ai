"""chat 终答防幻觉守卫——单一来源（非流式 + 流式增量共用，规则不漂移）。

- guard_text(reply, action)：对完整文本做守卫（非流式 converse / 流式累积兜底）。
- StreamGuard：流式增量守卫，每 delta 喂入；命中即返回 fallback 文案
  （converse_stream 据此停流 + 发 replace 事件 + 落库 fallback）。

两闸：
- 泄露：模型把内部工具名/调用参数/调用计划当回复吐出 → 内部过程泄露。
- 假引用：回复声称官方/规则库背书，但本轮没真检索（action≠rules_search）→ 欺骗性幻觉。

⚠ 边界声明（ship 评审 H2/H3）：本守卫是**正则黑名单，语义不全、尽力而为**，不是安全边界。
真正的结构控制是：
  1) 召回闸 `_COMPLIANCE_RE`（service）——任何平台政策类问题强制走 rules_search，答案带真出处、
     查不到则 EMPTY_RULES_FALLBACK，不进自由生成；
  2) 租户隔离靠 RLS（DB 层），不靠本守卫。
即"防编造合规结论"主要靠①把问题逼进检索，本守卫只兜漏网的措辞，勿当唯一防线。
"""
import re

# 泄露探测：内部工具名（snake_case，绝不该出现在用户回复）、调用参数 JSON、
# 以及模型把"调用计划/反问是否调用"当回复正文吐出的话术。命中即判为内部过程泄露。
_LEAK_RE = re.compile(
    r"rules_search|box_list|box_count|box_delete|box_translate|box_list_tiktok|collect_products"
    r'|"\s*(?:query|platform|keywords)\s*"\s*:'
    r"|是否需要我.{0,12}(?:发起|调用|查询)"
    r"|(?:立即|请|我将|让我)\s*调用.{0,8}工具"
    r"|发起(?:该|这个|此)?查询",
    re.IGNORECASE,
)
_LEAK_FALLBACK = (
    "抱歉，我刚才没能正确处理这个问题。请换种说法再问一次，或更具体地描述你的需求。"
)

# 假引用闸：回复声称官方/规则库背书，但本轮没真检索（action≠rules_search）→ 欺骗性幻觉。
_VERIFY_CLAIM_RE = re.compile(
    r"依据官方|官方文档|官方规则|规则库|附来源|经核查|官方政策|已(?:通过|经).{0,6}(?:验证|核查)"
)
_FALSE_CITE_FALLBACK = (
    "抱歉，这个问题我未能从平台规则库取证，无法给出确证的合规结论。"
    "请以平台官方最新公告为准，或换个更具体的问法以便我检索。"
)


# 守卫模式的最长跨度上界（含 .{0,12} 等有界间隔）。两用：
#  - StreamGuard 只扫 buffer 尾窗（_WINDOW > 此值）→ O(n) 总开销，仍能抓跨 delta 凑齐的模式；
#  - converse_stream 发送时留此长度的"尾巴"不发 → 不安全区在守卫看清前永不传输给前端（H1）。
GUARD_TAIL = 48
_WINDOW = 64  # 尾窗扫描长度（> GUARD_TAIL，确保被扣留的尾巴始终在扫描范围内）


def guard_text(reply: str, action: dict) -> str:
    """完整文本守卫：命中返回 fallback，否则原文。"""
    if action.get("type") != "rules_search" and _VERIFY_CLAIM_RE.search(reply):
        return _FALSE_CITE_FALLBACK
    if _LEAK_RE.search(reply):
        return _LEAK_FALLBACK
    return reply


class StreamGuard:
    """流式增量守卫：累积 delta，命中守卫返回 fallback 文案（应停流 + 替换），否则 None。
    全程在线（每 delta 查 running buffer），不是事后补——正常回复不触发，少数出事即拦。"""

    def __init__(self, action: dict) -> None:
        self._buf = ""
        self._is_rules = action.get("type") == "rules_search"

    def feed(self, delta: str) -> str | None:
        self._buf += delta
        # 只扫尾窗（不是整个 buffer）：模式刚凑齐时其完整跨度必在最后 _WINDOW 字符内，
        # 故能抓跨 delta 分割的模式，且每 delta O(_WINDOW) 而非 O(len)，长回复不退化（perf）。
        tail = self._buf[-_WINDOW:]
        if _LEAK_RE.search(tail):
            return _LEAK_FALLBACK
        if not self._is_rules and _VERIFY_CLAIM_RE.search(tail):
            return _FALSE_CITE_FALLBACK
        return None

    @property
    def text(self) -> str:
        return self._buf
