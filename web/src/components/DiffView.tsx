// A file change as the hub recorded it (path, before, after), as added and removed lines. The text is the agent's:
// it is shown as text only.
import { diffLines } from "diff";
import { useMemo } from "react";
import { splitRows } from "@/features/hub/changes";
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

/** ``split``: before and after side by side (the panel's choice); otherwise one column of added and removed lines. */
export function DiffView({ diff, split, tall }: { diff: Diff; split?: boolean; tall?: boolean }) {
  const rows = useMemo(() => (split ? splitRows(diff, MAX_LINES) : []), [diff, split]);
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
      {split ? (
        <div className={cn("grid grid-cols-2 overflow-auto bg-code py-1 font-mono text-xs leading-5", tall ? "max-h-[32rem]" : "max-h-80")}
          role="table" aria-label={`${diff.path} before and after`}>
          {rows.map((row, index) => (
            <div key={index} role="row" className="contents">
              {[row.left, row.right].map((text, side) => (
                <div key={side} role="cell" className={cn("min-w-0 whitespace-pre px-2",
                  side === 0 && "border-r border-line",
                  row.kind === "gap" && "text-center text-faint",
                  side === 0 && (row.kind === "removed" || (row.kind === "changed" && text !== null)) && "bg-danger/10 text-danger",
                  side === 1 && (row.kind === "added" || (row.kind === "changed" && text !== null)) && "bg-ok/10 text-ok")}>
                  {row.kind === "gap" ? "⋯" : text ?? ""}
                </div>
              ))}
            </div>
          ))}
        </div>
      ) : (
      <pre className={cn("overflow-auto bg-code py-1 font-mono text-xs leading-5", tall ? "max-h-[32rem]" : "max-h-80")}>
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
      )}
      {(diff.cut || diff.more > 0) && (
        <p className="border-t border-line px-2 py-1 text-xs text-faint">
          {diff.cut && "Cut by the hub to fit. "}
          {diff.more > 0 && `${diff.more} more file${diff.more === 1 ? "" : "s"} not shown.`}
        </p>
      )}
    </figure>
  );
}
