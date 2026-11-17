import { X } from "lucide-react";
import { dismissToast, useToasts } from "@/features/toast";
import { cn } from "@/lib/cn";

export function Toasts() {
  const toasts = useToasts();
  if (!toasts.length) return null;
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-4 z-50 flex flex-col items-center gap-2 px-4" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} role={t.tone === "error" ? "alert" : "status"}
          className={cn("pointer-events-auto flex max-w-lg items-center gap-3 rounded-xl border bg-panel px-4 py-2 text-sm shadow-lg",
            t.tone === "error" ? "border-danger/40 text-danger" : "border-line text-ink")}>
          <span className="min-w-0 flex-1">{t.text}</span>
          {t.action && (
            <button type="button" className="shrink-0 font-medium text-accent hover:underline"
              onClick={() => { dismissToast(t.id); t.action!.run(); }}>
              {t.action.label}
            </button>
          )}
          <button type="button" aria-label="Dismiss" className="shrink-0 text-faint hover:text-ink" onClick={() => dismissToast(t.id)}>
            <X className="size-4" />
          </button>
        </div>
      ))}
    </div>
  );
}
