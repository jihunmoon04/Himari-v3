import { useQueryClient } from "@tanstack/react-query";
import { ChevronRight, FolderClock, FolderGit2, Inbox, Info, LayoutGrid, LogOut, Monitor, Moon, PanelLeftClose, Plus, Settings, SquarePen, Sun } from "lucide-react";
import { useState } from "react";
import { NavLink, useMatch, useNavigate } from "react-router";
import { signOut } from "@/api/himari";
import { wipeDrafts } from "@/lib/drafts";
import { clearProjectsDoubt, keys, useActivity, useConversations, useEvents, useHubPending, useHubProjects, useHubTask, useServer } from "@/app/queries";
import { toItems } from "@/features/himari/items";
import type { Delegation } from "@/features/timeline";
import { byProject, useHubList } from "@/features/hub/list";
import { openProjectDialog } from "@/features/hub/dialog";
import { heldWithoutProject } from "@/features/hub/project_view";
import { openRequests, requests, useHubRequests } from "@/features/hub/requests";
import { unconfirmed } from "@/features/hub/unconfirmed";
import { AdoptButton } from "./HeldFolders";
import { StatusDot, activityOf, sessionName } from "./SessionBadge";
import { setLook, setMode, useAppearance, type Look, type Mode } from "@/lib/appearance";
import { cn } from "@/lib/cn";
import { IconButton } from "./ui/button";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuRadioGroup, MenuRadioItem, MenuSeparator, MenuTrigger } from "./ui/menu";

function Section({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="mt-4">
      <div className="flex items-center pr-1">
        <h2 className="flex-1 px-3 pb-1 text-xs font-medium text-faint">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

/** One task the open conversation handed to the hub, as a link to the session it runs in (once it has one). */
function DelegatedRow({ delegation }: { delegation: Delegation }) {
  const hub = useHubProjects();
  const task = useHubTask(delegation.taskId, hub.status === "on").data?.task;
  const session = task ? task.session ?? null : delegation.session; // the task's own answer once read
  const label = `${delegation.project} · ${task?.agent ?? delegation.agent ?? "agent"}`;
  const tone = task?.status === "running" || task?.status === "queued" ? "run" : task?.status === "waiting" ? "wait"
    : "idle";
  if (session == null) return <li className="px-3 py-1 text-xs text-faint">{label} · starting</li>;
  return (
    <li>
      <NavLink to={`/s/${session}`} className={({ isActive }) => cn(
        "flex items-center gap-2 rounded-lg px-3 py-1 text-xs text-muted hover:bg-raised hover:text-ink",
        isActive && "bg-raised text-ink")}>
        <StatusDot tone={tone} />
        <span className="min-w-0 flex-1 truncate">{label}</span>
      </NavLink>
    </li>
  );
}

/** Under the open conversation: the sessions its work was handed to (V3-P102 §5.1). Read from that conversation's
 *  events, which its page has already read. */
function DelegatedSessions({ conversation }: { conversation: string }) {
  const events = useEvents(conversation);
  const hubSource = useServer(true).data?.hub_source ?? null;
  if (!hubSource) return null;
  const handed = toItems(events.data?.items ?? [], hubSource)
    .flatMap((item) => (item.kind === "delegation" && item.delegation.taskId !== null ? [item] : []));
  if (!handed.length) return null;
  return (
    <ul className="ml-4 space-y-0.5 border-l border-line pl-1" aria-label="Handed to the hub">
      {handed.map((item) => <DelegatedRow key={item.key} delegation={item.delegation} />)}
    </ul>
  );
}

function ConversationList() {
  const open = useMatch("/c/:id")?.params.id ?? null;
  const { data, isLoading, error, hasNextPage, fetchNextPage, isFetching, isFetchingNextPage } = useConversations();
  const activity = useActivity().data;
  // Offset pages can shift while new conversations arrive: one row per conversation, and the next page is read
  // only while no other read of the list runs (so it never lands on a list that changed under it).
  const seen = new Set<string>();
  const items = (data?.pages.flatMap((page) => page.items) ?? []).filter((item) => !seen.has(item.id) && seen.add(item.id));
  if (isLoading) return <p className="px-3 py-1 text-sm text-faint">Loading…</p>;
  if (error) return <p className="px-3 py-1 text-sm text-danger">Could not load the conversations.</p>;
  if (!items.length) return <p className="px-3 py-1 text-sm text-faint">No conversations yet.</p>;
  return (
    <>
    <ul className="space-y-0.5" aria-label="Conversations">
      {items.map((item) => {
        const working = activity?.working.includes(item.id);
        const waiting = activity?.waiting.includes(item.id);
        return (
          <li key={item.id}>
            <NavLink
              to={`/c/${encodeURIComponent(item.id)}`}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm text-muted hover:bg-raised hover:text-ink",
                  isActive && "bg-raised text-ink",
                )
              }
            >
              <span className="min-w-0 flex-1 truncate">{item.title || item.preview || "New conversation"}</span>
              {working && <span className="pulse size-1.5 shrink-0 rounded-full bg-accent" aria-label="Working" />}
              {!working && waiting && <span className="size-1.5 shrink-0 rounded-full bg-wait" aria-label="Waiting for you" />}
            </NavLink>
            {open === item.id && <DelegatedSessions conversation={item.id} />}
          </li>
        );
      })}
    </ul>
    {hasNextPage && (
      <button
        type="button"
        disabled={isFetching}
        onClick={() => void fetchNextPage({ cancelRefetch: false })}
        className="mt-1 w-full rounded-lg px-3 py-1.5 text-left text-sm text-faint hover:bg-raised hover:text-ink"
      >
        {isFetchingNextPage ? "Loading…" : "Show more"}
      </button>
    )}
    </>
  );
}

