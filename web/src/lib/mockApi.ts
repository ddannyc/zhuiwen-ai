// Mock 后端：内存态模拟 chat 域 + sourcing 任务 + 多租户隔离。
// 只为前端独立开发存在。后端就绪后整文件弃用，换 realApi。
import type {
  ChatApi,
  ChatAction,
  CollectJob,
  Conversation,
  Message,
  Session,
  SseEvent,
} from "./contract";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
let seq = 0;
const uid = (p: string) => `${p}_${Date.now()}_${seq++}`;

// 租户展示名（侧栏用）。
export const TENANT_NAMES: Record<string, string> = {
  acme: "Acme 跨境",
  globex: "Globex 优选",
};

// mock 账号目录：账号 → {租户, 用户}。真后端在 DB 里查，租户绑账号，前端不可见此映射。
const ACCOUNTS: Record<string, { tenant_id: string; user_id: string }> = {
  "alice@acme": { tenant_id: "acme", user_id: "alice" },
  "bob@acme": { tenant_id: "acme", user_id: "bob" },
  "carol@globex": { tenant_id: "globex", user_id: "carol" },
};

// 按租户分区，模拟 DB RLS：A 租户看不见 B 的会话。
interface Store {
  conversations: Conversation[];
  messages: Record<string, Message[]>;
}
const stores: Record<string, Store> = {};
const jobs: Record<string, CollectJob> = {}; // job id 全局唯一，无需分区

// mock token = base64({tenant_id,user_id})。真 JWT 对前端不透明，仅后端验签。
function mintToken(tenant_id: string, user_id: string): string {
  return btoa(JSON.stringify({ tenant_id, user_id }));
}
function tenantOf(token: string): string {
  return JSON.parse(atob(token)).tenant_id as string;
}
function userOf(token: string): string {
  return JSON.parse(atob(token)).user_id as string;
}
function curUser(): string {
  if (!activeToken) throw new Error("未登录");
  return userOf(activeToken);
}

let activeToken: string | null = null;
function cur(): Store {
  if (!activeToken) throw new Error("未登录");
  const tid = tenantOf(activeToken);
  return (stores[tid] ??= { conversations: [], messages: {} });
}

// 粗略意图路由，模拟 agent。真后端由 LangGraph tool-calling 决定。
function route(text: string): ChatAction {
  const t = text.trim();
  if (/采集箱|列出|box/i.test(t))
    return {
      type: "box_list",
      rows: [
        { id: "p1", title: "便携榨汁杯 USB 充电", price: "$12.90", status: "待译" },
        { id: "p2", title: "宠物自动喂食器", price: "$28.00", status: "已译" },
        { id: "p3", title: "LED 化妆镜", price: "$9.50", status: "待译" },
      ],
    };
  if (/采集|选品|蓝海|自动/i.test(t)) {
    const id = uid("job");
    jobs[id] = { id, stage: "pending", collected: 0, target: 20 };
    return { type: "collect_products", job_id: id };
  }
  if (/规则|类目|准入|禁限售|合规|处罚|费用|能卖|磁铁|含电池/i.test(t)) {
    // 模拟“检索到”与“检索为空”两态：含“禁限售”走空态演示低幻觉。
    if (/禁限售/.test(t)) return { type: "rules_search", empty: true, cites: [] };
    return {
      type: "rules_search",
      empty: false,
      cites: [
        {
          summary:
            "亚马逊美国站玩具类目允许销售含磁铁商品，但磁通量需符合 ASTM F963 与 CPSC 强磁标准，小零件需附窒息警告。",
          source_url: "https://sellercentral.amazon.com/help/hub/reference/XXXXX",
          version: "2025-09",
          confidence: "high",
          last_verified_at: "2026-05-01",
        },
        {
          summary: "强磁产品（磁通量指数 > 50 kG²mm²）若可被吞咽则禁售。",
          source_url: "https://www.cpsc.gov/Regulations/magnets",
          version: "2024-01",
          confidence: "low",
          last_verified_at: "2024-02-10", // 故意过期 → 触发时效提示
        },
      ],
    };
  }
  return { type: "answer" };
}

