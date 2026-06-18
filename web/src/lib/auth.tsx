import { createContext, useContext, useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import type { Session } from "./contract";

const KEY = "zhuiwen_session";

interface AuthCtx {
  session: Session | null;
  login: (account: string, password: string) => Promise<void>;
  logout: () => void;
}

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const qc = useQueryClient();
  const [session, setSession] = useState<Session | null>(() => {
    const raw = localStorage.getItem(KEY);
    const s = raw ? (JSON.parse(raw) as Session) : null;
    if (s) api.setToken(s.token); // 恢复时立刻装载 token
    return s;
  });

  // session 变更 → 同步 token 到 api + 持久化。
  useEffect(() => {
    api.setToken(session?.token ?? null);
    if (session) localStorage.setItem(KEY, JSON.stringify(session));
    else localStorage.removeItem(KEY);
  }, [session]);

  async function login(account: string, password: string) {
    const s = await api.login(account, password);
    qc.clear(); // 换账号必清缓存，杜绝跨租户数据残留
    setSession(s);
  }

  function logout() {
    qc.clear();
    setSession(null);
  }

  return <Ctx.Provider value={{ session, login, logout }}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useAuth 须在 AuthProvider 内");
  return c;
}
