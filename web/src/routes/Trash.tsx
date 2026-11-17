// The trash (V3-P65): conversations moved there, most recently first, each restored or deleted for good before the
// server deletes it on its own. A deletion for good says what goes with it (the notes HIMARI saved in it).
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ApiError } from "@/api/client";
import { listMemories, listTrash, purgeConversation, restoreConversation, type TrashedConversation } from "@/api/himari";
import { keys } from "@/app/queries";
import { PageHeader } from "@/components/Shell";
import { Dialog, DialogClose } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { toast } from "@/features/toast";

const name = (item: TrashedConversation) => item.title || item.preview || "Conversation";
const trashKey = ["conversations", "trash"] as const;

function PurgeDialog({ item, onDone }: { item: TrashedConversation | null; onDone: () => void }) {
  const client = useQueryClient();
  const [lines, setLines] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const open = item !== null;
  const prepare = async (target: TrashedConversation) => {
    const out = ["This cannot be undone. Its messages, its records and its files are deleted.",
      "Data stored by external services is not deleted.",
      "Text that other conversations found with search stays in them."];
    try {
      const saved = (await listMemories()).memories.filter((n) => n.source?.conversation === target.id).length;
      if (saved) out.splice(1, 0, saved === 1 ? "1 note HIMARI saved in this conversation is deleted with it."
        : `${saved} notes HIMARI saved in this conversation are deleted with it.`);
    } catch {
      out.push("Notes HIMARI saved in it, if any, are deleted with it.");
    }
    setLines(out);
  };
  useEffect(() => {
    if (item) void prepare(item);
  }, [item]); // prepare reads only what it is given
  const purge = async () => {
    if (!item) return;
    setBusy(true);
    try {
      await purgeConversation(item.id);
      toast(`Deleted “${name(item)}” for good.`);
    } catch (failure) {
      toast(failure instanceof ApiError && failure.code === "offline"
        ? "Could not confirm the deletion; check the trash again." : failure instanceof ApiError ? failure.message
        : "Could not delete it.", "error");
    } finally {
      setBusy(false);
      onDone();
      void client.invalidateQueries({ queryKey: trashKey });
    }
  };
  return (
    <Dialog open={open} onOpenChange={(now) => { if (!now) onDone(); }}
      title={item ? `Delete “${name(item)}” for good?` : "Delete for good?"}>
      <div className="space-y-2 text-sm text-muted">
        {(lines ?? ["Loading…"]).map((line) => <p key={line}>{line}</p>)}
      </div>
      <div className="mt-4 flex justify-end gap-2">
        <DialogClose asChild><Button variant="ghost">Cancel</Button></DialogClose>
        <Button variant="danger" disabled={busy || lines === null} onClick={() => void purge()}>Delete for good</Button>
      </div>
    </Dialog>
  );
}

export function Trash() {
  const client = useQueryClient();
  const trash = useInfiniteQuery({
    queryKey: trashKey,
    queryFn: ({ pageParam }) => listTrash(50, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
  });
  const [purging, setPurging] = useState<TrashedConversation | null>(null);
  const seen = new Set<string>();
  const items = (trash.data?.pages.flatMap((p) => p.items) ?? []).filter((i) => !seen.has(i.id) && seen.add(i.id));
  const restore = async (item: TrashedConversation) => {
    try {
      await restoreConversation(item.id);
      toast(`Restored “${name(item)}”.`);
    } catch (failure) {
      toast(failure instanceof ApiError ? failure.message : "Could not restore it.", "error");
    } finally {
      void client.invalidateQueries({ queryKey: trashKey });
      void client.invalidateQueries({ queryKey: keys.conversations });
    }
  };
  return (
    <>
      <PageHeader><h1 className="text-sm font-medium">Trash</h1></PageHeader>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl px-4 py-6">
          {trash.isLoading && <p className="text-sm text-faint">Loading…</p>}
          {trash.error && <p role="alert" className="text-sm text-danger">Could not read the trash.</p>}
          {trash.data && items.length === 0 && <p className="text-sm text-faint">The trash is empty.</p>}
          <ul className="space-y-2" aria-label="Trashed conversations">
            {items.map((item) => (
              <li key={item.id} className="flex flex-wrap items-center gap-2 rounded-xl border border-line px-4 py-3 text-sm">
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium">{name(item)}</div>
                  <div className="text-xs text-faint">
                    Moved {new Date(item.trashed_at).toLocaleDateString()} · deleted for good on {new Date(item.purge_after).toLocaleDateString()}
                  </div>
                </div>
                <Button onClick={() => void restore(item)}>Restore</Button>
                <Button variant="ghost" className="text-danger" onClick={() => setPurging(item)}>Delete for good</Button>
              </li>
            ))}
          </ul>
          {trash.hasNextPage && (
            <Button variant="ghost" className="mt-3" disabled={trash.isFetchingNextPage} onClick={() => void trash.fetchNextPage()}>
              {trash.isFetchingNextPage ? "Loading…" : "Show more"}
            </Button>
          )}
        </div>
      </div>
      <PurgeDialog key={purging?.id ?? "none"} item={purging} onDone={() => setPurging(null)} />
    </>
  );
}
