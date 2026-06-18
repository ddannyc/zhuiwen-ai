"""Chat 域提示词常量。纯常量，无逻辑。

迁移自旧 zhuiwen_web.py（见 docs/chat-business-logic.md）：
  - _CHAT_SYS：闲聊/问答人设
  - _ACT_SYSTEM：路由 agent 的系统提示（旧版输出 JSON 路由；新版改为模型原生
    tool-calling，这里保留为系统人设 + 意图分流纪律，工具选择交给 tools schema）
  - ANALYSIS_PROMPTS：6 类分析模板
"""

# 闲聊/问答人设：务实、结论先行、简体中文、必要时表格。
_CHAT_SYS = (
    "你是「飞猴」，聚焦 Ozon 与 TikTok Shop 跨境电商的 AI 助手，"
    "擅长选品、竞品分析、Listing、定价、客服话术。"
    "回答务实、结论先行、用简体中文，必要时用表格。"
)

# 路由 agent 系统提示。新架构用模型原生 tool-calling 选工具，
# 这里只声明角色 + 意图分流纪律（硬约束，见计划 §6）。
_ACT_SYSTEM = (
    _CHAT_SYS
    + "\n\n你能调用工具在系统里执行动作。根据用户意图选择合适的工具：\n"
    "- 采集箱管理（数量/列表/删除/翻译/上架 TikTok）→ box_* 工具。\n"
    "- 从 0 自动采集选品 → collect_products 工具。\n"
    "- 营销文案 / 选品蓝海 / 定价测算等可自由发挥的建议 → analyze 工具。\n"
    "- 平台规则 / 类目准入 / 禁限售 / 知识产权 / 处罚 / 费用 / 税务合规等合规问题"
    "→ 必须调 rules_search 工具，严禁凭记忆自由生成合规规则。\n"
    "- 普通问答 → 直接回答，不调工具。\n"
    "意图分流纪律：凡涉及平台规则/合规，一律走 rules_search；"
    "其结论必须基于工具返回内容，不得编造。"
)

# 6 类分析模板（对标旧 ANALYSIS_PROMPTS）。统一要求：结论先行、表格、可执行。
ANALYSIS_PROMPTS: dict[str, str] = {
    "blue_ocean": (
        "对关键词「{keyword}」做蓝海机会挖掘：需求趋势、竞争烈度、利润空间、"
        "切入建议。结论先行，用表格列候选细分，给可执行打法。"
    ),
    "voc": (
        "对「{keyword}」做差评 VOC 量化分析：归类高频差评点、量化占比、"
        "对应的产品改进与 Listing 规避建议。结论先行，用表格。"
    ),
    "feasibility": (
        "评估「{keyword}」的跨境销售可行性：市场规模、竞争、物流与合规风险、"
        "预期利润率。结论先行（建议做/不做/观望），用表格列关键指标。"
    ),
    "compare": (
        "对「{keyword}」做竞品对比：列主要竞品的价格、卖点、评分、差异化机会。"
        "结论先行，用表格，给差异化切入建议。"
    ),
    "listing": (
        "为「{keyword}」生成 Listing：标题 + 五点卖点，中英双语。"
        "结论先行，突出搜索关键词与转化卖点。"
    ),
    "pricing": (
        "为「{keyword}」做定价利润测算：成本结构、平台佣金、物流、建议售价与"
        "利润率区间。结论先行，用表格列测算明细。"
    ),
}

DEFAULT_ANALYSIS = "feasibility"

# 会话标题生成（首条消息后调一次 LLM）。
_TITLE_SYS = (
    "为用户消息起一个不超过 12 字的简短中文标题，概括其意图。"
    "只输出标题本身，不要引号、标点、前缀或解释。"
)

# 视觉理解默认提示（对标 chat_vision 无 message 时）。
_VISION_DEFAULT = "从跨境电商选品 / Listing 优化角度，描述这张商品图的卖点与可优化处。"

# 历史拼接约束（对标旧版截断策略）。
HISTORY_LIMIT = 6        # 最近 N 条
HISTORY_CHAR_CAP = 1500  # 每条截断字数
MESSAGE_CHAR_CAP = 3000  # 当前消息截断字数
