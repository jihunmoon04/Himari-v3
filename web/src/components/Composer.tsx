import { ArrowUp, Square } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { cn } from "@/lib/cn";

/** Enter sends, Shift+Enter breaks the line; an Enter that ends an IME composition never sends. The draft stays
 *  in this tab (sessionStorage) until it is sent. */
export function Composer({
  onSend,
  onStop,
  running,
  disabled,
  placeholder,
  draftKey,
  autoFocus,
}: {
  onSend: (text: string) => Promise<unknown> | void;
  onStop?: () => void;
  running?: boolean;
  disabled?: boolean;
  placeholder?: string;
  draftKey: string;
  autoFocus?: boolean;
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
    if (!message || sending || disabled) return;
    setSending(true);
    const sent = text;
    try {
      await onSend(message);
      setText((now) => (now === sent ? "" : now)); // what was typed while it was sending stays
    } catch {
      // the caller shows why; the draft stays
    } finally {
      setSending(false);
      area.current?.focus();
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    void send();
  };

  return (
    <div className="rounded-2xl border border-line bg-panel p-2 shadow-sm focus-within:border-faint">
      <textarea
        ref={area}
        aria-label="Message"
        value={text}
        rows={1}
        autoFocus={autoFocus}
        placeholder={placeholder ?? "Message HIMARI"}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
        className="block max-h-60 w-full resize-none bg-transparent px-2 py-1.5 leading-relaxed outline-none placeholder:text-faint"
      />
      <div className="flex items-center justify-end gap-2 px-1 pt-1">
        {running && onStop ? (
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
            title="Send"
            disabled={!text.trim() || sending || disabled}
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
