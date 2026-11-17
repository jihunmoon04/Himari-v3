// A HIMARI conversation's composer (V3-P102 W6b, the classic page's): files attached by the button, pasted or dropped
// (each uploaded as it is added, with its progress; sent with the message once ready), files pointed at from the
// Files panel, and "/" for the prompts of the Tool servers this conversation uses (V3-P60 §4): one with arguments
// asks for them first; either way the prompt comes into the composer as text to review and send.
import { useQuery } from "@tanstack/react-query";
import { Link2, Paperclip, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ApiError } from "@/api/client";
import { expandPrompt, listPrompts, sendMessage, uploadFile, type PromptInfo } from "@/api/himari";
import { matchPrompts } from "@/features/prompts";
import { unpoint, usePointed } from "@/features/references";
import { toast } from "@/features/toast";
import { cn } from "@/lib/cn";
import { Composer, type ComposerHandle } from "./Composer";
import { ModelPicker, modelSettled } from "./ModelPicker";
import { Dialog, DialogClose } from "./ui/dialog";
import { Button, IconButton } from "./ui/button";

interface Upload {
  key: number;
  name: string;
  size: number;
  state: "uploading" | "ready" | "failed";
  progress: number;
  fileId: string | null;
  note: string;
  abort: AbortController;
}

let nextKey = 1;

function PromptForm({ prompt, onDone }: { prompt: PromptInfo | null; onDone: (args: Record<string, string> | null) => void }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const missing = (prompt?.arguments ?? []).some((a) => a.required && !values[a.name]?.trim());
  return (
    <Dialog open={prompt !== null} onOpenChange={(open) => { if (!open) onDone(null); }}
      title={prompt?.title || prompt?.name || "Prompt"} description={prompt?.description ?? undefined}>
      <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); if (!missing) onDone(values); }}>
        {(prompt?.arguments ?? []).map((arg, index) => (
          <label key={arg.name} className="block text-sm">
            <span className="text-xs font-medium text-muted">{arg.name}{arg.required ? " *" : ""}</span>
            {arg.description && <span className="block text-xs text-faint">{arg.description}</span>}
            <input autoFocus={index === 0} value={values[arg.name] ?? ""} aria-label={arg.name}
              onChange={(event) => setValues({ ...values, [arg.name]: event.target.value })}
              className="mt-1 w-full rounded-md border border-line bg-bg px-2 py-1.5 outline-none focus:border-faint" />
          </label>
        ))}
        <div className="flex justify-end gap-2">
          <DialogClose asChild><Button variant="ghost">Cancel</Button></DialogClose>
          <Button type="submit" variant="primary" disabled={missing}>Insert</Button>
        </div>
      </form>
    </Dialog>
  );
}

