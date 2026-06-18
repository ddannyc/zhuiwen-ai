import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "./lib/api";
import { AuthProvider, useAuth } from "./lib/auth";
import { LoginGate } from "./components/LoginGate";
import { ConversationSidebar } from "./components/ConversationSidebar";
import { ChatPane } from "./components/ChatPane";

export default function App() {
  return (
    <AuthProvider>
      <Shell />
    </AuthProvider>
  );
}

function Shell() {
  const { session } = useAuth();
  const qc = useQueryClient();
  const [activeId, setActiveId] = useState<string | null>(null);
  const creating = useRef(false); // 守卫：StrictMode 双调 effect 也只建一次

  // 登录后自动建首个会话；登出时清空选中。
  useEffect(() => {
    if (!session) {
      setActiveId(null);
      creating.current = false;
      return;
    }
    if (activeId || creating.current) return;
    creating.current = true;
    api.createConversation().then((c) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      setActiveId(c.id);
    });
  }, [session, activeId, qc]);

  if (!session) return <LoginGate />;

  return (
    <div className="flex h-full">
      <ConversationSidebar activeId={activeId} onSelect={setActiveId} />
      {activeId ? (
        <ChatPane key={activeId} conversationId={activeId} />
      ) : (
        <div className="flex flex-1 items-center justify-center text-slate-400">
          加载中…
        </div>
      )}
    </div>
  );
}