const rowClass = ({ isActive }: { isActive: boolean }) =>
  cn("flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm text-muted hover:bg-raised hover:text-ink",
    isActive && "bg-raised text-ink");

function Project({ name, sessions, waits }: {
  name: string;
  sessions: ReturnType<typeof byProject>[number][1];
  waits: Map<number, number>;
}) {
  const [open, setOpen] = useState(true);
  return (
    <li>
      <div className="group flex items-center rounded-lg hover:bg-raised">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-1.5 px-3 py-1.5 text-left text-sm text-ink"
        >
          <ChevronRight className={cn("size-3.5 shrink-0 text-faint transition-transform", open && "rotate-90")} />
          <FolderGit2 className="size-4 shrink-0 text-muted" />
          <span className="min-w-0 flex-1 truncate">{name}</span>
        </button>
        {/* V3-P103 §6.2: the project's own page; its name is a query value */}
        <NavLink to={`/project?name=${encodeURIComponent(name)}`} aria-label={`About ${name}`} title="Project"
          className="mr-1 rounded p-1 text-faint hover:text-ink">
          <Info className="size-3.5" />
        </NavLink>
      </div>
      {open && (
        <ul className="ml-4 space-y-0.5 border-l border-line pl-1" aria-label={`Sessions of ${name}`}>
          {sessions.length === 0 && <li className="px-3 py-1 text-xs text-faint">No open sessions</li>}
          {sessions.map((session) => {
            const count = session.task != null ? waits.get(session.task) ?? 0 : 0;
            const { label, tone } = activityOf(session, count);
            return (
              <li key={session.id}>
                <NavLink to={`/s/${session.id}`} className={rowClass} title={label}>
                  <StatusDot tone={tone} />
                  <span className="min-w-0 flex-1 truncate">{sessionName(session)}</span>
                  {count > 0 && <span className="rounded-full bg-wait px-1.5 text-xs text-white">{count}</span>}
                </NavLink>
              </li>
            );
          })}
        </ul>
      )}
    </li>
  );
}

/** The hub's projects and their open sessions (V3-P102 §4.1); nothing when the hub is not set up here. */
function HubSection() {
  const hub = useHubProjects();
  const list = useHubList(hub.status === "on");
  const pending = useHubPending(hub.status === "on");
  const asked = openRequests(useHubRequests()).filter((r) => r.state === "waiting_user").length;
  if (hub.status === "off" || hub.status === "loading") return null;
  const waits = new Map<number, number>();
  for (const p of pending.data?.pending ?? []) waits.set(p.task_id, (waits.get(p.task_id) ?? 0) + 1);
  const names = (hub.data?.projects ?? []).map((p) => p.name).filter((n): n is string => Boolean(n));
  const groups = byProject(list.sessions.filter((s) => s.state !== "closed"), names);
  const held = heldWithoutProject(hub.data);
  const waiting = (pending.data?.pending.length ?? 0) + asked;
  return (
    <Section title="Projects" action={hub.status === "on" && (
      <IconButton label="New project" className="size-6" onClick={() => openProjectDialog({ kind: "create" })}>
        <Plus className="size-3.5" />
      </IconButton>
    )}>
      <NavLink to="/approvals" className={rowClass}>
        <Inbox className="size-4" /> <span className="flex-1">Approvals</span>
        {waiting > 0 && (
          <span className="rounded-full bg-wait px-1.5 text-xs text-white" aria-label={`${waiting} waiting`}>
            {waiting}
          </span>
        )}
      </NavLink>
      <NavLink to="/agents" className={rowClass}>
        <LayoutGrid className="size-4" /> Work overview
      </NavLink>
      {(hub.status === "down" || !list.reachable) && (
        <p className="px-3 py-1 text-xs text-danger">The hub is not answering.</p>
      )}
      <ul className="mt-1 space-y-0.5" aria-label="Projects">
        {groups.map(([name, sessions]) => <Project key={name} name={name} sessions={sessions} waits={waits} />)}
      </ul>
      {held.length > 0 && ( // V3-P103 §6.2: names held on a device that no project has yet
        <div className="mt-2">
          <h3 className="flex items-center gap-1.5 px-3 pb-1 text-xs text-faint">
            <FolderClock className="size-3.5" /> Held folders
          </h3>
          <ul className="space-y-0.5" aria-label="Held folders">
            {held.map((h) => (
              <li key={JSON.stringify([h.project, h.runner])}className="flex items-center gap-2 px-3 py-1 text-xs">
                <span className="min-w-0 flex-1 truncate" title={`${h.project} on ${h.runner}`}>
                  {h.project} <span className="text-faint">· {h.runner}{h.connected ? "" : " (not connected)"}</span>
                </span>
                <AdoptButton project={h.project} runner={h.runner} connected={h.connected} className="px-2 py-0.5 text-xs" />
              </li>
            ))}
          </ul>
        </div>
      )}
    </Section>
  );
}