export function HimariComposer({ conversation, running, waiting, onStop, onSent, onError }: {
  conversation: string;
  running: boolean;
  waiting: boolean;
  onStop: () => void;
  onSent: () => Promise<unknown> | void;
  onError: (message: string | null) => void;
}) {
  const composer = useRef<ComposerHandle>(null);
  const input = useRef<HTMLInputElement>(null);
  const [uploads, setUploads] = useState<Upload[]>([]);
  const live = useRef(new Set<AbortController>()); // uploads under way, aborted directly when the composer goes
  const pointed = usePointed(conversation);
  const [text, setText] = useState("");
  const latest = useRef("");
  latest.current = text;
  const [highlight, setHighlight] = useState(0);
  const [asking, setAsking] = useState<PromptInfo | null>(null);
  const [dismissed, setDismissed] = useState<string | null>(null); // Esc closed the picker over this draft
  const slash = text !== dismissed ? /^\/(\S*)$/.exec(text) : null;
  const prompts = useQuery({ queryKey: ["conversation", conversation, "prompts"], queryFn: () => listPrompts(conversation),
    enabled: slash !== null, staleTime: 60_000 });
  const offered = slash ? matchPrompts(prompts.data?.prompts ?? [], slash[1] ?? "") : [];
  useEffect(() => setHighlight(0), [slash?.[1]]);
  // leaving the conversation stops what was still uploading for it
  useEffect(() => {
    const running = live.current;
    return () => {
      for (const controller of running) controller.abort();
      running.clear();
    };
  }, [conversation]);

  const change = (key: number, patch: Partial<Upload>) =>
    setUploads((now) => now.map((u) => (u.key === key ? { ...u, ...patch } : u)));
  const add = useCallback((files: File[]) => {
    for (const file of files) {
      const entry: Upload = { key: nextKey++, name: file.name || "pasted", size: file.size, state: "uploading", progress: 0,
        fileId: null, note: "", abort: new AbortController() };
      setUploads((now) => [...now, entry]);
      live.current.add(entry.abort);
      uploadFile(conversation, file, (share) => change(entry.key, { progress: share }), entry.abort.signal)
        .then((done) => change(entry.key, { state: "ready", fileId: done.file_id, size: done.size, progress: 1 }))
        .catch((failure: unknown) => {
          if (failure instanceof DOMException && failure.name === "AbortError") return;
          change(entry.key, { state: "failed", note: failure instanceof Error ? failure.message : "The upload failed." });
        })
        .finally(() => live.current.delete(entry.abort));
    }
  }, [conversation]);
  const drop = (key: number) => setUploads((now) => {
    now.find((u) => u.key === key)?.abort.abort();
    return now.filter((u) => u.key !== key);
  });

  /** ``args`` undefined: not asked yet (a prompt with arguments asks for them first). */
  const choose = async (prompt: PromptInfo, args?: Record<string, string>) => {
    if (args === undefined && prompt.arguments.length) return setAsking(prompt);
    const from = latest.current;
    try {
      const { text: expanded } = await expandPrompt(conversation, prompt.source, prompt.name, args ?? {});
      if (latest.current !== from) { // written on meanwhile: that stays
        toast("The draft changed while the prompt was prepared; it was not put in.", "info");
        return;
      }
      composer.current?.setText(expanded);
    } catch (failure) {
      toast(failure instanceof ApiError ? failure.message : "Could not use that prompt.", "error");
    }
  };
  const onKeyFirst = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!offered.length || event.nativeEvent.isComposing) return false;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setHighlight((h) => (h + (event.key === "ArrowDown" ? 1 : offered.length - 1)) % offered.length);
      return true;
    }
    if ((event.key === "Enter" && !event.shiftKey) || event.key === "Tab") {
      event.preventDefault();
      void choose(offered[highlight] ?? offered[0]!);
      return true;
    }
    if (event.key === "Escape") { // the picker goes, the draft stays (it may be meant as it is)
      event.preventDefault();
      setDismissed(text);
      return true;
    }
    return false;
  };

  const uploading = uploads.some((u) => u.state === "uploading");
  const send = async (message: string) => {
    onError(null);
    const ready = uploads.filter((u) => u.state === "ready" && u.fileId);
    const refs = pointed;
    try {
      await modelSettled(conversation);
      await sendMessage(conversation, message, ready.map((u) => u.fileId!),
        refs.map((r) => ({ path: r.path, identity: r.identity })));
      setUploads((now) => now.filter((u) => !ready.includes(u)));
      unpoint(conversation, refs.map((r) => r.path));
    } catch (failure) {
      onError(failure instanceof ApiError ? failure.message : "Something went wrong.");
      throw failure; // the draft stays
    } finally {
      await onSent();
    }
  };

  const chips = (uploads.length > 0 || pointed.length > 0) && (
    <div className="mb-1.5 flex flex-wrap gap-1.5 px-1" aria-label="To send with the message">
      {uploads.map((u) => (
        <span key={u.key} className={cn("inline-flex max-w-full items-center gap-1.5 rounded-lg border px-2 py-1 text-xs",
          u.state === "failed" ? "border-danger/40 text-danger" : "border-line")} title={u.note || u.name}>
          <Paperclip className="size-3" aria-hidden />
          <span className="min-w-0 truncate">{u.name}</span>
          <span className="shrink-0 text-faint">
            {u.state === "uploading" ? `${Math.round(u.progress * 100)}%` : u.state === "failed" ? `failed: ${u.note}` : "ready"}
          </span>
          <button type="button" aria-label={`Remove ${u.name}`} onClick={() => drop(u.key)} className="text-faint hover:text-ink">
            <X className="size-3" />
          </button>
        </span>
      ))}
      {pointed.map((r) => (
        <span key={r.path} className="inline-flex max-w-60 items-center gap-1.5 rounded-lg bg-raised px-2 py-1 font-mono text-xs" title={r.path}>
          <Link2 className="size-3" aria-hidden />
          <span className="min-w-0 truncate">{r.name}</span>
          <button type="button" aria-label={`Remove ${r.name}`} onClick={() => unpoint(conversation, [r.path])}
            className="text-faint hover:text-ink"><X className="size-3" /></button>
        </span>
      ))}
    </div>
  );
  const picker = slash && (
    <div className="mb-1.5 rounded-lg border border-line bg-bg p-1 text-sm" role="listbox" aria-label="Prompts">
      {prompts.isLoading && <p className="px-2 py-1 text-xs text-faint">Loading prompts…</p>}
      {!prompts.isLoading && offered.length === 0 && <p className="px-2 py-1 text-xs text-faint">No prompts match.</p>}
      {offered.map((p, index) => (
        <button key={`${p.source}/${p.name}`} type="button" role="option" aria-selected={index === highlight}
          onMouseDown={(event) => event.preventDefault() /* the composer keeps its focus */}
          onClick={() => void choose(p)}
          className={cn("flex w-full items-baseline gap-2 rounded-md px-2 py-1 text-left", index === highlight && "bg-raised")}>
          <span className="font-mono">/{p.name}</span>
          <span className="min-w-0 flex-1 truncate text-xs text-faint">{p.title || p.description}</span>
          <span className="shrink-0 text-xs text-faint">{p.source}</span>
        </button>
      ))}
    </div>
  );

  return (
    <>
      <Composer
        ref={composer}
        key={conversation}
        draftKey={`c:${conversation}`}
        autoFocus
        running={running}
        disabled={uploading}
        allowEmpty={uploads.some((u) => u.state === "ready") || pointed.length > 0}
        placeholder={waiting ? "Answer HIMARI" : "Message HIMARI (/ for prompts)"}
        onStop={onStop}
        onSend={send}
        onFiles={add}
        onTextChange={setText}
        onKeyFirst={onKeyFirst}
        above={<>{picker}{chips}</>}
        tools={
          <>
            <IconButton label="Attach files" onClick={() => input.current?.click()}>
              <Paperclip className="size-4" />
            </IconButton>
            <input ref={input} type="file" multiple hidden aria-label="Files to attach"
              onChange={(event) => { add([...(event.target.files ?? [])]); event.target.value = ""; }} />
            <ModelPicker conversation={conversation} />
          </>
        }
      />
      <PromptForm key={asking ? `${asking.source}/${asking.name}` : "none"} prompt={asking}
        onDone={(args) => { const prompt = asking; setAsking(null); if (prompt && args) void choose(prompt, args); }} />
    </>
  );
}
