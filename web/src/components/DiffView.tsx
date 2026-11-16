// A file change as the hub recorded it (path, before, after), as added and removed lines. The text is the agent's:
// it is shown as text only.
import { diffLines } from "diff";
import { useMemo } from "react";
import type { Diff } from "@/features/timeline";
import { cn } from "@/lib/cn";

const MAX_LINES = 400;

export function diffStats(diff: Diff): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const part of diffLines(diff.old ?? "", diff.new)) {
    if (part.added) added += part.count ?? 0;
    else if (part.removed) removed += part.count ?? 0;
  }
  return { added, removed };
}

export function DiffView({ diff }: { diff: Diff }) {
  const lines = useMemo(() => {
    const out: { sign: "+" | "-" | " "; text: string }[] = [];
    for (const part of diffLines(diff.old ?? "", diff.new)) {
      const sign = part.added ? "+" : part.removed ? "-" : " ";
      const texts = part.value.replace(/\n$/, "").split("\n");
      // unchanged runs are kept short: their first and last two lines
      const shown = sign === " " && texts.length > 5 ? [...texts.slice(0, 2), "⋯", ...texts.slice(-2)] : texts;
      for (const text of shown) out.push({ sign, text });
      if (out.length > MAX_LINES) break;
    }
    return out.slice(0, MAX_LINES);
  }, [diff]);
  const stats = useMemo(() => diffStats(diff), [diff]);
  return (
    <figure className="overflow-hidden rounded-md border border-line">
      <figcaption className="flex items-center gap-2 border-b border-line bg-raised px-2 py-1 font-mono text-xs">
        <span className="min-w-0 flex-1 truncate">{diff.path}</span>
        {diff.old === null && <span className="text-ok">new</span>}
        <span className="text-ok">+{stats.added}</span>
        <span className="text-danger">−{stats.removed}</span>
      </figcaption>
      <pre className="max-h-80 overflow-auto bg-code py-1 font-mono text-xs leading-5">
        {lines.map((line, index) => (
          <div
            key={index}
            className={cn("px-2", line.sign === "+" && "bg-ok/10 text-ok", line.sign === "-" && "bg-danger/10 text-danger")}
          >
            <span className="select-none pr-2 opacity-60">{line.sign}</span>
            {line.text}
          </div>
        ))}
      </pre>
      {(diff.cut || diff.more > 0) && (
        <p className="border-t border-line px-2 py-1 text-xs text-faint">
          {diff.cut && "Cut by the hub to fit. "}
          {diff.more > 0 && `${diff.more} more file${diff.more === 1 ? "" : "s"} not shown.`}
        </p>
      )}
    </figure>
  );
}
