"""Chat 路由 agent（LangGraph + 模型原生 tool-calling）。

对标旧 agent_act()，但弃用脆弱的 JSON 路由 + 正则短路：改由模型 tool-calling
判定意图。守住两条纪律：
  1. LLM 唯一出口是 shared/llm/gateway —— 这里不碰 OpenAI SDK，也不引 langchain-openai，
     在 LangGraph 节点内手动跑工具循环（gateway.chat_with_tools）。
  2. 跨域只调对方 service（BoxService / RulesKbService），不读对方表。

结构化产出（对齐前端 web/src/lib/contract.ts 的 ChatAction 联合类型）：
工具不仅回字符串给模型续写，还产出结构化 payload（rows/cites/job_id），
由 converse 透出给前端渲染富卡片。

意图分流纪律（计划 §6）：合规规则类问题必须走 rules_search，回答受硬约束：
metadata 过滤 + 每条附 source_url+version + 空检索说"不知道"不编造 +
未核验/低置信附警示。
"""
import json
from typing import Any, Callable, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import current_tenant_id
from app.domains.box.service import BoxService
from app.domains.chat.prompts import ANALYSIS_PROMPTS, DEFAULT_ANALYSIS
from app.domains.rules_kb.service import RulesKbService
from app.domains.sourcing.service import SourcingService
from app.shared.llm.gateway import chat, chat_with_tools

# ---- 工具 schema（OpenAI tools 格式，透传给 LiteLLM）----
TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "box_count", "description": "查询采集箱当前商品数量。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "box_list", "description": "列出采集箱商品，可按关键词过滤，最多 30 条。",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "过滤关键词，可空"},
            "limit": {"type": "integer", "description": "返回条数，默认 30"},
        }},
    }},
    {"type": "function", "function": {
        "name": "box_delete", "description": "删除采集箱商品。scope=chinese 仅删未翻译中文标题；scope=all 清空。",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["chinese", "all"]},
        }},
    }},
    {"type": "function", "function": {
        "name": "box_translate", "description": "翻译采集箱商品标题/图片并写回。",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["all", "chinese"]},
            "lang": {"type": "string", "description": "目标语言，如 en/ru/ms"},
            "images": {"type": "boolean", "description": "是否同时翻译图片"},
        }},
    }},
    {"type": "function", "function": {
        "name": "box_list_tiktok", "description": "把采集箱商品上架 TikTok Shop。auto=true 直接发布。",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["all", "chinese"]},
            "auto": {"type": "boolean"},
        }},
    }},
    {"type": "function", "function": {
        "name": "analyze",
        "description": "选品/竞品/定价等营销分析，可自由生成。type 取 "
                       "blue_ocean/voc/feasibility/compare/listing/pricing。",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string"},
            "type": {"type": "string"},
        }, "required": ["keyword"]},
    }},
    {"type": "function", "function": {
        "name": "rules_search",
        "description": "查询平台规则/类目准入/禁限售/知识产权/处罚/费用/税务合规。"
                       "凡合规类问题必须用本工具，不得自由生成。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "platform": {"type": "string", "description": "如 ozon / amazon / tiktok"},
            "site": {"type": "string", "description": "站点，如 US / RU / GLOBAL"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "collect_products",
        "description": ("下发自动采集任务给浏览器插件（仅排队采集，不含翻译/上架——"
                        "那些是后续工作流步骤，本工具不执行，勿向用户声称已翻译/已上架）。"),
        # 只声明 impl 真消费的参数（keywords/perKw/market）。翻译/上架等后处理由
        # sourcing 工作流的 activity 负责（当前为桩），不在此工具暴露，避免模型"设置"
        # 实际不发生的动作（项目反幻觉纪律）。
        "parameters": {"type": "object", "properties": {
            "keywords": {"type": "array", "items": {"type": "string"}},
            "perKw": {"type": "integer"},
            "market": {"type": "string", "description": "目标市场/站点，如 my / us"},
        }},
    }},
    # 路由标记工具（方案B）：无专用工具可用时选它，表示"直接回答"。强制 tool_choice 下
    # 模型必选某工具 → 路由永不自由生成正文；终答由独立 chat/chat_stream 生成（可流式）。
    {"type": "function", "function": {
        "name": "answer",
        "description": "无需专用工具，直接用你的知识回答（闲聊/解释/非合规非采集类问题）。",
        "parameters": {"type": "object", "properties": {}},
    }},
]

