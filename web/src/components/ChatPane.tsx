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

export function ChatPane({ conversationId }: { conversationId: string }) {
  const qc = useQueryClient();
  const [streaming, setStreaming] = useState<Streaming | null>(null);
  const [pendingUser, setPendingUser] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const { data: messages = [] } = useQuery({
    queryKey: ["messages", conversationId],
    queryFn: () => api.listMessages(conversationId),
  });

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, streaming]);

  async function send(text: string) {
    setPendingUser(text);
    setStreaming({ content: "", action: null, tool: null });
    let action: ChatAction | null = null;
    let content = "";
    for await (const ev of api.sendMessage(conversationId, text)) {
      if (ev.event === "tool_running")
        setStreaming((s) => s && { ...s, tool: ev.data.label });
      else if (ev.event === "token") {
        content += ev.data.delta;
        setStreaming((s) => s && { ...s, content, tool: null });
      } else if (ev.event === "payload") {
        // 富结构到位，才挂动作组件（骨架 action 事件无 payload 字段，不能渲）。
        action = ev.data;
        setStreaming((s) => s && { ...s, action });
      } else if (ev.event === "done") break;
      else if (ev.event === "error") {
        content += `\n\n⚠️ ${ev.data.msg}`;
        setStreaming((s) => s && { ...s, content });
      }
    }
    // 落库真源刷新，清临时态
    await qc.invalidateQueries({ queryKey: ["messages", conversationId] });
    await qc.invalidateQueries({ queryKey: ["conversations"] });
    setStreaming(null);
    setPendingUser(null);
  }

  return (
    <div className="flex h-full flex-1 flex-col bg-slate-50">
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-6">
        <div className="mx-auto flex max-w-2xl flex-col gap-4">
          {messages.length === 0 && !streaming && (
            <p className="mt-20 text-center text-sm text-slate-400">
              开始对话 — 试试「列出采集箱前10个」或「亚马逊美国站玩具含磁铁能卖吗」
            </p>
          )}
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
      <Composer disabled={!!streaming} onSend={send} />
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
