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
from typing import Any, Callable, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
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
        "description": "从 0 全自动采集选品（下发采集任务给浏览器插件）。",
        "parameters": {"type": "object", "properties": {
            "keywords": {"type": "array", "items": {"type": "string"}},
            "perKw": {"type": "integer"},
            "topN": {"type": "integer"},
            "lang": {"type": "string"},
            "translate": {"type": "boolean"},
            "listTiktok": {"type": "boolean"},
            "tkAuto": {"type": "boolean"},
        }},
    }},
]

# 工具 → 前端展示标签（SSE tool_running 事件用）
TOOL_LABELS: dict[str, str] = {
    "box_count": "查询采集箱", "box_list": "列出采集箱", "box_delete": "删除商品",
    "box_translate": "翻译商品", "box_list_tiktok": "上架 TikTok",
    "analyze": "分析中", "rules_search": "检索平台规则", "collect_products": "下发采集任务",
}

MAX_TOOL_ROUNDS = 4  # 防工具循环无限递归


class ChatState(TypedDict):
    messages: list[dict]                 # OpenAI 格式消息
    action: Optional[dict]               # 结构化 ChatAction（首个工具产出），无则 None
    tools_used: list[str]                # 本轮调用过的工具名（SSE tool_running 用）
    reply: str
    rounds: int
    force_tool: Optional[str]            # 首轮强制调用的工具名（合规召回闸），无则模型自选
    user_message: str                    # 用户原始消息，供工具参数缺失时回退（如空 query）


def build_chat_agent(session: AsyncSession, model: str = "gpt-4o-mini"):
    box = BoxService(session)
    rules = RulesKbService(session)
    sourcing = SourcingService(session)
    tool_impls = _make_tool_impls(box, rules, sourcing, model)

    async def route(state: ChatState) -> ChatState:
        # 合规召回闸：首轮强制调用指定工具（如 rules_search），不让模型漏路由。
        tool_choice = None
        if state.get("force_tool") and state.get("rounds", 0) == 0:
            tool_choice = {"type": "function", "function": {"name": state["force_tool"]}}
        msg = await chat_with_tools(
            state["messages"], tools=TOOLS, model=model, tool_choice=tool_choice
        )
        messages = state["messages"] + [msg]
        if not (msg.get("tool_calls") or []):
            return {
                **state, "messages": messages,
                "reply": msg.get("content") or "",
                "action": state.get("action") or {"type": "answer"},
            }
        return {**state, "messages": messages}

    async def tool_exec(state: ChatState) -> ChatState:
        tool_calls = state["messages"][-1].get("tool_calls") or []
        messages = list(state["messages"])
        action = state.get("action")
        tools_used = list(state.get("tools_used", []))
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            # 强制调用（tool_choice）时模型常漏传 query → 回退用用户原始消息，避免空检索假"未找到"。
            if name == "rules_search" and not (args.get("query") or "").strip():
                args["query"] = state.get("user_message", "")
            impl = tool_impls.get(name)
            if impl:
                text, payload = await impl(args)
            else:
                text, payload = f"未知工具：{name}", {"type": "answer"}
            tools_used.append(name)
            if action is None:  # 记首个动作的结构化 payload
                action = payload
            messages.append({
                "role": "tool", "tool_call_id": tc.get("id", ""), "name": name,
                "content": text,
            })
        return {**state, "messages": messages, "action": action,
                "tools_used": tools_used, "rounds": state.get("rounds", 0) + 1}

    def after_route(state: ChatState) -> str:
        return "tool_exec" if state["messages"][-1].get("tool_calls") else END

    def after_tools(state: ChatState) -> str:
        return END if state.get("rounds", 0) >= MAX_TOOL_ROUNDS else "route"

    graph = StateGraph(ChatState)
    graph.add_node("route", route)
    graph.add_node("tool_exec", tool_exec)
    graph.add_edge(START, "route")
    graph.add_conditional_edges("route", after_route, {"tool_exec": "tool_exec", END: END})
    graph.add_conditional_edges("tool_exec", after_tools, {"route": "route", END: END})
    return graph.compile()


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
        # 触发 sourcing 域采集（Temporal CollectWorkflow；不可达则降级写库，见 SourcingService）。
        # tenant_id 必须显式贯穿到 Temporal（跨进程，不能靠 ContextVar）——从请求态取。
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
