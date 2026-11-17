// A HIMARI conversation's folders, to look at (V3-P102 §4.4; the classic page's file panel, V3-P75, V3-P99): its own
// folder first, then the folders attached to it. It only shows: a text file as text, an image as the copy the
// server's image sandbox made, anything else as a download. Names, paths and texts are shown as text only.
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ChevronLeft, File, Folder } from "lucide-react";
import { useState } from "react";
import { ApiError } from "@/api/client";
import {
  browseFolder, conversationFolders, machineDownloadUrl, machineImageUrl, showMachineFile, statMachinePath,
} from "@/api/himari";
import { cn } from "@/lib/cn";

function size(bytes: number | null | undefined) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

const why = (error: unknown) => (error instanceof ApiError ? error.message : "Could not read it.");

/** How a file is shown is the server's call (its ``kind``), never its name's. */
function FileView({ path, onBack }: { path: string; onBack: () => void }) {
  const shown = useQuery({ queryKey: ["machine", "file", path], queryFn: () => showMachineFile(path), retry: false });
  return (
    <div className="space-y-2">
      <button type="button" onClick={onBack} className="inline-flex items-center gap-1 text-xs text-muted hover:text-ink">
        <ChevronLeft className="size-3.5" /> Back
      </button>
      <p className="break-all font-mono text-xs text-muted">{path}</p>
      {shown.isLoading ? (
        <p className="text-faint">Loading…</p>
      ) : shown.data?.kind === "image" ? (
        <img src={machineImageUrl(path)} alt={path.split("/").pop() ?? "image"} className="max-w-full rounded-md border border-line" />
      ) : shown.error ? (
        <p role="alert" className="text-danger">{why(shown.error)}</p>
      ) : shown.data?.kind === "text" && typeof shown.data.text === "string" ? (
        <>
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-words rounded-md bg-code p-2 font-mono text-xs">
            {shown.data.text}
          </pre>
          {shown.data.truncated && <p className="text-xs text-faint">Only the start of the file is shown.</p>}
        </>
      ) : (
        <p className="text-muted">This file is not text.</p>
      )}
      <a href={machineDownloadUrl(path)} className="inline-block text-xs underline">Download</a>
    </div>
  );
}

function FolderView({ path, onOpen }: { path: string; onOpen: (path: string, kind: string) => void }) {
  const pages = useInfiniteQuery({
    queryKey: ["machine", "list", path],
    queryFn: ({ pageParam }) => browseFolder(path, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next ?? undefined,
    retry: false,
  });
  if (pages.isLoading) return <p className="text-faint">Loading…</p>;
  if (pages.error) return <p role="alert" className="text-danger">{why(pages.error)}</p>;
  const listing = { data: pages.data?.pages[0] };
  const entries = pages.data?.pages.flatMap((page) => page.entries) ?? [];
  return (
    <div className="space-y-2">
      {listing.data?.parent && (
        <button type="button" onClick={() => onOpen(listing.data!.parent!, "directory")}
          className="inline-flex items-center gap-1 text-xs text-muted hover:text-ink">
          <ChevronLeft className="size-3.5" /> Up
        </button>
      )}
      <p className="break-all font-mono text-xs text-muted">{listing.data?.path}</p>
      {entries.length === 0 && <p className="text-faint">This folder is empty.</p>}
      <ul className="space-y-0.5" aria-label="Folder">
        {entries.map((entry) => {
          const full = `${(listing.data?.path ?? path).replace(/\/$/, "")}/${entry.name}`;
          const folder = entry.kind === "directory";
          return (
            <li key={entry.name}>
              <button type="button" onClick={() => onOpen(full, entry.kind)}
                className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left hover:bg-raised">
                {folder ? <Folder className="size-4 shrink-0 text-muted" aria-hidden />
                  : <File className="size-4 shrink-0 text-faint" aria-hidden />}
                <span className="min-w-0 flex-1 truncate">{entry.name}</span>
                {!folder && <span className="text-xs text-faint">{size(entry.size)}</span>}
              </button>
            </li>
          );
        })}
      </ul>
      {pages.hasNextPage && (
        <button type="button" disabled={pages.isFetchingNextPage} onClick={() => void pages.fetchNextPage()}
          className="text-xs text-muted underline hover:text-ink">
          {pages.isFetchingNextPage ? "Loading…" : "Show more"}
        </button>
      )}
    </div>
  );
}

export function FilesPanel({ conversation }: { conversation: string }) {
  const folders = useQuery({ queryKey: ["conversation", conversation, "folders"],
    queryFn: () => conversationFolders(conversation), retry: false });
  const [at, setAt] = useState<{ path: string; kind: string } | null>(null);
  if (folders.isLoading) return <p className="text-faint">Loading…</p>;
  if (folders.error) return <p role="alert" className="text-danger">{why(folders.error)}</p>;
  const items = folders.data?.items ?? [];
  if (!items.length) {
    return <p className="text-faint">This conversation has no folders here (it only talks, or the machine is off).</p>;
  }
  const [problem, setProblem] = useState<string | null>(null);
  const open = (path: string, kind: string) => {
    setProblem(null);
    if (kind === "directory" || kind === "file") return setAt({ path, kind });
    // a link (or anything else): where it leads decides how it opens
    statMachinePath(path)
      .then((found) => setAt({ path: found.path, kind: found.kind === "directory" ? "directory" : "file" }))
      .catch((failure: unknown) => setProblem(why(failure)));
  };
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-1" role="group" aria-label="Folders">
        {items.map((item) => (
          <button key={item.binding_id ?? item.path} type="button" onClick={() => open(item.path, "directory")}
            className={cn("rounded-md border border-line px-2 py-0.5 text-xs hover:bg-raised",
              at?.path === item.path && "bg-raised text-ink")}>
            {item.own ? "This conversation" : item.label || item.path.split("/").pop() || item.path}
            {item.working ? " · working" : ""}
          </button>
        ))}
      </div>
      {problem && <p role="alert" className="text-danger">{problem}</p>}
      {at === null ? (
        <FolderView path={items[0]!.path} onOpen={open} />
      ) : at.kind === "directory" ? (
        <FolderView path={at.path} onOpen={open} />
      ) : (
        <FileView path={at.path} onBack={() => setAt({ path: at.path.replace(/\/[^/]*$/, "") || "/", kind: "directory" })} />
      )}
    </div>
  );
}
