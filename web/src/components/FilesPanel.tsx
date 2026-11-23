// A HIMARI conversation's own files (V3-P66): what was uploaded to it and what its Tools gave the user,
// as the server lists them. An image shows the copy the server made; anything else downloads.
import { useInfiniteQuery } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { listFiles } from "@/api/himari";
import { shownFile } from "@/features/himari/items";
import { Files } from "./TimelineFiles";

export function FilesPanel({ conversation }: { conversation: string }) {
  const pages = useInfiniteQuery({
    queryKey: ["conversation", conversation, "files"],
    queryFn: ({ pageParam }) => listFiles(conversation, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    retry: false,
  });
  if (pages.isLoading) return <p className="text-faint">Loading…</p>;
  if (pages.error) {
    return <p role="alert" className="text-danger">{pages.error instanceof ApiError ? pages.error.message : "Could not read them."}</p>;
  }
  const files = pages.data?.pages.flatMap((page) => page.items) ?? [];
  if (!files.length) return <p className="text-faint">No files in this conversation yet.</p>;
  return (
    <div className="space-y-3">
      <Files files={files.map((file) => shownFile(file, conversation))} />
      {pages.hasNextPage && (
        <button type="button" disabled={pages.isFetchingNextPage} onClick={() => void pages.fetchNextPage()}
          className="text-xs text-muted underline hover:text-ink">
          {pages.isFetchingNextPage ? "Loading…" : "Show more"}
        </button>
      )}
    </div>
  );
}
