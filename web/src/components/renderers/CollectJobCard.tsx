import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { JobStage } from "../../lib/contract";

// 采集任务卡。SSE 只给 job_id，进度走 TanStack Query 轮询（终态前每 1.5s）。
// 对应后端 Temporal CollectWorkflow 的 activity 流转投影。
const STAGES: { key: JobStage; label: string }[] = [
  { key: "collecting", label: "采集" },
  { key: "scoring", label: "打分" },
  { key: "translating", label: "翻译" },
  { key: "publishing", label: "上架" },
];

const ORDER: JobStage[] = [
  "pending",
  "collecting",
  "scoring",
  "translating",
  "publishing",
  "done",
];

export function CollectJobCard({ jobId }: { jobId: string }) {
  const { data: job } = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.getJob(jobId),
    refetchInterval: (q) => {
      const s = q.state.data?.stage;
      return s === "done" || s === "failed" ? false : 1500;
    },
  });

  const cur = ORDER.indexOf(job?.stage ?? "pending");
  const failed = job?.stage === "failed";
  const done = job?.stage === "done";

  return (
    <div className="mt-2 rounded-lg border border-slate-200 bg-white px-3 py-2.5 text-sm">
      <div className="mb-2 flex items-center justify-between">
        <span className="font-medium text-slate-700">自动采集任务</span>
        <span
          className={
            done
              ? "text-emerald-600"
              : failed
                ? "text-rose-600"
                : "text-sky-600"
          }
        >
          {done
            ? "已完成"
            : failed
              ? "失败"
              : `进行中 ${job?.collected ?? 0}/${job?.target ?? 0}`}
        </span>
      </div>
      <div className="flex items-center gap-1">
        {STAGES.map((s) => {
          const idx = ORDER.indexOf(s.key);
          const state = idx < cur ? "past" : idx === cur ? "now" : "future";
          return (
            <div key={s.key} className="flex flex-1 flex-col items-center gap-1">
              <div
                className={
                  "h-1.5 w-full rounded-full " +
                  (state === "past" || done
                    ? "bg-emerald-400"
                    : state === "now"
                      ? "animate-pulse bg-sky-400"
                      : "bg-slate-200")
                }
              />
              <span
                className={
                  "text-xs " +
                  (state === "future" ? "text-slate-400" : "text-slate-600")
                }
              >
                {s.label}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