function SettingsMenu() {
  const [look, mode] = useAppearance();
  const client = useQueryClient();
  const navigate = useNavigate();
  const leave = async () => {
    await signOut().catch(() => undefined);
    wipeDrafts(); // at once, before anything re-renders with them
    requests.reset(true); // V3-P103: what this tab followed, and what it sent with a key (it holds paths)
    clearProjectsDoubt();
    unconfirmed.reset();
    await client.invalidateQueries({ queryKey: keys.session });
    navigate("/");
  };
  return (
    <Menu>
      <MenuTrigger asChild>
        <button
          type="button"
          className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted hover:bg-raised hover:text-ink"
        >
          <Settings className="size-4" /> Settings
        </button>
      </MenuTrigger>
      <MenuContent side="top" align="start" className="w-56">
        <MenuLabel>Look</MenuLabel>
        <MenuRadioGroup value={look} onValueChange={(value) => setLook(value as Look)}>
          <MenuRadioItem value="classic">Classic</MenuRadioItem>
          <MenuRadioItem value="warm">Warm</MenuRadioItem>
        </MenuRadioGroup>
        <MenuSeparator />
        <MenuLabel>Mode</MenuLabel>
        <MenuRadioGroup value={mode} onValueChange={(value) => setMode(value as Mode)}>
          <MenuRadioItem value="system"><Monitor className="size-3.5" /> System</MenuRadioItem>
          <MenuRadioItem value="light"><Sun className="size-3.5" /> Light</MenuRadioItem>
          <MenuRadioItem value="dark"><Moon className="size-3.5" /> Dark</MenuRadioItem>
        </MenuRadioGroup>
        <MenuSeparator />
        <MenuItem onSelect={() => navigate("/settings/instructions")}>Instructions</MenuItem>
        <MenuItem onSelect={() => navigate("/settings/memory")}>Memory</MenuItem>
        <MenuItem onSelect={() => navigate("/settings/devices")}>Devices</MenuItem>
        <MenuItem onSelect={() => navigate("/settings/tools")}>Tool servers</MenuItem>
        <MenuItem onSelect={() => navigate("/settings/folders")}>Folders</MenuItem>
        <MenuItem onSelect={() => navigate("/trash")}>Trash</MenuItem>
        <MenuItem onSelect={() => void leave()} className="text-danger">
          <LogOut className="size-3.5" /> Sign out
        </MenuItem>
      </MenuContent>
    </Menu>
  );
}

export function Sidebar({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  return (
    <nav className="flex h-full w-72 flex-col bg-sidebar" aria-label="Sidebar">
      <div className="flex items-center justify-between px-3 pb-2 pt-3">
        <span className="px-1 text-sm font-semibold tracking-wide">HIMARI</span>
        <IconButton label="Close sidebar" onClick={onClose}>
          <PanelLeftClose className="size-4" />
        </IconButton>
      </div>
      <div className="px-2">
        <button
          type="button"
          onClick={() => navigate("/")}
          className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium hover:bg-raised"
        >
          <SquarePen className="size-4" /> New task
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        <Section title="HIMARI">
          <ConversationList />
        </Section>
        <HubSection />
      </div>
      <div className="border-t border-line p-2">
        <SettingsMenu />
      </div>
    </nav>
  );
}

