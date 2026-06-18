import { useState } from "react";

export function Composer({
  disabled,
  onSend,
}: {
  disabled: boolean;
  onSend: (text: string) => void;
}) {
  const [text, setText] = useState("");
  const canSend = !!text.trim() && !disabled;
  const send = () => {
    if (!canSend) return;
    onSend(text.trim());
    setText("");
  };
  return (
    <div className="border-t border-slate-200 bg-white px-4 py-3">
      <div className="mx-auto max-w-2xl">
        <div className="flex items-end gap-2 rounded-2xl border border-slate-300 bg-white px-3 py-2 shadow-sm transition-colors focus-within:border-slate-400 focus-within:ring-1 focus-within:ring-slate-300">
          <textarea
            className="max-h-40 flex-1 resize-none bg-transparent text-sm leading-6 text-slate-800 outline-none placeholder:text-slate-400"
            rows={1}
            placeholder="问问题，或下指令…"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
          />
          <button
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-slate-900 text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-400"
            disabled={!canSend}
            onClick={send}
            aria-label="发送"
            title="发送"
          >
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M12 19V5M5 12l7-7 7 7" />
            </svg>
          </button>
        </div>
        <p className="mt-1.5 px-1 text-xs text-slate-400">
          Enter 发送 · Shift+Enter 换行
        </p>
      </div>
    </div>
  );
}
