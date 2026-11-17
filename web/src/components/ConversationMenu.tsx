// A HIMARI conversation's own tools (V3-P102 W6a, the classic page's conversation menu): rename it, choose which Tool
// servers it uses, summarize its older history now, close what was left waiting, move it to the trash (Undo from
// the notice). What the trash confirmation says comes from the server's delete notice (V3-P65 §6.1).
import { useQueryClient } from "@tanstack/react-query";
import { Archive, Ellipsis, PencilLine, Shrink, Wrench, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { ApiError } from "@/api/client";
import {
  compactConversation, dismissWaiting, readDeleteNotice, readToolSources, renameConversation, restoreConversation,
  setToolSource, trashConversation, type DeleteNotice, type ToolSource,
} from "@/api/himari";
import { keys } from "@/app/queries";
import { toast } from "@/features/toast";
import { Dialog, DialogClose } from "./ui/dialog";
import { Button, IconButton } from "./ui/button";
import { Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger } from "./ui/menu";

const message = (failure: unknown, fallback: string) => (failure instanceof ApiError ? failure.message : fallback);

export function deleteLines(notice: DeleteNotice): string[] {
  const lines = [`It moves to the trash and is deleted for good after ${notice.trash_days} days. Until then you can `
    + "restore it from the trash."];
  if (notice.running) lines.push("The work under way is stopped first.");
  if (notice.memory_count) {
    lines.push(notice.memory_count === 1 ? "1 note HIMARI saved in this conversation is deleted when it is deleted for good."
      : `${notice.memory_count} notes HIMARI saved in this conversation are deleted when it is deleted for good.`);
  }
  if (notice.episode_count) {
    lines.push(notice.episode_count === 1
      ? "1 timeline entry HIMARI kept from this conversation is deleted when it is deleted for good."
      : `${notice.episode_count} timeline entries HIMARI kept from this conversation are deleted when it is deleted for good.`);
  }
  lines.push(notice.servers.length
    ? `Data stored by the external servers this conversation used (${notice.servers.map((s) =>
      notice.unconfigured.includes(s) ? `${s} (no longer configured)` : s).join(", ")}) is not deleted.`
    : "Data stored by external services is not deleted.");
  return lines;
}

function RenameDialog({ id, title, open, onOpenChange }: {
  id: string; title: string; open: boolean; onOpenChange: (open: boolean) => void;
}) {
  const client = useQueryClient();
  const [name, setName] = useState(title);
  const [problem, setProblem] = useState<string | null>(null);
  const save = async () => {
    setProblem(null);
    try {
      await renameConversation(id, name.trim() || null);
      await Promise.all([client.invalidateQueries({ queryKey: keys.conversations }),
        client.invalidateQueries({ queryKey: keys.detail(id) })]);
      onOpenChange(false);
    } catch (failure) {
      setProblem(message(failure, "Could not rename it."));
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="Rename conversation" description="Leave it empty to name it after its start.">
      <form onSubmit={(event) => { event.preventDefault(); void save(); }} className="space-y-3">
        <input aria-label="Name" autoFocus value={name} maxLength={200} onChange={(event) => setName(event.target.value)}
          className="w-full rounded-md border border-line bg-bg px-2 py-1.5 text-sm outline-none focus:border-faint" />
        {problem && <p role="alert" className="text-sm text-danger">{problem}</p>}
        <div className="flex justify-end gap-2">
          <DialogClose asChild><Button variant="ghost">Cancel</Button></DialogClose>
          <Button type="submit" variant="primary">Save</Button>
        </div>
      </form>
    </Dialog>
  );
}

function ToolsDialog({ id, open, onOpenChange }: { id: string; open: boolean; onOpenChange: (open: boolean) => void }) {
  const [sources, setSources] = useState<ToolSource[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); // one change at a time: each answer is the whole list as saved
  const load = () => {
    setProblem(null);
    readToolSources(id).then((answer) => setSources(answer.sources))
      .catch((failure: unknown) => setProblem(message(failure, "Could not read the Tool servers.")));
  };
  useEffect(() => {
    if (open) load();
  }, [open]); // load reads only what it is given
  const change = async (source: string, enabled: boolean | null) => {
    setProblem(null);
    setBusy(true);
    try {
      setSources((await setToolSource(id, source, enabled)).sources);
    } catch (failure) {
      setProblem(message(failure, "Could not change it."));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="Tool servers"
      description="Which MCP servers this conversation may use, from its next turn.">
      {sources === null && !problem && <p className="text-sm text-faint">Loading…</p>}
      {sources && sources.length === 0 && <p className="text-sm text-faint">No Tool servers are set up.</p>}
      <ul className="space-y-2" aria-label="Tool servers of this conversation">
        {(sources ?? []).map((source) => (
          <li key={source.source} className="rounded-lg border border-line px-3 py-2 text-sm">
            <div className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate font-medium">{source.source}</span>
              <select aria-label={`Use ${source.source}`} disabled={busy} value={source.choice === null ? "default" : source.choice ? "on" : "off"}
                onChange={(event) => void change(source.source, event.target.value === "default" ? null : event.target.value === "on")}
                className="rounded-md border border-line bg-transparent px-1.5 py-1 text-xs">
                <option value="default">Default ({source.default ? "on" : "off"})</option>
                <option value="on">On</option>
                <option value="off">Off</option>
              </select>
            </div>
            <p className="mt-1 text-xs text-faint">
              {source.available ? `${source.tools.length} tool${source.tools.length === 1 ? "" : "s"}` : "Not connected"}
              {source.enabled ? " · in use" : " · not used"}
            </p>
            {source.problems.map((p) => <p key={p} className="mt-1 text-xs text-danger">{p}</p>)}
          </li>
        ))}
      </ul>
      {problem && <p role="alert" className="mt-2 text-sm text-danger">{problem}</p>}
      <div className="mt-4 flex justify-end"><DialogClose asChild><Button>Done</Button></DialogClose></div>
    </Dialog>
  );
}

function TrashDialog({ id, name, notice, open, onOpenChange }: {
  id: string; name: string; notice: DeleteNotice | null; open: boolean; onOpenChange: (open: boolean) => void;
}) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const move = async () => {
    setBusy(true);
    try {
      const moved = await trashConversation(id);
      onOpenChange(false);
      // home only from the conversation that went (stopping its work may take a while; the reader may have moved on)
      if (window.location.pathname.endsWith(`/c/${encodeURIComponent(id)}`)) navigate("/");
      await client.invalidateQueries({ queryKey: keys.conversations });
      toast(`Moved “${name}” to the trash. It is deleted for good on ${new Date(moved.purge_after).toLocaleDateString()}.`, "ok", {
        label: "Undo",
        run: () => void restoreConversation(id).then(async () => {
          await client.invalidateQueries({ queryKey: keys.conversations });
          toast(`Restored “${name}”.`);
          navigate(`/c/${encodeURIComponent(id)}`);
        }).catch((failure: unknown) => toast(message(failure, "Could not restore it."), "error")),
      });
    } catch (failure) {
      toast(failure instanceof ApiError && failure.code === "offline"
        ? "Could not confirm the move; check the trash before trying again." : message(failure, "Could not move it."), "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={`Delete “${name}”?`}>
      <div className="space-y-2 text-sm text-muted">
        {notice ? deleteLines(notice).map((line) => <p key={line}>{line}</p>) : <p>Loading…</p>}
      </div>
      <div className="mt-4 flex justify-end gap-2">
        <DialogClose asChild><Button variant="ghost">Cancel</Button></DialogClose>
        <Button variant="danger" disabled={!notice || busy} onClick={() => void move()}>Move to trash</Button>
      </div>
    </Dialog>
  );
}

export function ConversationMenu({ id, title, waiting, running }: {
  id: string; title: string; waiting: boolean; running: boolean;
}) {
  const client = useQueryClient();
  const [open, setOpen] = useState<"rename" | "tools" | "trash" | null>(null);
  const [notice, setNotice] = useState<DeleteNotice | null>(null);
  const refresh = () => void client.invalidateQueries({ queryKey: keys.detail(id) });
  const askTrash = () => {
    setNotice(null);
    setOpen("trash");
    readDeleteNotice(id).then(setNotice).catch((failure: unknown) => {
      setOpen(null);
      toast(message(failure, "Could not prepare the deletion."), "error");
    });
  };
  const compact = () => compactConversation(id)
    .then(() => { toast("Summarizing the older history…", "info"); refresh(); })
    .catch((failure: unknown) => toast(message(failure, "Could not summarize it now."), "error"));
  const dismiss = () => dismissWaiting(id)
    .then(refresh)
    .catch((failure: unknown) => toast(message(failure, "Could not close it."), "error"));
  return (
    <>
      <Menu>
        <MenuTrigger asChild>
          <IconButton label="Conversation menu"><Ellipsis className="size-4" /></IconButton>
        </MenuTrigger>
        <MenuContent align="end">
          <MenuItem onSelect={() => setOpen("rename")}><PencilLine className="size-3.5" /> Rename…</MenuItem>
          <MenuItem onSelect={() => setOpen("tools")}><Wrench className="size-3.5" /> Tool servers…</MenuItem>
          <MenuItem disabled={running} onSelect={() => void compact()}><Shrink className="size-3.5" /> Summarize older history</MenuItem>
          {waiting && <MenuItem disabled={running} onSelect={() => void dismiss()}><XCircle className="size-3.5" /> Dismiss the question</MenuItem>}
          <MenuSeparator />
          <MenuItem className="text-danger" onSelect={askTrash}><Archive className="size-3.5" /> Move to trash…</MenuItem>
        </MenuContent>
      </Menu>
      <RenameDialog key={`${id}:${open === "rename"}`} id={id} title={title} open={open === "rename"}
        onOpenChange={(now) => setOpen(now ? "rename" : null)} />
      <ToolsDialog id={id} open={open === "tools"} onOpenChange={(now) => setOpen(now ? "tools" : null)} />
      <TrashDialog id={id} name={title || "this conversation"} notice={notice} open={open === "trash"}
        onOpenChange={(now) => setOpen(now ? "trash" : null)} />
    </>
  );
}
