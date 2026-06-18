import type { ChatAction } from "../lib/contract";
import { Markdown } from "./renderers/Markdown";
import { BoxTableCard } from "./renderers/BoxTableCard";
import { RuleCiteCard } from "./renderers/RuleCiteCard";
import { CollectJobCard } from "./renderers/CollectJobCard";

// 按 action.type 分发渲染。正文 markdown 始终渲，action 附富组件。
export function MessageRenderer({
  content,
  action,
}: {
  content: string;
  action: ChatAction | null;
}) {
  return (
    <div>
      {content && <Markdown text={content} />}
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
