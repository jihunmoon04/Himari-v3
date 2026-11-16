import type { HubSession } from "@/api/hub";
import { cn } from "@/lib/cn";

/** A session's state as a dot and a word: working, waiting for you, idle, closed; a device that is not connected. */
export function activityOf(session: HubSession, pending = 0): { label: string; tone: "run" | "wait" | "idle" | "off" } {
  if (session.state === "closed" || session.activity === "closed") return { label: "Closed", tone: "off" };
  if (pending > 0 || session.activity === "waiting") return { label: "Waiting for you", tone: "wait" };
  if (session.activity === "running") return { label: "Working", tone: "run" };
  return { label: "Idle", tone: "idle" };
}

export function StatusDot({ tone, className }: { tone: "run" | "wait" | "idle" | "off"; className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-block size-2 shrink-0 rounded-full",
        tone === "run" && "pulse bg-accent",
        tone === "wait" && "bg-wait",
        tone === "idle" && "bg-faint/60",
        tone === "off" && "border border-faint",
        className,
      )}
    />
  );
}

export function SessionBadge({ session, pending }: { session: HubSession; pending?: number }) {
  const { label, tone } = activityOf(session, pending);
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-line px-2 py-0.5 text-xs text-muted">
      <StatusDot tone={tone} />
      {label}
      {session.runner_connected === false && session.state !== "closed" && (
        <span className="text-danger">· {session.runner ?? "device"} offline</span>
      )}
    </span>
  );
}

export const sessionName = (session: HubSession) =>
  `${session.agent ?? "agent"}@${session.runner ?? "?"}${session.profile ? ` · ${session.profile}` : ""}`;
