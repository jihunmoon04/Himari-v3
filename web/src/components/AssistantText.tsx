// The assistant's text through the old page's audited renderer (rich_text.js): it builds DOM nodes itself and
// never parses HTML; React only hands it a container.
import { renderAssistantText } from "@static/rich_text.js";
import { useLayoutEffect, useRef } from "react";
import { cn } from "@/lib/cn";

export function AssistantText({ text, className }: { text: string; className?: string }) {
  const box = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const node = box.current;
    if (!node) return;
    node.replaceChildren(renderAssistantText(document, text));
  }, [text]);
  return <div ref={box} className={cn("answer", className)} />;
}