# 空检索的安全话术（防幻觉，事前定：rules_search 无命中即用此，不让模型凭记忆编造）。
EMPTY_RULES_FALLBACK = "未找到相关平台规则，请以平台最新官方公告为准。（知识库无匹配，不臆测。）"

# 工具 → 前端展示标签（SSE tool_running 事件用）
TOOL_LABELS: dict[str, str] = {
    "box_count": "查询采集箱", "box_list": "列出采集箱", "box_delete": "删除商品",
    "box_translate": "翻译商品", "box_list_tiktok": "上架 TikTok",
    "analyze": "分析中", "rules_search": "检索平台规则", "collect_products": "下发采集任务",
}

class Prep(TypedDict):
    """prepare() 产物：终答生成所需。"""
    action: dict
    tools_used: list[str]
    needs_gen: bool                      # True→service 用 gen_messages 流式/非流式生成终答
    gen_messages: list[dict] | None      # 终答生成的消息（含工具结果），needs_gen 时有
    static_reply: str | None             # 事前定的静态回复（当前仅空检索），needs_gen=False 时用


async def prepare(
    session: AsyncSession, messages: list[dict], *, model: str,
    force_tool: str | None = None, user_message: str = "",
) -> Prep:
    """路由 + 工具执行，返回终答生成所需——**不做终答正文生成**（留给 service 流式/非流式）。

    方案 B：强制 tool_choice（工具集含 answer 标记工具）→ 路由永不自由吐正文；终答总是
    一次独立的可流式调用。**单路由轮**：一次路由决定 answer 或调工具；调工具则执行后直接
    综述，不再 route（省 1 次 LLM；放弃"工具后再调另一工具"的串行多轮——本 chat 工具相互
    独立、罕见串联；一次路由内的并行多 tool_call 仍支持）。
    """
    impls = _make_tool_impls(
        BoxService(session), RulesKbService(session), SourcingService(session), model
    )
    msgs = list(messages)
    tool_choice: Any = (
        {"type": "function", "function": {"name": force_tool}} if force_tool else "required"
    )
    route_msg = await chat_with_tools(msgs, tools=TOOLS, model=model, tool_choice=tool_choice)
    real = [c for c in (route_msg.get("tool_calls") or []) if c["function"]["name"] != "answer"]
    if not real:
        # 路由决定直接答（answer / 未调真工具，含 provider 不认 required 时模型直接回内容）
        # → 终答就绪。answer 路由 msg 不入 gen_messages。
        return {"action": {"type": "answer"}, "tools_used": [], "needs_gen": True,
                "gen_messages": msgs, "static_reply": None}

    # 执行真工具（一轮内全部）→ 综述。
    msgs.append(route_msg)
    action: dict | None = None
    tools_used: list[str] = []
    for tc in real:
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        # 强制调用时模型常漏传 query → 回退用用户原始消息，避免空检索假"未找到"。
        if name == "rules_search" and not (args.get("query") or "").strip():
            args["query"] = user_message
        impl = impls.get(name)
        text, payload = await impl(args) if impl else (f"未知工具：{name}", {"type": "answer"})
        tools_used.append(name)
        if action is None:  # 记首个动作的结构化 payload
            action = payload
        msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""), "name": name, "content": text})

    # 空检索：事前定，不流，静态覆盖（防幻觉，不让模型凭记忆编造平台规则）。
    if action and action.get("type") == "rules_search" and action.get("empty"):
        return {"action": action, "tools_used": tools_used, "needs_gen": False,
                "gen_messages": None, "static_reply": EMPTY_RULES_FALLBACK}
    return {"action": action or {"type": "answer"}, "tools_used": tools_used,
            "needs_gen": True, "gen_messages": msgs, "static_reply": None}

    # 达最大轮：从现有 msgs（含工具结果）综述。
    return {"action": action or {"type": "answer"}, "tools_used": tools_used,
            "needs_gen": True, "gen_messages": msgs, "static_reply": None}


