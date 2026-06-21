import type { ChatAction } from "../lib/contract";
import { Markdown } from "./renderers/Markdown";
import { BoxTableCard } from "./renderers/BoxTableCard";
import { RuleCiteCard } from "./renderers/RuleCiteCard";
import { CollectJobCard } from "./renderers/CollectJobCard";

// 按 action.type 分发渲染。正文 markdown 渲，action 附富组件。
// 例外：空检索（rules_search empty）的守卫文案由 RuleCiteCard 渲，正文不再渲——
// 否则与卡片两条「未找到」重叠（后端此时也不流式 token，见 chat/service.converse_stream）。
export function MessageRenderer({
  content,
  action,
}: {
  content: string;
  action: ChatAction | null;
}) {
  const suppressContent = action?.type === "rules_search" && action.empty;
  return (
    <div>
      {content && !suppressContent && <Markdown text={content} />}
      {action?.type === "box_list" && <BoxTableCard rows={action.rows} />}
      {action?.type === "rules_search" && (
        <RuleCiteCard empty={action.empty} cites={action.cites} />
      )}
      {action?.type === "collect_products" && (
        <CollectJobCard jobId={action.job_id} />
      )}
    </div>
  );
}
