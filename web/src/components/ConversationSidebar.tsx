import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { TENANT_NAMES } from "../lib/mockApi";

export function ConversationSidebar({
  activeId,
  onSelect,
}: {
  activeId: string | null;
  onSelect: (id: string) => void;
}) {
  const qc = useQueryClient();
  const { session, logout } = useAuth();
  const tenantName =
    (session && TENANT_NAMES[session.tenant_id]) ?? session?.tenant_id;
  const { data: conversations = [] } = useQuery({
    queryKey: ["conversations"],
    queryFn: () => api.listConversations(),
  });

  const create = useMutation({
    mutationFn: () => api.createConversation(),
    onSuccess: (c) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      onSelect(c.id);
    },
  });

  return (
    <aside className="flex h-full w-64 flex-col border-r border-slate-200 bg-white">
      <div className="flex items-center gap-2 px-4 py-3">
        <span className="text-lg">🐒</span>
        <span className="font-semibold text-slate-800">飞猴</span>
      </div>
      <button
        className="mx-3 mb-2 rounded-lg border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50"
        onClick={() => create.mutate()}
      >
        ＋ 新对话
      </button>
      <nav className="flex-1 overflow-y-auto px-2">
        {conversations.map((c) => (
          <button
            key={c.id}
            onClick={() => onSelect(c.id)}
            className={
              "mb-0.5 w-full truncate rounded-lg px-3 py-2 text-left text-sm " +
              (c.id === activeId
                ? "bg-slate-100 text-slate-900"
                : "text-slate-600 hover:bg-slate-50")
            }
          >
            {c.title}
          </button>
        ))}
      </nav>
      <div className="border-t border-slate-200 px-4 py-2.5">
        <div className="mb-1.5 flex items-center justify-between">
          <span className="truncate text-sm text-slate-700">{tenantName}</span>
          <button
            className="shrink-0 text-xs text-slate-400 hover:text-slate-600"
            onClick={logout}
          >
            切换/登出
          </button>
        </div>
        <span className="text-xs text-slate-400">
          用户 {session?.user_id} · mock 后端
        </span>
      </div>
    </aside>
  );
}
