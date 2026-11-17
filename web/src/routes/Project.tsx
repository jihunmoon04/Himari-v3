// A hub project's own page, ``/project?name=<name>`` (V3-P103 §6.2): the name is a query value (the hub's names may
// hold ``/``, ``?``, ``#``). Its settings (default agent, profile, permission preset: the preset only after a
// confirmation), the devices that have a folder for it (each can be taken out: a ``remove_folder`` request the device
// ends), adding a device's folder, the folders held for its name (adopt), and its requests' cards.
import { useQueryClient } from "@tanstack/react-query";
import { FolderMinus, FolderPlus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { newClientKey, setProjectSettings, type HubProject, type HubProjects } from "@/api/hub";
import { doubtProjects, hubKeys, readProjectsNow, useHubProjects } from "@/app/queries";
import { AdoptButton } from "@/components/HeldFolders";
import { failureText, ProjectRequestCard } from "@/components/ProjectRequestCard";
import { PageHeader } from "@/components/Shell";
import { UnconfirmedRequests } from "@/components/UnconfirmedRequests";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { openProjectDialog } from "@/features/hub/dialog";
import { foldersOf, heldFor } from "@/features/hub/project_view";
import { isOpen, requests, useHubRequests } from "@/features/hub/requests";
import { answerLost, onlySend, sendKeyed, unconfirmed, unconfirmedFor, useUnconfirmed } from "@/features/hub/unconfirmed";
import { toast } from "@/features/toast";
import { cn } from "@/lib/cn";

const field = "w-full rounded-md border border-line bg-bg px-2 py-1.5 text-sm outline-none focus:border-faint";

type Changes = { agent?: string | null; profile?: string | null; preset?: string | null };

function Settings({ project }: { project: HubProject }) {
  const hub = useHubProjects();
  const client = useQueryClient();
  const [agent, setAgent] = useState(project.default_agent ?? "");
  const [profile, setProfile] = useState(project.profile ?? "");
  const [preset, setPreset] = useState(project.preset ?? "");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const tried = useRef<Changes | null>(null); // a change whose answer was lost, until the projects say what became of it
  const changed: Changes = {};
  if (agent !== (project.default_agent ?? "")) changed.agent = agent || null;
  if (profile !== (project.profile ?? "")) changed.profile = profile || null;
  if (preset !== (project.preset ?? "")) changed.preset = preset || null;
  const any = Object.keys(changed).length > 0;

  const save = async () => {
    if ("preset" in changed && !window.confirm(
      `Change the permission preset of ${project.name} to ${changed.preset ?? "none"}? Its agents ask by it from their next task.`)) {
      return;
    }
    setBusy(true);
    setProblem(null);
    try {
      await setProjectSettings(project.name ?? "", changed);
      toast("Saved.");
      void client.invalidateQueries({ queryKey: hubKeys.projects });
    } catch (error) {
      if (answerLost(error)) { // it may have changed: the settings as the hub has them, before anything else
        setProblem("The hub did not answer; the projects are read again to show whether it changed.");
        tried.current = changed;
        doubtProjects();
        await readAgain();
      } else {
        setProblem(failureText(error));
        void client.invalidateQueries({ queryKey: hubKeys.projects });
      }
    }
    setBusy(false);
  };

  /** The projects were read again (here or by any other control): the form shows the settings as the hub has them
   *  now, and says whether a change whose answer was lost was made (§4.1). */
  const shown = (now: HubProject | undefined) => {
    if (now) {
      setAgent(now.default_agent ?? "");
      setProfile(now.profile ?? "");
      setPreset(now.preset ?? "");
      const attempt = tried.current;
      if (attempt) {
        const has = { agent: now.default_agent, profile: now.profile, preset: now.preset };
        const made = (Object.keys(attempt) as (keyof Changes)[]).every((name) => (attempt[name] ?? null) === (has[name] ?? null));
        toast(made ? "The hub had saved it." : "It was not saved: the settings are as the hub has them.",
          made ? undefined : "error");
      }
    }
    tried.current = null;
    setProblem(null);
  };

  const readAgain = async () => {
    setBusy(true);
    if (await readProjectsNow(client)) {
      shown(client.getQueryData<HubProjects>(hubKeys.projects)?.projects?.find((p) => p.name === project.name));
    }
    setBusy(false);
  };

  const unsureNow = hub.doubted;
  useEffect(() => { // the doubt ended by a read elsewhere (an unconfirmed request's, a held folder's): the same
    if (!unsureNow && tried.current) shown(hub.data?.projects?.find((p) => p.name === project.name));
  });

  const unsure = hub.doubted;
  const profiles = (hub.data?.profiles ?? []).filter((p) => p.name);
  return (
    <section aria-label="Settings" className="space-y-3">
      <h2 className="text-sm font-medium">Settings</h2>
      <div className="grid gap-3 sm:grid-cols-3">
        <div>
          <label htmlFor="project-default-agent" className="block text-xs font-medium text-muted">Default agent</label>
          <select id="project-default-agent" className={cn(field, "mt-1")} value={agent}
            onChange={(e) => setAgent(e.target.value)}>
            <option value="">None</option>
            {(hub.data?.agents ?? []).map((a) => a.name && <option key={a.name} value={a.name}>{a.name}</option>)}
            {agent && !(hub.data?.agents ?? []).some((a) => a.name === agent) && <option value={agent}>{agent}</option>}
          </select>
        </div>
        <div>
          <label htmlFor="project-profile" className="block text-xs font-medium text-muted">Profile</label>
          <select id="project-profile" className={cn(field, "mt-1")} value={profile}
            onChange={(e) => setProfile(e.target.value)}>
            <option value="">None</option>
            {profiles.map((p) => <option key={p.name} value={p.name}>{p.name}{p.agent ? ` (${p.agent})` : ""}</option>)}
            {profile && !profiles.some((p) => p.name === profile) && <option value={profile}>{profile}</option>}
          </select>
        </div>
        <div>
          <label htmlFor="project-preset" className="block text-xs font-medium text-muted">Permission preset</label>
          <input id="project-preset" className={cn(field, "mt-1")} value={preset} maxLength={200} autoComplete="off"
            onChange={(e) => setPreset(e.target.value)} placeholder="None" />
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Button variant="primary" disabled={!any || busy || unsure} onClick={() => void save()}>Save</Button>
        {unsure && <Button disabled={busy} onClick={() => void readAgain()}>Read the projects again</Button>}
        {problem && <p role="alert" className="text-xs text-danger">{problem}</p>}
        {unsure && !busy && (
          <p role="alert" className="text-xs text-danger">The projects could not be read: whether it changed is not known yet.</p>
        )}
      </div>
    </section>
  );
}

function RemoveFolder({ project, runner, connected }: { project: string; runner: string; connected: boolean }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const waiting = unconfirmedFor(useUnconfirmed(), "remove", project, runner); // never a second key for it
  const remove = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const sent = await onlySend("remove", project, runner, async () => {
        if (unconfirmedFor(unconfirmed.getSnapshot(), "remove", project, runner)) return "waiting" as const;
        const key = await newClientKey();
        return sendKeyed({ ...key, route: "remove", body: { project, runner }, at: Date.now() }, false);
      });
      if (sent === null) setProblem("This removal is being sent already.");
      else if (sent === "waiting") setProblem("A removal whose answer was lost waits: send it again or discard it first.");
      else if ("error" in sent && !answerLost(sent.error)) setProblem(failureText(sent.error));
      else setOpen(false); // its card (or, its answer lost, the unconfirmed one) shows below
    } catch (error) {
      setProblem(failureText(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button variant="ghost" disabled={!connected || !!waiting}
        title={waiting ? "A removal whose answer was lost waits below: send it again or discard it first."
          : connected ? undefined : "The device is not connected."}
        aria-label={`Remove ${runner}'s folder`} onClick={() => setOpen(true)}>
        <FolderMinus className="size-3.5" /> Remove
      </Button>
      <Dialog open={open} onOpenChange={setOpen} title={`Remove ${runner}'s folder?`}
        description="It is taken out of that device's runner.json only; the folder and its work stay.">
        {problem && <p role="alert" className="mb-2 text-xs text-danger">{problem}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setOpen(false)}>Keep</Button>
          <Button variant="danger" disabled={busy} onClick={() => void remove()}>Remove</Button>
        </div>
      </Dialog>
    </>
  );
}

export function Project() {
  const [params] = useSearchParams();
  const name = params.get("name") ?? "";
  const hub = useHubProjects();
  const store = useHubRequests();
  const project = (hub.data?.projects ?? []).find((p) => p.name === name);
  const folders = foldersOf(hub.data, name);
  const held = heldFor(hub.data, name);
  const cards = [...store.entries.entries()]
    // followed ones whatever became of them (one the hub no longer has says its result cannot be confirmed)
    .filter(([id, entry]) => entry.request?.project === name
      && (store.tracked.includes(id) || (isOpen(entry.request) && !entry.unknown)))
    .map(([id]) => id).sort((a, b) => b - a);

  return (
    <>
      <PageHeader>
        <h1 className="truncate text-sm font-medium" title={name}>{name || "Project"}</h1>
      </PageHeader>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl space-y-6 px-4 py-6">
          {hub.status === "off" && <p className="text-sm text-muted">The hub is not set up on this server.</p>}
          {hub.status === "down" && <p role="alert" className="text-sm text-danger">The hub is not answering.</p>}
          {hub.status === "on" && !project && (
            <p className="text-sm text-muted">The hub has no project named {name ? `“${name}”` : "this"}.</p>
          )}
          {project && <Settings key={`${project.default_agent}|${project.profile}|${project.preset}`} project={project} />}
          {project && (
            <section aria-label="Folders on devices" className="space-y-2">
              <div className="flex items-center gap-2">
                <h2 className="flex-1 text-sm font-medium">Folders on devices</h2>
                <Button onClick={() => openProjectDialog({ kind: "add_folder", project: name })}>
                  <FolderPlus className="size-3.5" /> Add a folder
                </Button>
              </div>
              {folders.length === 0 && <p className="text-sm text-faint">No connected device has a folder for it.</p>}
              <ul className="divide-y divide-line rounded-xl border border-line">
                {folders.map((f) => (
                  <li key={f.runner} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
                    <span className="font-medium">{f.runner}</span>
                    <span className={cn("text-xs", f.connected ? "text-ok" : "text-faint")}>
                      {f.connected ? "connected" : "not connected"}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-xs text-muted" title={f.folder ?? ""}>{f.folder ?? ""}</span>
                    <RemoveFolder project={name} runner={f.runner} connected={f.connected} />
                  </li>
                ))}
              </ul>
            </section>
          )}
          {held.length > 0 && (
            <section aria-label="Held folders" className="space-y-2">
              <h2 className="text-sm font-medium">Held folders</h2>
              <p className="text-xs text-faint">Devices that reported a folder of this name while it was reserved.</p>
              <ul className="divide-y divide-line rounded-xl border border-line">
                {held.map((h) => (
                  <li key={h.runner} className="flex items-center gap-3 px-3 py-2 text-sm">
                    <span className="flex-1">{h.runner} <span className="text-xs text-faint">
                      {h.connected ? "connected" : "not connected"}</span></span>
                    <AdoptButton project={name} runner={h.runner} connected={h.connected} />
                  </li>
                ))}
              </ul>
            </section>
          )}
          {hub.status === "on" && (
            <section aria-label="Requests" className="space-y-2">
              <h2 className="text-sm font-medium">Requests</h2>
              <UnconfirmedRequests project={name} />
              {cards.length === 0 && <p className="text-sm text-faint">No open request.</p>}
              {cards.map((id) => (
                <ProjectRequestCard key={id} id={id}
                  onClose={store.tracked.includes(id) ? () => requests.untrack(id) : undefined} />
              ))}
            </section>
          )}
        </div>
      </div>
    </>
  );
}
