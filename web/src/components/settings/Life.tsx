// Settings → HIMARI (character-life §6; character-routine §5, §9.2): the life of the persona in use (or the default),
// which HIMARI lives apart from the user and the app never shows, in five tabs at /settings/himari/<tab> (Today when
// none is named): Today (HIMARI now, today's and tomorrow's slots, today's events), Schedule (its slots from today on,
// written and moved), Records (its days, a line a date, with older ones read on), Projects (its projects and their
// pieces) and About HIMARI (its facts and kept thoughts). The tabs live in ./himari. Today is the server's (it dates
// every entry); a tab change asks first while an editor holds changes not saved.
import { useQuery } from "@tanstack/react-query";
import { useCallback, useRef } from "react";
import { Link, Navigate, useParams } from "react-router";
import { readLife } from "@/api/settings";
import { addDays, localDay } from "@/features/schedule";
import { cn } from "@/lib/cn";
import { AboutTab } from "./himari/About";
import { ProjectsTab } from "./himari/Projects";
import { RecordsTab } from "./himari/Records";
import { ScheduleTab } from "./himari/Schedule";
import { KEY, NEXT_TURN, Unsaved } from "./himari/shared";
import { TodayTab } from "./himari/Today";

export const TABS = [
  { id: "today", label: "Today" },
  { id: "schedule", label: "Schedule" },
  { id: "records", label: "Records" },
  { id: "projects", label: "Projects" },
  { id: "about", label: "About HIMARI" },
] as const;
type Tab = (typeof TABS)[number]["id"];

export function LifeSettings() {
  const named = useParams().tab;
  const query = useQuery({ queryKey: KEY, queryFn: readLife });
  const unsaved = useRef(new Set<symbol>()); // the editors open with changes not saved
  const report = useCallback((token: symbol, dirty: boolean) => {
    if (dirty) unsaved.current.add(token);
    else unsaved.current.delete(token);
  }, []);
  const tab = TABS.find((t) => t.id === (named ?? "today"))?.id as Tab | undefined;
  if (!tab) return <Navigate to="/settings/himari" replace />;
  if (query.isLoading) return <p className="text-sm text-faint">Loading…</p>;
  if (query.error || !query.data) return <p role="alert" className="text-sm text-danger">Could not read HIMARI's life.</p>;
  const life = query.data;
  const today = life.today ?? localDay(new Date()); // the browser's only for a server from before
  const since = life.since ?? addDays(today, -30);
  return (
    <section className="space-y-4" aria-label="HIMARI">
      <h2 className="text-base font-semibold">HIMARI</h2>
      <p className="text-sm text-muted">
        <span className="font-medium text-ink">{life.persona ? `${life.persona.name}'s life` : "Default (no persona)"}</span>.
        HIMARI lives it apart from you: its day, its schedule and sleep, the record of its days, its projects and what
        it has settled about itself. Each persona has its own; this is the one in use. The app never shows it.
        {` ${NEXT_TURN}`}
      </p>
      {!life.enabled && (
        <p className="text-sm text-wait">
          HIMARI's life is not used now (the companion or life is off on this server); it is kept as it is.
        </p>
      )}
      <nav aria-label="HIMARI tabs" className="-mx-1 flex gap-1 overflow-x-auto border-b border-line px-1">
        {TABS.map((t) => (
          <Link key={t.id} to={`/settings/himari/${t.id}`} aria-current={t.id === tab ? "page" : undefined}
            onClick={(event) => {
              if (t.id !== tab && unsaved.current.size > 0
                && !window.confirm("You have changes not saved on this tab. Leave it and lose them?")) {
                event.preventDefault();
              }
            }}
            className={cn("-mb-px whitespace-nowrap border-b-2 border-transparent px-3 py-1.5 text-sm text-muted",
              "hover:text-ink", t.id === tab && "border-ink text-ink")}>
            {t.label}
          </Link>
        ))}
      </nav>
      <Unsaved.Provider value={report}>
        {tab === "today" && <TodayTab life={life} today={today} />}
        {tab === "schedule" && <ScheduleTab life={life} today={today} />}
        {tab === "records" && <RecordsTab life={life} today={today} since={since} />}
        {tab === "projects" && <ProjectsTab life={life} />}
        {tab === "about" && <AboutTab life={life} />}
      </Unsaved.Provider>
    </section>
  );
}
