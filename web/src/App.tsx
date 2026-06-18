import { useEffect, useState } from "react";
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
  // activeId 为 null = 新对话草稿（未落库）。首条消息发送时才在后端建会话，
  // 避免点「新对话」就落一条空白会话污染列表。
  const [activeId, setActiveId] = useState<string | null>(null);

  useEffect(() => {
    if (!session) setActiveId(null); // 登出清空
  }, [session]);

  if (!session) return <LoginGate />;

  return (
    <div className="flex h-full">
      <ConversationSidebar
        activeId={activeId}
        onSelect={setActiveId}
        onNew={() => setActiveId(null)}
      />
      <ChatPane
        key={activeId ?? "draft"}
        conversationId={activeId}
        onConversationCreated={setActiveId}
      />
    </div>
  );
}
