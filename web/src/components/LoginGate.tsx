import { useState } from "react";
import { useAuth } from "../lib/auth";

// 未登录守卫页。账号+密码 → 后端验证并签 token，租户由账号决定（前端不选）。
export function LoginGate() {
  const { login } = useAuth();
  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      await login(account.trim(), password);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full items-center justify-center bg-slate-50">
      <div className="w-80 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="mb-1 flex items-center gap-2">
          <span className="text-xl">🐒</span>
          <span className="text-lg font-semibold text-slate-800">飞猴</span>
        </div>
        <p className="mb-5 text-xs text-slate-400">跨境电商智能体 · 登录</p>

        <label className="mb-1 block text-xs text-slate-500">账号</label>
        <input
          className="mb-3 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          placeholder="alice@acme"
          value={account}
          onChange={(e) => setAccount(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />

        <label className="mb-1 block text-xs text-slate-500">密码</label>
        <input
          type="password"
          className="mb-4 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          placeholder="mock 不校验"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />

        {err && <p className="mb-3 text-xs text-rose-600">{err}</p>}

        <button
          className="w-full rounded-lg bg-slate-800 py-2 text-sm text-white disabled:opacity-40"
          disabled={busy}
          onClick={submit}
        >
          {busy ? "登录中…" : "登录"}
        </button>

        <p className="mt-3 text-xs text-slate-400">
          mock 账号：alice@acme · bob@acme · carol@globex
        </p>
      </div>
    </div>
  );
}
