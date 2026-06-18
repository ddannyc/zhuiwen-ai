import type { RuleCite } from "../../lib/contract";

// 合规硬约束渲染（对齐规则知识库设计 + redesign 冲突①裁决）：
//  ① 空检索 → 灰条「未找到，以官方公告为准」，不渲生成文本。
//  ② 每条结论挂 source_url + version 角标。
//  ③ confidence=low 或超时效阈值 → 黄色提示条。
// 视觉上与 analyze 自由生成气泡明显区分（左侧蓝边 + 溯源图标）。

const STALE_DAYS = 180;

function isStale(iso: string): boolean {
  const days = (Date.now() - new Date(iso).getTime()) / 86_400_000;
  return days > STALE_DAYS;
}

export function RuleCiteCard({
  empty,
  cites,
}: {
  empty: boolean;
  cites: RuleCite[];
}) {
  if (empty) {
    return (
      <div className="mt-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-500">
        未找到相关规则，请以平台最新公告为准。
      </div>
    );
  }

  return (
    <div className="mt-2 space-y-2">
      {cites.map((c, i) => {
        const stale = isStale(c.last_verified_at);
        const low = c.confidence === "low";
        return (
          <div
            key={i}
            className="rounded-lg border border-slate-200 border-l-4 border-l-sky-500 bg-white px-3 py-2 text-sm"
          >
            <div className="flex items-start gap-2">
              <span className="mt-0.5 text-sky-500">§</span>
              <div className="flex-1">
                <p className="leading-relaxed text-slate-800">{c.summary}</p>
                <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
                  <a
                    href={c.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-sky-600 underline underline-offset-2"
                  >
                    来源
                  </a>
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-500">
                    版本 {c.version}
                  </span>
                  <span className="text-slate-400">核验 {c.last_verified_at}</span>
                </div>
                {(stale || low) && (
                  <div className="mt-1.5 rounded bg-amber-50 px-2 py-1 text-xs text-amber-700">
                    {low && "信度偏低；"}
                    {stale && `已超 ${STALE_DAYS} 天未核验；`}
                    建议以平台官方最新公告复核。
                  </div>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
