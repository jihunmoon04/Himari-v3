// Files in the timeline (V3-P66, V3-P67): a message's attachments and what a Tool gave the user. An image shows the
// copy the server's image sandbox made (its box kept before it loads; the plain file if the copy fails), opened
// larger on click; anything else is a link that downloads it. A memory line says what HIMARI remembered, with Undo
// while the call can still be undone (V3-P71 §7.1). Names and texts are shown as text only.
import { Brain, Download, File as FileIcon, Link2 } from "lucide-react";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { undoMemory } from "@/api/himari";
import type { Shown } from "@/features/timeline";
import { toast } from "@/features/toast";
import { cn } from "@/lib/cn";
import { Dialog } from "./ui/dialog";

const THUMB = 160;

function size(bytes: number | null) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function FileLink({ file }: { file: Shown }) {
  const body = (
    <>
      <FileIcon className="size-3.5 shrink-0" aria-hidden />
      <span className="min-w-0 truncate">{file.name}</span>
      {file.size != null && <span className="shrink-0 text-faint">{size(file.size)}</span>}
      {file.url ? <Download className="size-3.5 shrink-0" aria-hidden /> : <span className="shrink-0 text-faint">expired</span>}
    </>
  );
  const chip = "inline-flex max-w-full items-center gap-1.5 rounded-lg border border-line px-2 py-1 text-xs";
  return file.url
    ? <a href={file.url} download={file.name} className={cn(chip, "hover:bg-raised")} aria-label={`Download ${file.name}`}>{body}</a>
    : <span className={cn(chip, "text-muted")}>{body}</span>;
}

function Thumb({ file }: { file: Shown }) {
  const [open, setOpen] = useState(false);
  const [broken, setBroken] = useState(false);
  if (!file.preview || broken) return <FileLink file={file} />;
  const height = file.height && file.width ? Math.min(THUMB, file.height) : undefined;
  const width = height && file.width && file.height ? Math.max(1, Math.round((file.width * height) / file.height)) : undefined;
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} aria-label={`Preview ${file.name}`}
        className="overflow-hidden rounded-lg border border-line hover:opacity-90">
        <img src={file.preview} alt={file.name} loading="lazy" decoding="async" width={width} height={height}
          onError={() => setBroken(true)} className="block max-h-40 max-w-full object-contain" />
      </button>
      <Dialog open={open} onOpenChange={setOpen} title={file.name}>
        <img src={file.preview} alt={file.name} className="max-h-[70vh] w-full rounded-md object-contain" />
        {file.url && <a href={file.url} download={file.name} className="mt-3 inline-block text-sm underline">Download the original</a>}
      </Dialog>
    </>
  );
}

export function Files({ files, align = "start" }: { files: Shown[]; align?: "start" | "end" }) {
  return (
    <div className={cn("flex flex-wrap gap-2", align === "end" && "justify-end")} aria-label="Files">
      {files.map((file) => <Thumb key={file.key} file={file} />)}
    </div>
  );
}

export function References({ labels }: { labels: string[] }) {
  return (
    <div className="flex flex-wrap justify-end gap-1.5" aria-label="References">
      {labels.map((label) => (
        <span key={label} className="inline-flex items-center gap-1 rounded-md bg-raised px-1.5 py-0.5 font-mono text-xs text-muted">
          <Link2 className="size-3" aria-hidden /> {label}
        </span>
      ))}
    </div>
  );
}

const WORDS: Record<string, string> = {
  saved: "Remembered", updated: "Updated a note", forgot: "Forgot a note", already: "Already remembered",
};

export function MemoryLine({ conversation, run, call, action, text, undo }: {
  conversation: string | null | undefined; run: string; call: string; action: string; text: string; undo: boolean;
}) {
  const [state, setState] = useState<"can" | "busy" | "done" | "gone">(undo ? "can" : "gone");
  const back = async () => {
    if (!conversation) return;
    setState("busy");
    try {
      await undoMemory(conversation, run, call);
      setState("done");
      toast(action === "saved" ? "HIMARI no longer remembers it." : "The note is back as it was.");
    } catch (failure) {
      if (failure instanceof ApiError && (failure.status === 404 || failure.status === 409)) {
        setState("gone"); // it cannot be undone any more
        toast(failure.status === 409 ? "The note was changed since; see Settings › Memory." : failure.message, "info");
      } else {
        setState("can");
        toast(failure instanceof ApiError ? failure.message : "Could not undo it.", "error");
      }
    }
  };
  return (
    <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted" role="note" aria-label="Memory">
      <Brain className="size-3.5" aria-hidden />
      <span className="font-medium">{WORDS[action] ?? action}</span>
      {text && <span className="min-w-0 break-words">“{text}”</span>}
      {state === "can" || state === "busy" ? (
        <button type="button" disabled={state === "busy" || !conversation} onClick={() => void back()}
          className="rounded px-1 text-accent hover:underline disabled:opacity-50">Undo</button>
      ) : state === "done" ? <span className="text-faint">Undone</span> : null}
    </p>
  );
}
