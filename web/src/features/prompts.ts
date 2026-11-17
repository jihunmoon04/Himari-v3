// The "/" picker's matching (V3-P102 W6b). Pure, tested in prompts.test.ts.
import type { PromptInfo } from "@/api/himari";

/** The prompts whose name or title holds what follows "/", those whose name starts with it first; at most 8. */
export function matchPrompts(prompts: readonly PromptInfo[], query: string): PromptInfo[] {
  const q = query.toLowerCase();
  const name = (p: PromptInfo) => p.name.toLowerCase();
  const title = (p: PromptInfo) => (p.title ?? "").toLowerCase();
  return prompts
    .filter((p) => !q || name(p).includes(q) || title(p).includes(q))
    .sort((a, b) => Number(!name(a).startsWith(q)) - Number(!name(b).startsWith(q)))
    .slice(0, 8);
}
