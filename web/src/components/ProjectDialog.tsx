// Ask a device for a project's folder (V3-P103 §6.1, §6.2, §6.5): a new project (name, device, folder, default
// agent) or a folder added to a project (device, folder). Its client key is made as it opens; the request it sends
// becomes a card here, and a new project that is done opens its page.
//
// The folder: a ``local`` device's person picks it on that device (here: whether to make a new folder, and where
// to start, a suggestion); a ``user`` device's folder is picked here from its roots, one level at a time, and the
// path sent is only ever the one picked. Asked again (``again``): the projects are read first, and a ``user`` path
// must be picked again (it may be gone, or the roots changed).
import { useQueryClient } from "@tanstack/react-query";
import { ChevronRight, CornerLeftUp, Folder } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { newClientKey, readHubProjects, readRunnerDirs, type ClientKey, type ProjectRequestBody } from "@/api/hub";
import { hubKeys, useHubProjects } from "@/app/queries";
import { closeProjectDialog, useProjectDialog, type DialogInit } from "@/features/hub/dialog";
import {
  cannotAdd,
  folderNameProblem,
  joinPath,
  MODE_TEXT,
  nameProblem,
  runnerOf,
} from "@/features/hub/project_view";
import { useHubRequests } from "@/features/hub/requests";
import { answerLost, sendKeyed, useUnconfirmed } from "@/features/hub/unconfirmed";
import { cn } from "@/lib/cn";
import { failureText, ProjectRequestCard } from "./ProjectRequestCard";
import { UnconfirmedItem } from "./UnconfirmedRequests";
import { Button } from "./ui/button";
import { Dialog } from "./ui/dialog";

const field = "w-full rounded-md border border-line bg-bg px-2 py-1.5 text-sm outline-none focus:border-faint";

