// V3-P102 §4: the list on the left, the page in the middle (each page brings its own right-hand panel).
// Under 768px the list is a drawer over the page.
import { PanelLeftOpen } from "lucide-react";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { Outlet, useLocation } from "react-router";
import { useHubPending, useHubProjects, usePrincipalEvents } from "@/app/queries";
import { openRequests, useHubRequests } from "@/features/hub/requests";
import { cn } from "@/lib/cn";
import { ProjectDialogHost } from "./ProjectDialog";
import { Sidebar } from "./Sidebar";
import { Toasts } from "./Toasts";
import { IconButton } from "./ui/button";

const NARROW = "(max-width: 767px)";

/** Whether the screen is narrow (under 768 px): lists and panels become drawers and sheets. */
export function useNarrow() {
  const [narrow, setNarrow] = useState(() => window.matchMedia(NARROW).matches);
  useEffect(() => {
    const query = window.matchMedia(NARROW);
    const changed = () => setNarrow(query.matches);
    query.addEventListener("change", changed);
    return () => query.removeEventListener("change", changed);
  }, []);
  return narrow;
}

const SidebarToggle = createContext<{ open: boolean; show: () => void }>({ open: true, show: () => undefined });

/** The button a page's header shows when the list is closed. */
export function OpenSidebarButton() {
  const { open, show } = useContext(SidebarToggle);
  if (open) return null;
  return (
    <IconButton label="Open sidebar" onClick={show}>
      <PanelLeftOpen className="size-4" />
    </IconButton>
  );
}

export function PageHeader({ children, actions }: { children?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="flex h-12 shrink-0 items-center gap-2 px-3">
      <OpenSidebarButton />
      <div className="min-w-0 flex-1">{children}</div>
      <div className="flex items-center gap-1">{actions}</div>
    </header>
  );
}

/** "(2) HIMARI" while two agent requests (and HIMARI's project requests, V3-P103 §6.7) wait for the user. */
function useWaitingTitle() {
  const hub = useHubProjects();
  const pending = useHubPending(hub.status === "on");
  const asked = openRequests(useHubRequests()).filter((r) => r.state === "waiting_user").length;
  const count = (pending.data?.pending.length ?? 0) + (hub.status === "on" ? asked : 0);
  useEffect(() => {
    document.title = count > 0 ? `(${count}) HIMARI` : "HIMARI";
  }, [count]);
}

export function Shell() {
  usePrincipalEvents(true);
  useWaitingTitle();
  const narrow = useNarrow();
  const [wide, setWide] = useState(true); // the list's state on a wide screen
  const [drawer, setDrawer] = useState(false); // and on a narrow one
  const location = useLocation();
  useEffect(() => setDrawer(false), [location.pathname]);
  const open = narrow ? drawer : wide;
  const show = () => (narrow ? setDrawer(true) : setWide(true));
  const hide = () => (narrow ? setDrawer(false) : setWide(false));

  return (
    <SidebarToggle.Provider value={{ open, show }}>
      <div className="flex h-dvh overflow-hidden bg-bg text-ink">
        {narrow ? (
          drawer && (
            <>
              <button type="button" aria-label="Close sidebar" className="fixed inset-0 z-30 bg-black/40" onClick={hide} />
              <div className="fixed inset-y-0 left-0 z-40 shadow-xl">
                <Sidebar onClose={hide} />
              </div>
            </>
          )
        ) : (
          <div className={cn("shrink-0 border-r border-line", !wide && "hidden")}>
            <Sidebar onClose={hide} />
          </div>
        )}
        <main className="flex min-w-0 flex-1 flex-col">
          <Outlet />
        </main>
        <Toasts />
        <ProjectDialogHost />
      </div>
    </SidebarToggle.Provider>
  );
}
