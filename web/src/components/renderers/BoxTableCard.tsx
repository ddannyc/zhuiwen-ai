import { useState } from "react";
import type { BoxRow } from "../../lib/contract";

// box_list 动作内联表格卡。MVP 支持勾选 + 批量占位按钮。
export function BoxTableCard({ rows }: { rows: BoxRow[] }) {
  const [sel, setSel] = useState<Set<string>>(new Set());
  const toggle = (id: string) =>
    setSel((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

  return (
    <div className="mt-2 overflow-hidden rounded-lg border border-slate-200">
      <table className="w-full text-sm">
        <thead className="bg-slate-50 text-left text-slate-500">
          <tr>
            <th className="w-8 px-2 py-1.5"></th>
            <th className="px-2 py-1.5">商品</th>
            <th className="px-2 py-1.5">价格</th>
            <th className="px-2 py-1.5">状态</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-t border-slate-100">
              <td className="px-2 py-1.5">
                <input
                  type="checkbox"
                  checked={sel.has(r.id)}
                  onChange={() => toggle(r.id)}
                />
              </td>
              <td className="px-2 py-1.5">{r.title}</td>
              <td className="px-2 py-1.5 tabular-nums">{r.price}</td>
              <td className="px-2 py-1.5 text-slate-500">{r.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {sel.size > 0 && (
        <div className="flex items-center gap-2 border-t border-slate-200 bg-slate-50 px-2 py-1.5 text-xs">
          <span className="text-slate-500">已选 {sel.size}</span>
          <button className="rounded bg-slate-800 px-2 py-1 text-white">批量翻译</button>
          <button className="rounded border border-slate-300 px-2 py-1">删除</button>
        </div>
      )}
    </div>
  );
}
