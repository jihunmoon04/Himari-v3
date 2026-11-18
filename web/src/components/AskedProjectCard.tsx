// A project request HIMARI made in a conversation (V3-P103 §6.6), drawn like a hand-over card: the request's id from
// its ``hub_project_request`` result is followed (the page's one store, §5) and the card shows the user path's
// record, path and pairing code included (this is the operator's page). The model cannot allow it; the user does,
// here or in Approvals.
import { useEffect } from "react";
import { useHubProjects } from "@/app/queries";
import { KINDS } from "@/features/hub/project_view";
import { requests } from "@/features/hub/requests";
import type { AskedProject } from "@/features/timeline";
import { ProjectRequestCard } from "./ProjectRequestCard";

export function AskedProjectCard({ asked }: { asked: AskedProject }) {
  const hub = useHubProjects();
  const on = hub.status === "on";
  useEffect(() => {
    if (on && asked.requestId !== null) void requests.track(asked.requestId);
  }, [asked.requestId, on]);

  if (asked.requestId !== null && on) return <ProjectRequestCard id={asked.requestId} />;
  const what = [KINDS[asked.kind] ?? asked.kind, asked.project, asked.runner && `on ${asked.runner}`]
    .filter(Boolean).join(" · ");
  return (
    <section role="group" aria-label="Project request" className="rounded-xl border border-line px-4 py-3 text-sm">
      <p className="text-xs text-muted">{what || "Project request"}</p>
      <p className={asked.error ? "mt-1 whitespace-pre-wrap break-words text-danger" : "mt-1 text-muted"}>
        {asked.error ?? (asked.answered ? "The hub is not set up here." : "Asking the hub…")}
      </p>
    </section>
  );
}
