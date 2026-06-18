// 真后端接缝：打到 FastAPI 的 /auth、/chat 端点，消费 SSE 事件流。
// 与 mockApi 实现同一个 ChatApi 契约；切换在 api.ts。
import type {
  ChatApi,
  CollectJob,
  Conversation,
  Message,
  Session,
  SseEvent,
} from "./contract";

const BASE: string =
  (import.meta as any).env?.VITE_API_BASE ?? "http://localhost:8000";

let activeToken: string | null = null;

function authHeaders(json = true): Record<string, string> {
  const h: Record<string, string> = {};
  if (json) h["Content-Type"] = "application/json";
  if (activeToken) h["Authorization"] = `Bearer ${activeToken}`;
  return h;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, init);
  if (!resp.ok) {
    const detail = await resp.text().catch(() => "");
    throw new Error(`${resp.status} ${path} ${detail}`);
  }
  return (await resp.json()) as T;
}

// 解析一个 SSE 帧（event: X\ndata: {...}）为 SseEvent。
function parseFrame(frame: string): SseEvent | null {
  let event = "";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!event) return null;
  const data = dataLines.length ? JSON.parse(dataLines.join("\n")) : {};
  return { event, data } as SseEvent;
}

export const realApi: ChatApi = {
  async login(account, password) {
    return await req<Session>("/auth/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account, password }),
    });
  },

  setToken(token) {
    activeToken = token;
  },

  async listConversations() {
    return await req<Conversation[]>("/chat/conversations", {
      headers: authHeaders(false),
    });
  },

  async createConversation() {
    return await req<Conversation>("/chat/conversations", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ title: "新对话" }),
    });
  },

  async listMessages(conversationId) {
    const r = await req<{ messages: Message[] }>(
      `/chat/conversations/${conversationId}/messages`,
      { headers: authHeaders(false) },
    );
    return r.messages;
  },

  async *sendMessage(conversationId, text) {
    const resp = await fetch(
      `${BASE}/chat/conversations/${conversationId}/messages`,
      {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ message: text }),
      },
    );
    if (!resp.ok || !resp.body) {
      throw new Error(`发送失败 ${resp.status}`);
    }
    // 流式读取，按空行分帧解析 SSE。
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const ev = parseFrame(frame);
        if (ev) yield ev;
      }
    }
    // 冲洗残帧
    const tail = parseFrame(buf);
    if (tail) yield tail;
  },

  async getJob(jobId) {
    // sourcing 域（Temporal CollectWorkflow + /sourcing/jobs）尚未落地（计划 §3）。
    // 端点就绪后改为真实 GET；当前直连会 404。
    return await req<CollectJob>(`/sourcing/jobs/${jobId}`, {
      headers: authHeaders(false),
    });
  },
};