/** A ``user`` device's folders, one level at a time (names only; hidden ones and links the device leaves out). */
export function FolderPicker({ runner, picked, onPick }: {
  runner: string; picked: string | null; onPick: (path: string | null) => void;
}) {
  const [trail, setTrail] = useState<string[]>([]); // the folders entered, from a root down
  const [dirs, setDirs] = useState<string[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const here = trail.at(-1) ?? null;

  useEffect(() => {
    let live = true;
    setDirs(null);
    setProblem(null);
    readRunnerDirs(runner, here)
      .then((got) => live && setDirs(got.dirs))
      .catch((error: unknown) => live && setProblem(failureText(error)));
    return () => {
      live = false;
    };
  }, [runner, here]);

  const enter = (name: string) => {
    const next = here === null ? name : joinPath(here, name); // at the top the roots come whole
    setTrail([...trail, next]);
    onPick(null);
  };
  return (
    <div className="rounded-lg border border-line" aria-label="Folders on the device" role="group">
      <div className="flex items-center gap-1 border-b border-line px-2 py-1 text-xs text-muted">
        <button type="button" className="rounded p-1 hover:bg-raised disabled:opacity-40" disabled={!trail.length}
          aria-label="Up one folder" onClick={() => { setTrail(trail.slice(0, -1)); onPick(null); }}>
          <CornerLeftUp className="size-3.5" />
        </button>
        <span className="min-w-0 flex-1 truncate" title={here ?? ""}>{here ?? "Roots"}</span>
      </div>
      <ul className="max-h-40 overflow-y-auto py-1 text-sm">
        {dirs === null && !problem && <li className="px-3 py-1 text-xs text-faint">Reading…</li>}
        {problem && <li role="alert" className="px-3 py-1 text-xs text-danger">{problem}</li>}
        {dirs?.length === 0 && <li className="px-3 py-1 text-xs text-faint">No folders here.</li>}
        {dirs?.map((name) => (
          <li key={name}>
            <button type="button" onClick={() => enter(name)}
              className="flex w-full items-center gap-2 px-3 py-1 text-left hover:bg-raised">
              <Folder className="size-3.5 shrink-0 text-muted" />
              <span className="min-w-0 flex-1 truncate">{name}</span>
              <ChevronRight className="size-3.5 text-faint" />
            </button>
          </li>
        ))}
      </ul>
      <div className="flex items-center gap-2 border-t border-line px-2 py-1.5">
        <Button className="text-xs" disabled={here === null} onClick={() => onPick(here)}>Choose this folder</Button>
        {picked && <span className="min-w-0 truncate text-xs text-ok" title={picked}>Chosen: {picked}</span>}
      </div>
    </div>
  );
}

function Form({ init, onDone }: { init: DialogInit; onDone: () => void }) {
  const hub = useHubProjects();
  const client = useQueryClient();
  const navigate = useNavigate();
  const store = useHubRequests();
  const pendingKeys = useUnconfirmed();
  const [name, setName] = useState(init.project ?? "");
  const [runner, setRunner] = useState(init.runner ?? "");
  const [createDir, setCreateDir] = useState(init.createDir ?? false);
  const [start, setStart] = useState(init.again && init.path ? init.path : "");
  const [picked, setPicked] = useState<string | null>(null);
  const [newFolder, setNewFolder] = useState("");
  const [agent, setAgent] = useState(init.agent ?? "");
  const [key, setKey] = useState<ClientKey | null>(null);
  const [sent, setSent] = useState<number | null>(null);
  const [lostKey, setLostKey] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [checked, setChecked] = useState(!init.again);
  const [checkFailed, setCheckFailed] = useState(false);
  const [checks, setChecks] = useState(0);

  useEffect(() => { // the key for this request, from this server's clock (H13)
    let live = true;
    newClientKey().then((made) => live && setKey(made)).catch(() => live && setKey(null));
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => { // asked again: only as things are now (a read that fails leaves nothing to send)
    if (!init.again) return;
    let live = true;
    setCheckFailed(false);
    client.fetchQuery({ queryKey: hubKeys.projects, queryFn: readHubProjects, staleTime: 0 })
      .then(() => live && setChecked(true), () => live && setCheckFailed(true));
    return () => {
      live = false;
    };
  }, [client, init.again, checks]);

  const request = sent !== null ? store.entries.get(sent)?.request ?? null : null;
  useEffect(() => { // done: the project's page
    if (request?.state === "done" && request.project) {
      onDone();
      navigate(`/project?name=${encodeURIComponent(request.project)}`);
    }
  }, [navigate, onDone, request?.project, request?.state]);

  const data = hub.data;
  const creating = init.kind === "create";
  const runners = (data?.runners ?? []).filter((r) => r.name);
  const chosen = runnerOf(data, runner);
  const mode = chosen?.approve?.mode;
  const blocked = chosen ? cannotAdd(chosen) : null;
  const nameWrong = creating ? nameProblem(name, (data?.projects ?? []).map((p) => p.name)) : null;
  const folderWrong = mode === "user" ? folderNameProblem(newFolder) : null;
  const lost = pendingKeys.find((u) => u.client_key === lostKey);
  useEffect(() => { // the unconfirmed one was discarded: its key is done with; the next try is a request of its own
    if (lostKey === null || lost) return;
    setLostKey(null);
    setKey(null);
    newClientKey().then(setKey).catch(() => setKey(null));
  }, [lost, lostKey]);
  const ready = !!key && checked && !busy && !nameWrong && !!chosen && !blocked && !folderWrong
    && (mode !== "user" || picked !== null) && sent === null && !lost;

  const submit = async () => {
    if (!ready || !key || !chosen?.name) return;
    const body: ProjectRequestBody = { kind: init.kind, project: creating ? name : init.project ?? "", runner: chosen.name };
    if (mode === "user" && picked) {
      body.path = newFolder ? joinPath(picked, newFolder) : picked;
      if (newFolder) body.create_dir = true;
    } else {
      if (createDir) body.create_dir = true;
      if (start.trim()) body.path = start.trim();
    }
    if (creating && agent) body.agent = agent;
    setBusy(true);
    setProblem(null);
    const result = await sendKeyed({ ...key, route: "request", body, at: Date.now() }, false);
    setBusy(false);
    if ("request" in result) {
      setSent(result.request.id);
      return;
    }
    if (answerLost(result.error)) {
      setLostKey(key.client_key);
      return;
    }
    setProblem(failureText(result.error));
    newClientKey().then(setKey).catch(() => setKey(null)); // refused: the next try is a request of its own
  };

  if (sent !== null) {
    return (
      <div className="space-y-3">
        <ProjectRequestCard id={sent} />
        <div className="flex justify-end"><Button onClick={onDone}>Close</Button></div>
      </div>
    );
  }
  return (
    <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      {creating ? (
        <div>
          <label htmlFor="project-name" className="block text-xs font-medium text-muted">Name</label>
          <input id="project-name" className={cn(field, "mt-1")} value={name} maxLength={200}
            onChange={(event) => setName(event.target.value)} autoComplete="off" />
          {name && nameWrong && <p className="mt-1 text-xs text-danger">{nameWrong}</p>}
        </div>
      ) : (
        <p className="text-sm">Project <span className="font-medium">{init.project}</span></p>
      )}
      <fieldset>
        <legend className="text-xs font-medium text-muted">Device</legend>
        {runners.length === 0 && <p className="mt-1 text-xs text-faint">No device is connected to the hub.</p>}
        <div className="mt-1 space-y-1">
          {runners.map((r) => {
            const why = cannotAdd(r) ?? (!creating && r.folders?.[init.project ?? ""] !== undefined
              ? "It has a folder for this project already." : null);
            return (
              <label key={r.name} className={cn("flex items-start gap-2 rounded-lg px-2 py-1.5",
                why ? "opacity-50" : "hover:bg-raised")}>
                <input type="radio" name="runner" className="mt-1" disabled={!!why} checked={runner === r.name}
                  onChange={() => { setRunner(r.name!); setPicked(null); }} />
                <span className="min-w-0">
                  <span className="text-sm">{r.name}</span>
                  <span className="block text-xs text-faint">{why ?? MODE_TEXT[r.approve?.mode ?? ""] ?? ""}</span>
                </span>
              </label>
            );
          })}
        </div>
      </fieldset>
      {chosen && !blocked && mode === "local" && (
        <div className="space-y-2">
          <p className="text-xs text-muted">The folder is picked on {chosen.name}'s screen.</p>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={createDir} onChange={(event) => setCreateDir(event.target.checked)} />
            Make a new folder
          </label>
          <div>
            <label htmlFor="project-start" className="block text-xs font-medium text-muted">Start in (a suggestion)</label>
            <input id="project-start" className={cn(field, "mt-1")} value={start} maxLength={4000}
              onChange={(event) => setStart(event.target.value)} autoComplete="off" />
          </div>
        </div>
      )}
      {chosen && !blocked && mode === "user" && (
        <div className="space-y-2">
          {init.again && init.path && <p className="text-xs text-faint">Before: {init.path} (pick it again)</p>}
          <FolderPicker key={chosen.name} runner={chosen.name!} picked={picked} onPick={setPicked} />
          {init.again && init.path && picked !== null && (newFolder ? joinPath(picked, newFolder) : picked) !== init.path && (
            <p className="text-xs text-wait">Not the folder asked for before: this asks for another one.</p>
          )}
          <div>
            <label htmlFor="project-new-folder" className="block text-xs font-medium text-muted">
              New folder in it (optional)
            </label>
            <input id="project-new-folder" className={cn(field, "mt-1")} value={newFolder} maxLength={255}
              onChange={(event) => setNewFolder(event.target.value)} autoComplete="off" />
            {folderWrong && <p className="mt-1 text-xs text-danger">{folderWrong}</p>}
          </div>
        </div>
      )}
      {creating && (
        <div>
          <label htmlFor="project-agent" className="block text-xs font-medium text-muted">Default agent (optional)</label>
          <select id="project-agent" className={cn(field, "mt-1")} value={agent} onChange={(e) => setAgent(e.target.value)}>
            <option value="">None</option>
            {(data?.agents ?? []).map((a) => a.name && <option key={a.name} value={a.name}>{a.name}</option>)}
          </select>
        </div>
      )}
      {lost && <UnconfirmedItem entry={lost} onSent={setSent} />}
      {problem && <p role="alert" className="text-xs text-danger">{problem}</p>}
      {checkFailed && !checked && (
        <div role="alert" className="flex items-center gap-2 text-xs text-danger">
          <span className="flex-1">The projects could not be read again; asking again waits for them.</span>
          <Button className="text-xs" onClick={() => setChecks((n) => n + 1)}>Read again</Button>
        </div>
      )}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>Close</Button>
        {!lost && <Button type="submit" variant="primary" disabled={!ready}>Ask the device</Button>}
      </div>
    </form>
  );
}

/** The dialog, once for the page (the shell draws it). */
export function ProjectDialogHost() {
  const { init, generation } = useProjectDialog();
  const title = init?.kind === "add_folder" ? "Add a folder" : "New project";
  return (
    <Dialog open={init !== null} onOpenChange={(open) => !open && closeProjectDialog()} title={title}
      description={init?.again ? "Asked again as things are now." : undefined}>
      {init && <Form key={generation} init={init} onDone={closeProjectDialog} />}
    </Dialog>
  );
}