def _make_tool_impls(box: BoxService, rules: RulesKbService, sourcing: SourcingService,
                     model: str) -> dict[str, Callable]:
    """工具名 → async 实现。每个实现返回 (text, payload)：
    text 回灌给模型续写最终答；payload 是结构化 ChatAction，透给前端渲染。"""

    async def t_box_count(a: dict):
        n = await box.count()
        return f"采集箱当前 {n} 个商品。", {"type": "answer"}

    async def t_box_list(a: dict):
        items = await box.list(keyword=a.get("keyword"), limit=int(a.get("limit", 30)))
        rows = [{"id": str(i.get("id", "")), "title": i.get("title", ""),
                 "price": str(i.get("price", "")), "status": i.get("status", "")} for i in items]
        return (f"采集箱列表（{len(rows)} 条）。", {"type": "box_list", "rows": rows})

    async def t_box_delete(a: dict):
        n = await box.delete(scope=a.get("scope", "chinese"))
        return f"已删除 {n} 个商品（scope={a.get('scope', 'chinese')}）。", {"type": "answer"}

    async def t_box_translate(a: dict):
        r = await box.translate(scope=a.get("scope", "all"), lang=a.get("lang", "en"),
                                images=bool(a.get("images", False)))
        return f"翻译完成：{json.dumps(r, ensure_ascii=False)}", {"type": "answer"}

    async def t_box_list_tiktok(a: dict):
        r = await box.list_tiktok(scope=a.get("scope", "all"), auto=bool(a.get("auto", False)))
        return f"上架结果：{json.dumps(r, ensure_ascii=False)}", {"type": "answer"}

    async def t_analyze(a: dict):
        atype = a.get("type") if a.get("type") in ANALYSIS_PROMPTS else DEFAULT_ANALYSIS
        prompt = ANALYSIS_PROMPTS[atype].format(keyword=a.get("keyword", ""))
        text = await chat(
            messages=[{"role": "system", "content": "你是跨境电商分析师，结论先行，用表格。"},
                      {"role": "user", "content": prompt}],
            model=model,
        )
        return text, {"type": "analyze"}

    async def t_rules_search(a: dict):
        hits = await rules.search(a.get("query", ""), platform=a.get("platform"),
                                  site=a.get("site"), limit=5)
        cites = [{
            "summary": h.get("summary", ""), "source_url": h.get("source_url", ""),
            "version": h.get("version", ""), "confidence": h.get("confidence", ""),
            "last_verified_at": h.get("last_verified_at", ""),
        } for h in hits]
        return _format_rules(hits), {"type": "rules_search", "empty": not hits, "cites": cites}

    async def t_collect_products(a: dict):
        # 触发 sourcing 域采集（落 pending 行，采集插件 poll/done 接力；见 SourcingService）。
        # tenant_id 从请求态取，显式传给 service（RLS 由会话 GUC 兜底）。
        kws = a.get("keywords") or []
        per_kw = int(a.get("perKw", a.get("per_kw", 10)))
        market = a.get("market")
        res = await sourcing.start_collect(
            tenant_id=current_tenant_id.get(), keywords=kws, per_kw=per_kw, market=market,
        )
        job_id = res["job_id"]
        short = job_id[:8]
        if res["mode"] == "unavailable":
            # 任务连库都没写进（如无 DB 环境）：如实告知，禁止声称已在抓取。
            text = (f"已生成采集任务号 {short}，但任务尚未成功登记（编排与存储均不可用）。"
                    f"请如实告知用户采集暂未排队成功，不要声称正在抓取或已完成。")
        else:
            # 已排队（temporal 或降级写库）。真正抓取由用户端采集插件认领后执行 —— 尚未发生。
            text = (f"已下发采集任务（任务号 {short}）：关键词 {kws or '待提炼'}、每词 {per_kw} 个"
                    f"{('、市场 ' + market) if market else ''}，等待采集插件认领执行。"
                    f"请如实告知用户任务已排队、采集进行中需等插件回结果，不要声称已完成。")
        return text, {"type": "collect_products", "job_id": job_id}

    return {
        "box_count": t_box_count, "box_list": t_box_list, "box_delete": t_box_delete,
        "box_translate": t_box_translate, "box_list_tiktok": t_box_list_tiktok,
        "analyze": t_analyze, "rules_search": t_rules_search,
        "collect_products": t_collect_products,
    }


def _format_rules(hits: list[dict[str, Any]]) -> str:
    """把 rules_kb 命中结果格式化给模型，并施加计划 §6 的硬约束。"""
    if not hits:
        return "未找到相关规则，请以平台最新公告为准。（知识库未命中，不做臆测。）"
    lines = []
    for h in hits:
        tag = ""
        if h.get("confidence") == "low" or h.get("verification_status") != "verified":
            tag = "  ⚠（待人工核验/可能非最新，请以官方为准）"
        lines.append(
            f"- {h.get('summary', '')}\n"
            f"  来源：{h.get('source_url', '')}  版本：{h.get('version', '')}"
            f"  最后核验：{h.get('last_verified_at', '')}{tag}"
        )
    return "依据知识库（每条附溯源，未命中不编造）：\n" + "\n".join(lines)
