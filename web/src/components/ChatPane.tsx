import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { ChatAction } from "../lib/contract";
import { MessageRenderer } from "./MessageRenderer";
import { Composer } from "./Composer";

// 流式中的 assistant 消息（未落库前的临时态）。
interface Streaming {
  content: string;
  action: ChatAction | null;
  tool: string | null;
}

export function ChatPane({
  conversationId,
  onConversationCreated,
}: {
  conversationId: string | null; // null = 新对话草稿，未落库
  onConversationCreated: (id: string) => void;
}) {
  const qc = useQueryClient();
  const [streaming, setStreaming] = useState<Streaming | null>(null);
  const [pendingUser, setPendingUser] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const { data: messages = [] } = useQuery({
    queryKey: ["messages", conversationId],
    queryFn: () => api.listMessages(conversationId as string),
    enabled: !!conversationId, // 草稿态不拉历史
  });

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, streaming]);

  async function send(text: string) {
    // 草稿态：首条消息发送时才真正建会话（空白会话不落库）。
    let id = conversationId;
    if (!id) {
      const c = await api.createConversation();
      id = c.id;
    }

    setPendingUser(text);
    setStreaming({ content: "", action: null, tool: null });
    let content = "";
    for await (const ev of api.sendMessage(id, text)) {
      if (ev.event === "tool_running")
        setStreaming((s) => s && { ...s, tool: ev.data.label });
      else if (ev.event === "token") {
        content += ev.data.delta;
        setStreaming((s) => s && { ...s, content, tool: null });
      } else if (ev.event === "payload") {
        // 富结构到位，才挂动作组件（骨架 action 事件无 payload 字段，不能渲）。
        setStreaming((s) => s && { ...s, action: ev.data });
      } else if (ev.event === "done") break;
      else if (ev.event === "error") {
        content += `\n\n⚠️ ${ev.data.msg}`;
        setStreaming((s) => s && { ...s, content });
      }
    }
    // 落库真源刷新，清临时态
    await qc.invalidateQueries({ queryKey: ["messages", id] });
    await qc.invalidateQueries({ queryKey: ["conversations"] });
    setStreaming(null);
    setPendingUser(null);
    // 草稿首发完成 → 切到真实会话 id（父级 remount，载入持久化历史 + LLM 标题）
    if (!conversationId) onConversationCreated(id);
  }

  const isEmpty = messages.length === 0 && !streaming && !pendingUser;

  return (
    <div className="flex h-full flex-1 flex-col bg-slate-50">
      {isEmpty ? (
        <EmptyState onPick={send} />
      ) : (
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-6">
        <div className="mx-auto flex max-w-2xl flex-col gap-4">
          {messages.map((m) => (
            <Bubble key={m.id} role={m.role}>
              <MessageRenderer content={m.content} action={m.action} />
            </Bubble>
          ))}
          {pendingUser && <Bubble role="user">{pendingUser}</Bubble>}
          {streaming && (
            <Bubble role="assistant">
              {streaming.tool && (
                <p className="mb-1 text-xs text-slate-400">{streaming.tool}</p>
              )}
              <MessageRenderer
                content={streaming.content}
                action={streaming.action}
              />
              {!streaming.content && !streaming.tool && (
                <span className="text-slate-400">…</span>
              )}
            </Bubble>
          )}
        </div>
        </div>
      )}
      <Composer disabled={!!streaming} onSend={send} />
    </div>
  );
}

const SUGGESTIONS = [
  "美国市场蓝海选品建议",
  "列出采集箱前 10 个",
  "亚马逊美国站玩具含磁铁能卖吗",
  "Ozon 平台佣金怎么算",
];

function EmptyState({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="flex flex-1 items-center justify-center px-4">
      <div className="w-full max-w-2xl text-center">
        <div className="mb-3 text-4xl">🐒</div>
        <h2 className="text-lg font-semibold text-slate-800">
          飞猴 · 跨境电商智能体
        </h2>
        <p className="mt-1 text-sm text-slate-400">
          选品、竞品、Listing、定价、平台规则 — 问我或下指令
        </p>
        <div className="mt-6 flex flex-wrap justify-center gap-2">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              onClick={() => onPick(s)}
              className="rounded-full border border-slate-200 bg-white px-3.5 py-1.5 text-sm text-slate-600 shadow-sm transition-colors hover:border-slate-300 hover:bg-slate-50 hover:text-slate-800"
            >
              {s}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function Bubble({
  role,
  children,
}: {
  role: "user" | "assistant";
  children: React.ReactNode;
}) {
  const me = role === "user";
  return (
    <div className={me ? "flex justify-end" : "flex justify-start"}>
      <div
        className={
          "max-w-[85%] rounded-2xl px-4 py-2.5 text-sm " +
          (me
            ? "bg-slate-800 text-white"
            : "border border-slate-200 bg-white text-slate-800")
        }
      >
        {children}
      </div>
    </div>
  );
}
