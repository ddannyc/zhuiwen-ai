import { marked } from "marked";
import { useMemo } from "react";

marked.setOptions({ breaks: true, gfm: true });

export function Markdown({ text }: { text: string }) {
  const html = useMemo(() => marked.parse(text) as string, [text]);
  return (
    <div
      className="prose-chat text-sm leading-relaxed [&_table]:my-2 [&_table]:w-full [&_table]:border-collapse [&_td]:border [&_td]:border-slate-200 [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-slate-200 [&_th]:bg-slate-50 [&_th]:px-2 [&_th]:py-1 [&_h3]:mb-1 [&_h3]:mt-2 [&_h3]:font-semibold"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
