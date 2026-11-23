// The right-hand panel of a page (V3-P102 §4.4, §4.7): tabs; on a wide screen beside the page, its width dragged
// from its left edge and kept in this browser; under 768 px a sheet that rises from the bottom over the page.
// The width is set through the CSSOM (the page's CSP allows no style attributes).
import { X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type PointerEvent, type ReactNode } from "react";
import { useNarrow } from "@/components/Shell";
import { cn } from "@/lib/cn";
import { IconButton } from "./ui/button";

const WIDTH_KEY = "himari.next.panel.width";
const MIN = 280;
const MAX = 760;
const DEFAULT = 340;

interface PanelTab {
  id: string;
  label: string;
  content: ReactNode;
}

function load(key: string, fallback: string): string {
  try {
    return window.localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback; // storage is off: this page's choice only
  }
}

function keep(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // storage is off: nothing is kept
  }
}

const clamp = (width: number) => Math.round(Math.max(MIN, Math.min(width, MAX, window.innerWidth * 0.65)));

export function SidePanel({ open, onClose, tabs, storageKey, label }: {
  open: boolean;
  onClose: () => void;
  tabs: PanelTab[];
  storageKey: string; // which tab was open last, per kind of page
  label: string;
}) {
  const narrow = useNarrow();
  const tabKey = `himari.next.panel.tab.${storageKey}`;
  const [tab, setTab] = useState(() => load(tabKey, tabs[0]?.id ?? ""));
  const shown = tabs.find((t) => t.id === tab) ?? tabs[0];
  const box = useRef<HTMLElement>(null);
  const width = useRef(clamp(Number(load(WIDTH_KEY, String(DEFAULT))) || DEFAULT));

  useLayoutEffect(() => {
    if (box.current) box.current.style.width = narrow ? "" : `${width.current}px`;
  }, [narrow, open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const choose = (id: string) => {
    setTab(id);
    keep(tabKey, id);
  };

  const drag = (start: PointerEvent<HTMLDivElement>) => {
    const node = box.current;
    if (!node) return;
    const handle = start.currentTarget;
    handle.setPointerCapture(start.pointerId);
    const from = start.clientX;
    const was = width.current;
    const move = (event: globalThis.PointerEvent) => {
      width.current = clamp(was + (from - event.clientX));
      node.style.width = `${width.current}px`;
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      keep(WIDTH_KEY, String(width.current));
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  };

  if (!open) return null;
  const body = (
    <>
      <div className="flex items-center gap-1 border-b border-line px-2" role="tablist" aria-label={label}>
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={shown?.id === t.id}
            onClick={() => choose(t.id)}
            className={cn("border-b-2 px-2.5 py-2 text-sm", shown?.id === t.id
              ? "border-accent text-ink" : "border-transparent text-muted hover:text-ink")}
          >
            {t.label}
          </button>
        ))}
        <IconButton label={`Close ${label.toLowerCase()}`} onClick={onClose} className="ml-auto">
          <X className="size-4" />
        </IconButton>
      </div>
      <div role="tabpanel" aria-label={shown?.label} className="min-h-0 flex-1 overflow-y-auto p-4 text-sm">
        {shown?.content}
      </div>
    </>
  );

  if (narrow) {
    return (
      <>
        <button type="button" aria-label={`Close ${label.toLowerCase()}`} className="fixed inset-0 z-30 bg-black/40"
          onClick={onClose} />
        <aside ref={box} aria-label={label}
          className="fixed inset-x-0 bottom-0 z-40 flex h-[75dvh] flex-col rounded-t-2xl border-t border-line bg-panel shadow-xl">
          {body}
        </aside>
      </>
    );
  }
  return (
    <aside ref={box} aria-label={label} className="relative flex shrink-0 flex-col border-l border-line bg-panel">
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the panel"
        onPointerDown={drag}
        className="absolute inset-y-0 -left-1 z-10 w-2 cursor-col-resize hover:bg-accent/30"
      />
      {body}
    </aside>
  );
}
