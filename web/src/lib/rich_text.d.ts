// The old page's audited renderer (src/himari/interfaces/web/static/rich_text.js), imported as it is.
declare module "@/lib/rich_text.js" {
  export const SAFE_LINK_PROTOCOLS: readonly string[];
  export function renderAssistantText(doc: Document, text: string, options?: Record<string, unknown>): Node;
  export function assistantTextLine(doc: Document, text: string): string;
  export function safeLinkHref(raw: string): string | null;
}
