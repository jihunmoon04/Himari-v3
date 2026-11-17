// The Tool servers' defaults for new conversations (V3-P53; each conversation may still choose its own) and the
// folders registered on this server (V3-P74 §10.3), which conversations attach and HIMARI reads.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { editWorkspace, readMcpSettings, readWorkspaces, registerWorkspace, setMcpDefault, unregisterWorkspace, type Workspace } from "@/api/settings";
import { Button } from "@/components/ui/button";
import { toast } from "@/features/toast";

const field = "rounded-md border border-line bg-bg px-2 py-1.5 text-sm outline-none focus:border-faint";
const why = (failure: unknown, fallback: string) => (failure instanceof ApiError ? failure.message : fallback);

export function ToolServerSettings() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["settings", "mcp"], queryFn: readMcpSettings });
  const [busy, setBusy] = useState(false);
  if (query.isLoading) return <p className="text-sm text-faint">Loading…</p>;
  if (query.error || !query.data) return <p role="alert" className="text-sm text-danger">{why(query.error, "Could not read them.")}</p>;
  const settings = query.data;
  const flip = async (source: string, enabled: boolean) => {
    setBusy(true);
    try {
      client.setQueryData(["settings", "mcp"], await setMcpDefault(source, enabled, settings.revision));
    } catch (failure) {
      toast(why(failure, "Could not save it."), "error");
      void client.invalidateQueries({ queryKey: ["settings", "mcp"] });
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="space-y-3" aria-label="Tool servers">
      <h2 className="text-base font-semibold">Tool servers</h2>
      <p className="text-sm text-muted">Whether a conversation uses each MCP server unless it chooses otherwise (its menu › Tool servers).</p>
      <ul className="space-y-2" aria-label="Tool server defaults">
        {settings.sources.map((s) => (
          <li key={s.source} className="flex items-center gap-3 rounded-lg border border-line px-3 py-2 text-sm">
            <span className="min-w-0 flex-1">
              <span className="font-medium">{s.source}</span>
              <span className="ml-2 text-xs text-faint">{s.available ? `${s.tools} tool${s.tools === 1 ? "" : "s"}` : "not connected"}</span>
            </span>
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={s.default} disabled={busy} aria-label={`${s.source} on by default`}
                onChange={(event) => void flip(s.source, event.target.checked)} />
              On by default
            </label>
          </li>
        ))}
      </ul>
      {settings.sources.length === 0 && <p className="text-sm text-faint">No Tool servers are set up (mcp.json).</p>}
    </section>
  );
}

function FolderRow({ folder, onChanged }: { folder: Workspace; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(folder.label ?? "");
  const [note, setNote] = useState(folder.note ?? "");
  const act = async (run: () => Promise<unknown>, done: string) => {
    try {
      await run();
      toast(done);
      setEditing(false);
    } catch (failure) {
      toast(why(failure, "Could not change it."), "error");
    } finally {
      onChanged();
    }
  };
  return (
    <li className="rounded-lg border border-line px-3 py-2 text-sm">
      {editing ? (
        <div className="space-y-2">
          <input aria-label="Label" value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Label" className={`${field} w-full`} />
          <input aria-label="Folder note" value={note} onChange={(event) => setNote(event.target.value)} placeholder="Note for HIMARI" className={`${field} w-full`} />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
            <Button variant="primary" onClick={() => void act(() => editWorkspace(folder.id, label.trim() || null, note.trim() || null), "Saved.")}>Save</Button>
          </div>
        </div>
      ) : (
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <p className="font-medium">{folder.label || folder.path.split("/").pop()}</p>
            <p className="break-all font-mono text-xs text-faint">{folder.path}</p>
            {folder.note && <p className="text-xs text-muted">{folder.note}</p>}
            <p className="text-xs text-faint">{folder.attached ? "attached to a conversation" : "not attached"}{folder.cloud_synced ? " · cloud-synced" : ""}</p>
          </div>
          <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => setEditing(true)}>Edit</Button>
          <Button variant="ghost" className="px-2 py-0.5 text-xs text-danger" disabled={folder.attached}
            title={folder.attached ? "Detach it from its conversations first" : undefined}
            onClick={() => void act(() => unregisterWorkspace(folder.id), "Unregistered (the folder itself stays).")}>Unregister</Button>
        </div>
      )}
    </li>
  );
}

export function FolderSettings() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["settings", "workspaces"], queryFn: readWorkspaces });
  const [path, setPath] = useState("");
  const [label, setLabel] = useState("");
  const refresh = () => void client.invalidateQueries({ queryKey: ["settings", "workspaces"] });
  if (query.isLoading) return <p className="text-sm text-faint">Loading…</p>;
  if (query.error || !query.data) return <p role="alert" className="text-sm text-danger">Could not read the folders.</p>;
  if (!query.data.available) {
    return (
      <section className="space-y-2" aria-label="Folders">
        <h2 className="text-base font-semibold">Folders</h2>
        <p className="text-sm text-faint">Folders on this server are not available here (no machine runner, or not for you).</p>
      </section>
    );
  }
  const register = async () => {
    try {
      await registerWorkspace(path.trim(), label.trim() || null, null);
      toast("Registered.");
      setPath("");
      setLabel("");
    } catch (failure) {
      toast(why(failure, "Could not register it."), "error");
    } finally {
      refresh();
    }
  };
  return (
    <section className="space-y-3" aria-label="Folders">
      <h2 className="text-base font-semibold">Folders</h2>
      <p className="text-sm text-muted">Folders a conversation may attach and HIMARI may read (changes are the hub's agents' work).</p>
      <ul className="space-y-2" aria-label="Registered folders">
        {query.data.items.map((f) => <FolderRow key={f.id} folder={f} onChanged={refresh} />)}
      </ul>
      <form className="flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); void register(); }}>
        <input aria-label="Folder path" placeholder="/home/you/project or ~/project" value={path} onChange={(event) => setPath(event.target.value)}
          className={`${field} min-w-0 flex-1`} />
        <input aria-label="Folder label" placeholder="Label (optional)" value={label} onChange={(event) => setLabel(event.target.value)}
          className={`${field} w-40`} />
        <Button type="submit" variant="primary" disabled={!path.trim()}>Register</Button>
      </form>
    </section>
  );
}
