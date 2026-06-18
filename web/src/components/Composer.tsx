import { useState } from "react";

export function Composer({
  disabled,
  onSend,
}: {
  disabled: boolean;
  onSend: (text: string) => void;
}) {
  const [text, setText] = useState("");
  const send = () => {
    const t = text.trim();
    if (!t || disabled) return;
    onSend(t);
    setText("");
  };
  return (
    <div className="border-t border-slate-200 bg-white p-3">
      <div className="flex items-end gap-2 rounded-xl border border-slate-300 bg-white px-3 py-2 focus-within:border-slate-400">
        <textarea
          className="max-h-32 flex-1 resize-none text-sm outline-none"
          rows={1}
          placeholder="问问题，或下指令（列采集箱 / 自动采集 / 平台规则…）"
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
          className="rounded-lg bg-slate-800 px-3 py-1.5 text-sm text-white disabled:opacity-40"
          disabled={disabled || !text.trim()}
          onClick={send}
        >
          发送
        </button>
      </div>
    </div>
  );
}
