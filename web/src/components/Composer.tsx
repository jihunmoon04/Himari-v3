import { ArrowUp, Square } from "lucide-react";
import {
  useEffect, useImperativeHandle, useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent, type ReactNode,
  type Ref,
} from "react";
import { cn } from "@/lib/cn";

/** What a page may do to its composer from outside (a prompt put in, the draft read). */
export interface ComposerHandle {
  setText: (text: string) => void;
  focus: () => void;
}

/** Enter sends, Shift+Enter breaks the line; an Enter that ends an IME composition never sends. The draft stays
 *  in this tab (sessionStorage) until it is sent. ``queues``: a message may be sent while it runs (it waits its
 *  turn), so Stop stands beside Send instead of taking its place. ``tools``: drawn on the left of the buttons. */
export function Composer({
  onSend,
  onStop,
  running,
  disabled,
  placeholder,
  draftKey,
  autoFocus,
  queues,
  tools,
  above,
  onFiles,
  onTextChange,
  onKeyFirst,
  allowEmpty,
  ref,
}: {
  onSend: (text: string) => Promise<unknown> | void;
  onStop?: () => void;
  running?: boolean;
  disabled?: boolean;
  placeholder?: string;
  draftKey: string;
  autoFocus?: boolean;
  queues?: boolean;
  tools?: ReactNode;
  above?: ReactNode; // inside the box, over the text: chips, a picker
  onFiles?: (files: File[]) => void; // files pasted or dropped on it (it takes none without this)
  onTextChange?: (text: string) => void;
  onKeyFirst?: (event: KeyboardEvent<HTMLTextAreaElement>) => boolean; // true: handled (a picker's keys)
  ref?: Ref<ComposerHandle>;
  allowEmpty?: boolean; // the message may go without text (it carries files)
}) {
  const storageKey = `himari.next.draft.${draftKey}`;
  const [text, setText] = useState(() => {
    try {
      return window.sessionStorage.getItem(storageKey) ?? "";
    } catch {
      return "";
    }
  });
  const [sending, setSending] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  const [dragging, setDragging] = useState(false);
  useImperativeHandle(ref, () => ({
    setText: (next: string) => {
      setText(next);
      window.setTimeout(() => {
        const node = area.current;
        if (node) { node.focus(); node.setSelectionRange(next.length, next.length); }
      });
    },
    focus: () => area.current?.focus(),
  }), []);
  useEffect(() => onTextChange?.(text), [onTextChange, text]);

  useEffect(() => {
    try {
      if (text) window.sessionStorage.setItem(storageKey, text);
      else window.sessionStorage.removeItem(storageKey);
    } catch {
      // the draft holds for this page only
    }
  }, [storageKey, text]);

  useEffect(() => {
    const node = area.current;
    if (!node) return;
    node.style.height = "auto"; // CSSOM: allowed by the CSP, unlike a style attribute
    node.style.height = `${Math.min(node.scrollHeight, 240)}px`;
  }, [text]);

  const send = async () => {
    const message = text.trim();
    if ((!message && !allowEmpty) || sending || disabled) return;
    setSending(true);
    const sent = text;
    try {
      await onSend(message);
      // The page may have moved on (the home composer is gone once its conversation opens): the stored draft is
      // cleared here, not by a render that may never come. What was typed while it was sending stays.
      try {
        if (window.sessionStorage.getItem(storageKey) === sent) window.sessionStorage.removeItem(storageKey);
      } catch {
        // storage is off: nothing was kept
      }
      setText((now) => (now === sent ? "" : now));
    } catch {
      // the caller shows why; the draft stays
    } finally {
      setSending(false);
      area.current?.focus();
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (onKeyFirst?.(event)) return;
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    void send();
  };

  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = [...event.clipboardData.files];
    if (!onFiles || !files.length) return; // text pastes as text
    event.preventDefault();
    onFiles(files);
  };
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    setDragging(false);
    if (!onFiles || !event.dataTransfer.files.length) return;
    event.preventDefault();
    onFiles([...event.dataTransfer.files]);
  };

  return (
    <div
      className={cn("rounded-2xl border border-line bg-panel p-2 shadow-sm focus-within:border-faint",
        dragging && "border-accent")}
      onDragOver={(event) => { if (onFiles && event.dataTransfer.types.includes("Files")) { event.preventDefault(); setDragging(true); } }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
    >
      {above}
      <textarea
        ref={area}
        aria-label="Message"
        value={text}
        rows={1}
        autoFocus={autoFocus}
        placeholder={placeholder ?? "Message HIMARI"}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
        onPaste={onPaste}
        className="block max-h-60 w-full resize-none bg-transparent px-2 py-1.5 leading-relaxed outline-none placeholder:text-faint"
      />
      <div className="flex items-center justify-end gap-2 px-1 pt-1">
        {tools && <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">{tools}</div>}
        {running && onStop && queues && (
          <button
            type="button"
            onClick={onStop}
            aria-label="Stop"
            title="Stop the running turn"
            className="inline-flex size-8 items-center justify-center rounded-full border border-line text-ink hover:bg-raised"
          >
            <Square className="size-3.5 fill-current" />
          </button>
        )}
        {running && onStop && !queues ? (
          <button
            type="button"
            onClick={onStop}
            aria-label="Stop"
            title="Stop"
            className="inline-flex size-8 items-center justify-center rounded-full bg-accent text-accent-ink"
          >
            <Square className="size-3.5 fill-current" />
          </button>
        ) : (
          <button
            type="button"
            onClick={() => void send()}
            aria-label="Send"
            title={running && queues ? "Send (it waits for the running turn)" : "Send"}
            disabled={(!text.trim() && !allowEmpty) || sending || disabled}
            className={cn(
              "inline-flex size-8 items-center justify-center rounded-full bg-accent text-accent-ink",
              "disabled:opacity-30",
            )}
          >
            <ArrowUp className="size-4" />
          </button>
        )}
      </div>
    </div>
  );
}
