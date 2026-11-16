import { useQueryClient } from "@tanstack/react-query";
import { FolderGit2, LogOut, Monitor, Moon, PanelLeftClose, Settings, SquarePen, Sun } from "lucide-react";
import { NavLink, useNavigate } from "react-router";
import { signOut } from "@/api/himari";
import { keys, useActivity, useConversations } from "@/app/queries";
import { setLook, setMode, useAppearance, type Look, type Mode } from "@/lib/appearance";
import { cn } from "@/lib/cn";
import { IconButton } from "./ui/button";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuRadioGroup, MenuRadioItem, MenuSeparator, MenuTrigger } from "./ui/menu";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-4">
      <h2 className="px-3 pb-1 text-xs font-medium text-faint">{title}</h2>
      {children}
    </section>
  );
}

function ConversationList() {
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

function SettingsMenu() {
  const [look, mode] = useAppearance();
  const client = useQueryClient();
  const navigate = useNavigate();
  const leave = async () => {
    await signOut().catch(() => undefined);
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
        <MenuItem onSelect={() => window.location.assign("/")}>Open the classic page</MenuItem>
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
        <Section title="Projects">
          <p className="flex items-center gap-2 px-3 py-1 text-sm text-faint">
            <FolderGit2 className="size-4" /> Agent sessions come next.
          </p>
        </Section>
      </div>
      <div className="border-t border-line p-2">
        <SettingsMenu />
      </div>
    </nav>
  );
}

