// The model, effort and permission mode one message may choose (V3-P102 §4.3): only what the hub's ``projects``
// offers for that agent (the operator's allow lists and modes). A choice left on "default" is not sent, so the
// hub's own rule applies (the session's last value, then the profile's; the usual mode).
import type { HubProjects, MessageChoices } from "@/api/hub";
import { cn } from "@/lib/cn";

export function choicesFor(projects: HubProjects | undefined, agent: string | undefined) {
  const name = agent ?? "";
  return {
    models: projects?.models?.[name] ?? [],
    efforts: projects?.efforts?.[name] ?? [],
    modes: projects?.agents?.find((a) => a.name === name)?.modes ?? [],
  };
}

/** Only the choices that were made (an empty one is never sent: the server refuses empty names). */
export function chosen(value: MessageChoices): MessageChoices {
  const out: MessageChoices = {};
  if (value.model) out.model = value.model;
  if (value.effort) out.effort = value.effort;
  if (value.mode) out.mode = value.mode;
  return out;
}

function Pick({ label, value, options, onChange, usual }: {
  label: string;
  value: string | undefined;
  options: string[];
  onChange: (value: string) => void;
  usual: string;
}) {
  return (
    <select
      aria-label={label}
      title={label}
      value={value ?? ""}
      onChange={(event) => onChange(event.target.value)}
      className={cn("max-w-40 truncate rounded-md border border-line bg-transparent px-1.5 py-1 text-xs text-muted",
        "outline-none hover:text-ink focus:border-faint", value && "text-ink")}
    >
      <option value="">{usual}</option>
      {options.map((option) => <option key={option} value={option}>{option}</option>)}
    </select>
  );
}

export function MessageOptions({ projects, agent, value, onChange }: {
  projects: HubProjects | undefined;
  agent: string | undefined;
  value: MessageChoices;
  onChange: (value: MessageChoices) => void;
}) {
  const { models, efforts, modes } = choicesFor(projects, agent);
  // a mode list of one (the usual) is no choice at all
  const modeChoices = modes.length > 1 ? modes : [];
  if (!models.length && !efforts.length && !modeChoices.length) return null;
  return (
    <>
      {models.length > 0 && (
        <Pick label="Model" value={value.model} options={models} usual="Model: default"
          onChange={(model) => onChange({ ...value, model })} />
      )}
      {efforts.length > 0 && (
        <Pick label="Effort" value={value.effort} options={efforts} usual="Effort: default"
          onChange={(effort) => onChange({ ...value, effort })} />
      )}
      {modeChoices.length > 0 && (
        <Pick label="Mode (this message only)" value={value.mode} options={modeChoices} usual="Mode: usual"
          onChange={(mode) => onChange({ ...value, mode })} />
      )}
    </>
  );
}
