// 前后端共享契约。后端 app/domains/chat 落地后，realApi 须吐与此一致的结构。
// 唯一改动点：把 mockApi 换成打到 /chat、/sourcing 真端点的 realApi。

export interface Conversation {
  id: string;
  user_id: string; // 归属人；列表按此过滤（非 tenant_id）
  title: string;
  created_at: string;
}

export type Role = "user" | "assistant";

// 一条已落库消息。assistant 消息带 action 判别其渲染形态。
export interface Message {
  id: string;
  conversation_id: string;
  role: Role;
  content: string; // 文本/markdown 主体
  action: ChatAction | null;
  created_at: string;
}

// agent 路由结果。后端 service 须按 type 吐对应 payload。前端不猜结构。
export type ChatAction =
  | { type: "answer" } // 纯问答，正文在 content
  | { type: "analyze" } // 营销/选品自由生成，正文在 content
  | { type: "box_list"; rows: BoxRow[] }
  | { type: "rules_search"; empty: boolean; cites: RuleCite[] }
  | { type: "collect_products"; job_id: string };

export interface BoxRow {
  id: string;
  title: string;
  price: string;
  status: string;
}

// 规则溯源条目。合规硬约束：每条挂 source_url + version。
export interface RuleCite {
  summary: string;
  source_url: string;
  version: string;
  confidence: "high" | "medium" | "low";
  last_verified_at: string; // ISO；前端据此判时效
}

// 采集任务（Temporal workflow 投影）。聊天只拿 job_id，进度走轮询。
export type JobStage =
  | "pending"
  | "collecting"
  | "scoring"
  | "translating"
  | "publishing"
  | "done"
  | "failed";

export interface CollectJob {
  id: string;
  stage: JobStage;
  collected: number;
  target: number;
}

// ── SSE 事件协议 ──
// POST /chat/conversations/{id}/messages 返回 text/event-stream。
// 顺序：action(先挂骨架) → [tool_running] → token* → payload → done。
export type SseEvent =
  // 骨架：仅判别 type，让前端挂占位；富结构随后由 payload 给。
  | { event: "action"; data: { type: ChatAction["type"] } }
  | { event: "tool_running"; data: { tool: string; label: string } }
  | { event: "token"; data: { delta: string } }
  // 流式守卫命中（泄露/假引用）→ 停流，用 text 替换已显示的流式正文（仅出事才发）。
  | { event: "replace"; data: { text: string } }
  | { event: "payload"; data: ChatAction } // 带 rows/cites/job_id 的完整结构
  | { event: "done"; data: { message_id: string } }
  | { event: "error"; data: { msg: string } };

// 登录态。token 即 JWT，由后端 issue_token 签发；前端只持有，不解析租户。
// 所有数据端点不带 tenant_id —— 服务端 RLS 据 token 隔离（对齐后端纪律）。
export interface Session {
  token: string;
  tenant_id: string;
  user_id: string;
}

// 前后端共用的 API 接缝。mockApi 与 realApi 都实现它。
export interface ChatApi {
  // 账号登录换 token（真后端：POST /auth/token）。租户由账号在服务端决定，
  // 写进 JWT，前端不传也不选 tenant_id。
  login(account: string, password: string): Promise<Session>;
  // 设置/清除当前 token，后续请求带 Authorization: Bearer。
  setToken(token: string | null): void;
  listConversations(): Promise<Conversation[]>;
  createConversation(): Promise<Conversation>;
  listMessages(conversationId: string): Promise<Message[]>;
  // 发消息：返回 SSE 事件流（async generator）。
  sendMessage(
    conversationId: string,
    text: string,
  ): AsyncGenerator<SseEvent, void, unknown>;
  getJob(jobId: string): Promise<CollectJob>;
}
