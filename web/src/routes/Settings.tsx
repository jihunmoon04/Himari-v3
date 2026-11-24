// Settings (V3-P102 W6c): one page, a section per kind, chosen on the left (above on a phone).
import { NavLink, useParams } from "react-router";
import { PageHeader } from "@/components/Shell";
import { DevicesSettings } from "@/components/settings/Devices";
import { InstructionsSettings } from "@/components/settings/Instructions";
import { MemorySettings } from "@/components/settings/Memory";
import { PersonaSettings } from "@/components/settings/Personas";
import { ToolServerSettings } from "@/components/settings/ToolServers";
import { cn } from "@/lib/cn";

const SECTIONS = [
  { id: "instructions", label: "Instructions", body: <InstructionsSettings /> },
  { id: "personas", label: "Personas", body: <PersonaSettings /> },
  { id: "memory", label: "Memory", body: <MemorySettings /> },
  { id: "devices", label: "Devices", body: <DevicesSettings /> },
  { id: "tools", label: "Tool servers", body: <ToolServerSettings /> },
] as const;

export function Settings() {
  const section = useParams().section ?? "instructions";
  const shown = SECTIONS.find((s) => s.id === section) ?? SECTIONS[0];
  return (
    <>
      <PageHeader><h1 className="text-sm font-medium">Settings</h1></PageHeader>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-4xl gap-6 px-4 py-6 max-md:flex-col">
          <nav aria-label="Settings sections" className="flex shrink-0 gap-1 md:w-44 md:flex-col max-md:overflow-x-auto">
            {SECTIONS.map((s) => (
              <NavLink key={s.id} to={`/settings/${s.id}`}
                className={cn("whitespace-nowrap rounded-lg px-3 py-1.5 text-sm text-muted hover:bg-raised hover:text-ink",
                  s.id === shown.id && "bg-raised text-ink")}>
                {s.label}
              </NavLink>
            ))}
          </nav>
          <div className="min-w-0 flex-1">{shown.body}</div>
        </div>
      </div>
    </>
  );
}
