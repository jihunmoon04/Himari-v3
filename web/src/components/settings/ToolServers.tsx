// The Tool servers' defaults for new conversations (V3-P53; each conversation may still choose its own).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { readMcpSettings, setMcpDefault } from "@/api/settings";
import { toast } from "@/features/toast";

const why = (failure: unknown, fallback: string) => (failure instanceof ApiError ? failure.message : fallback);

export function ToolServerSettings() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["settings", "mcp"], queryFn: readMcpSettings });
  const [busy, setBusy] = useState(false);
  if (query.isLoading) return <p className="text-sm text-faint">Loading…</p>;
  if (query.error || !query.data) return <p role="alert" className="text-sm text-danger">{why(query.error, "Could not read them.")}</p>;
  const settings = query.data;
  const flip = async (source: string, enabled: boolean) => {
    setBusy(true);
    try {
      client.setQueryData(["settings", "mcp"], await setMcpDefault(source, enabled, settings.revision));
    } catch (failure) {
      toast(why(failure, "Could not save it."), "error");
      void client.invalidateQueries({ queryKey: ["settings", "mcp"] });
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="space-y-3" aria-label="Tool servers">
      <h2 className="text-base font-semibold">Tool servers</h2>
      <p className="text-sm text-muted">Whether a conversation uses each MCP server unless it chooses otherwise (its menu › Tool servers).</p>
      <ul className="space-y-2" aria-label="Tool server defaults">
        {settings.sources.map((s) => (
          <li key={s.source} className="flex items-center gap-3 rounded-lg border border-line px-3 py-2 text-sm">
            <span className="min-w-0 flex-1">
              <span className="font-medium">{s.source}</span>
              <span className="ml-2 text-xs text-faint">{s.available ? `${s.tools} tool${s.tools === 1 ? "" : "s"}` : "not connected"}</span>
            </span>
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={s.default} disabled={busy} aria-label={`${s.source} on by default`}
                onChange={(event) => void flip(s.source, event.target.checked)} />
              On by default
            </label>
          </li>
        ))}
      </ul>
      {settings.sources.length === 0 && <p className="text-sm text-faint">No Tool servers are set up (mcp.json).</p>}
    </section>
  );
}
