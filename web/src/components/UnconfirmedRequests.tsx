// Requests and removals whose answer was lost (V3-P103 §4.1): kept in this tab until known. With a hub that keeps
// client keys (H13) one is sent again with the same key, time and body, which gives back the same request. Without
// it, sending again is not offered: the open requests are read and the ones that look the same are shown. A key the
// hub no longer takes (``client_key_expired`` …) ends the resending; the entry can then only be discarded, and
// (as without H13) only once the open requests and the projects were read again and shown.
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { ProjectRequestBody } from "@/api/hub";
import { hubKeys, useHubProjects } from "@/app/queries";
import { hubHasRequestKeys, KINDS } from "@/features/hub/project_view";
import { openRequests, requests, useHubRequests } from "@/features/hub/requests";
import { answerLost, sameAs, sendKeyed, unconfirmed, useUnconfirmed, type Unconfirmed } from "@/features/hub/unconfirmed";
import { failureText, ProjectRequestCard } from "./ProjectRequestCard";
import { Button } from "./ui/button";

export function UnconfirmedItem({ entry, onSent }: { entry: Unconfirmed; onSent?: (id: number) => void }) {
  const hub = useHubProjects();
  const store = useHubRequests();
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [looked, setLooked] = useState(false);
  const keys = hubHasRequestKeys(hub.data);
  const kind = entry.route === "remove" ? "remove_folder" : (entry.body as ProjectRequestBody).kind;
  const label = [KINDS[kind] ?? kind, entry.body.project, `on ${entry.body.runner}`].join(" · ");

  const reread = async () => { // shown once both are read again (a refused key may be discarded only then)
    setBusy(true);
    try {
      await Promise.allSettled([client.invalidateQueries({ queryKey: hubKeys.projects }), requests.refresh()]);
      setLooked(true);
    } finally {
      setBusy(false);
    }
  };
  const again = async () => {
    setBusy(true);
    setProblem(null);
    const sent = await sendKeyed(entry, true);
    setBusy(false);
    if ("request" in sent) {
      onSent?.(sent.request.id);
      return;
    }
    setProblem(answerLost(sent.error) ? "The hub did not answer again." : failureText(sent.error));
    if (!answerLost(sent.error)) await reread();
  };

  const alike = looked || entry.refused || !keys ? sameAs(entry, openRequests(store)) : [];
  return (
    <div role="group" aria-label="Unconfirmed request" className="rounded-xl border border-wait/40 bg-wait/5 px-4 py-3 text-sm">
      <p className="font-medium">Not confirmed: the hub's answer was lost</p>
      <p className="text-xs text-muted">{label} · sent {new Date(entry.at).toLocaleTimeString()}</p>
      {entry.refused && <p className="mt-1 text-xs text-danger">{entry.refused}</p>}
      {!keys && !entry.refused && (
        <p className="mt-1 text-xs text-faint">This hub cannot tell whether it was made: check the open requests below.</p>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        {keys && !entry.refused && (
          <Button variant="primary" disabled={busy} onClick={() => void again()}>Send again</Button>
        )}
        {(!keys || entry.refused) && (
          <Button disabled={busy} onClick={() => void reread()}>Read the open requests again</Button>
        )}
        <Button variant="ghost" disabled={busy || ((!keys || !!entry.refused) && !looked)}
          onClick={() => unconfirmed.drop(entry.client_key)}>Discard</Button>
      </div>
      {problem && <p role="alert" className="mt-2 text-xs text-danger">{problem}</p>}
      {alike.length > 0 && (
        <div className="mt-2 space-y-2">
          <p className="text-xs text-muted">Open requests like it:</p>
          {alike.map((r) => <ProjectRequestCard key={r.id} id={r.id} />)}
        </div>
      )}
      {(looked || !keys) && alike.length === 0 && (
        <p className="mt-2 text-xs text-faint">No open request like it.</p>
      )}
    </div>
  );
}

/** Every unconfirmed one of this tab (``project``: of that project only). */
export function UnconfirmedRequests({ project }: { project?: string }) {
  const items = useUnconfirmed().filter((u) => project === undefined || u.body.project === project);
  if (!items.length) return null;
  return (
    <div className="space-y-2">
      {items.map((entry) => <UnconfirmedItem key={entry.client_key} entry={entry} />)}
    </div>
  );
}