const replyText: Record<string, string> = {
  answer:
    "### 结论先行\n美国市场当前**家居小家电**与**宠物智能用品**蓝海度高。建议聚焦客单价 $15–35 区间，规避红海 3C 配件。\n\n| 类目 | 蓝海度 | 备注 |\n|---|---|---|\n| 宠物喂食器 | 高 | 复购强 |\n| 便携厨房电器 | 中 | 物流需测算 |",
  analyze: "（营销/选品建议，自由生成）此为 analyze 渲染示例。",
  box_list: "已读取采集箱前 3 个商品：",
  rules_search: "依据平台规则检索结果：",
  collect_products: "已启动自动采集任务，进度见下方卡片：",
};

async function* streamReply(
  conversationId: string,
  action: ChatAction,
): AsyncGenerator<SseEvent, void, unknown> {
  yield { event: "action", data: { type: action.type } };
  if (action.type === "box_list")
    yield { event: "tool_running", data: { tool: "box_list", label: "读采集箱…" } };
  if (action.type === "rules_search")
    yield { event: "tool_running", data: { tool: "rules_search", label: "检索规则库…" } };

  const full = replyText[action.type] ?? "";
  let acc = "";
  for (const ch of chunk(full, 6)) {
    await sleep(40);
    acc += ch;
    yield { event: "token", data: { delta: ch } };
  }
  yield { event: "payload", data: action };

  const id = uid("m");
  const msg: Message = {
    id,
    conversation_id: conversationId,
    role: "assistant",
    content: acc,
    action,
    created_at: new Date().toISOString(),
  };
  cur().messages[conversationId].push(msg);
  yield { event: "done", data: { message_id: id } };
}

function chunk(s: string, n: number): string[] {
  const out: string[] = [];
  for (let i = 0; i < s.length; i += n) out.push(s.slice(i, i + n));
  return out;
}

// 采集任务推进：每次 getJob 前进一阶，模拟 Temporal activity 流转。
const STAGES: CollectJob["stage"][] = [
  "pending",
  "collecting",
  "scoring",
  "translating",
  "publishing",
  "done",
];

export const mockApi: ChatApi = {
  async login(account, _password) {
    await sleep(120);
    const rec = ACCOUNTS[account.trim()];
    if (!rec) throw new Error("账号或密码错误");
    // 租户由账号决定，写进 token。前端拿不到“选租户”的权力。
    const s: Session = {
      token: mintToken(rec.tenant_id, rec.user_id),
      tenant_id: rec.tenant_id,
      user_id: rec.user_id,
    };
    return s;
  },
  setToken(token) {
    activeToken = token;
  },
  async listConversations() {
    await sleep(80);
    // 归属过滤：按 user_id，非 tenant_id（租户隔离已由 store 分区/RLS 兜底）。
    const me = curUser();
    return cur()
      .conversations.filter((c) => c.user_id === me)
      .reverse();
  },
  async createConversation() {
    await sleep(80);
    const c: Conversation = {
      id: uid("c"),
      user_id: curUser(),
      title: "新对话",
      created_at: new Date().toISOString(),
    };
    cur().conversations.push(c);
    cur().messages[c.id] = [];
    return c;
  },
  async listMessages(conversationId) {
    await sleep(80);
    return [...(cur().messages[conversationId] ?? [])];
  },
  async *sendMessage(conversationId, text) {
    const um: Message = {
      id: uid("m"),
      conversation_id: conversationId,
      role: "user",
      content: text,
      action: null,
      created_at: new Date().toISOString(),
    };
    (cur().messages[conversationId] ??= []).push(um);
    // 首条消息用文本做标题
    const conv = cur().conversations.find((c) => c.id === conversationId);
    if (conv && conv.title === "新对话") conv.title = text.slice(0, 20);

    yield* streamReply(conversationId, route(text));
  },
  async getJob(jobId) {
    await sleep(60);
    const j = jobs[jobId];
    const i = STAGES.indexOf(j.stage);
    if (i < STAGES.length - 1) {
      j.stage = STAGES[i + 1];
      j.collected = Math.min(j.target, (i + 1) * 4);
    }
    return { ...j };
  },
};
