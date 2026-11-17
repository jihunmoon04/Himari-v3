import { ArrowDown } from "lucide-react";

/** Shown over the composer while the reader is scrolled up. */
export function JumpToLatest({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} aria-label="Jump to the latest"
      className="absolute -top-11 left-1/2 z-10 inline-flex -translate-x-1/2 items-center gap-1 rounded-full border border-line bg-panel px-3 py-1 text-xs text-muted shadow-sm hover:text-ink">
      <ArrowDown className="size-3.5" /> Latest
    </button>
  );
}
