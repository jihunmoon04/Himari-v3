import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";
import { ApiError } from "@/api/client";
import { createConversation, sendMessage } from "@/api/himari";
import { startHubSession, type MessageChoices } from "@/api/hub";
import { keys, useHubProjects } from "@/app/queries";
import { Composer } from "@/components/Composer";
import { MessageOptions, chosen } from "@/components/MessageOptions";
import { PageHeader } from "@/components/Shell";
import { dismiss, failedBeforeRunning, follow, runsIn, useFollowed } from "@/features/hub/follow";
import { sendProblem } from "@/routes/HubSession";

const HIMARI = "";
const pick = "rounded-md border border-line bg-transparent px-1.5 py-1 text-xs text-muted outline-none focus:border-faint";

/** A new task (V3-P102 §4.1): to HIMARI (a new conversation), or straight to one of the hub's projects (another
 *  session of its lane, ``new``). An agent session exists only once its task runs, so the page follows the task
 *  and opens the session when the hub names it. The follow store keeps new tasks for the whole page: one started
 *  here before the user went elsewhere is still told on coming back (opened, failed, or still starting), as a
 *  notice rather than a jump. */
export function Home() {
  const navigate = useNavigate();
  const client = useQueryClient();
  const hub = useHubProjects();
  const [error, setError] = useState<string | null>(null);
  const [target, setTarget] = useState(HIMARI);
  const [agent, setAgent] = useState<string | undefined>(undefined);
  const [choices, setChoices] = useState<MessageChoices>({});
  const startedHere = useRef(new Set<number>()); // opened as soon as it runs; others are only told
  const followed = useFollowed();
  const fresh = followed.filter((f) => f.from === null); // new sessions asked for from this page
  const starting = fresh.some((f) => startedHere.current.has(f.task.id) && runsIn(f) === null && !f.done);

  const projects = hub.status === "on" ? (hub.data?.projects ?? []).filter((p) => p.name) : [];
  const project = projects.find((p) => p.name === target);
  const agents = (hub.data?.agents ?? []).filter((a) => a.installed !== false && a.name).map((a) => a.name!);
  const chosenAgent = agent ?? project?.default_agent ?? agents[0];

  // a project chosen while the hub answered, gone from the list (the hub went down): back to HIMARI, so what is
  // shown is always where the message goes
  useEffect(() => {
    if (target !== HIMARI && hub.status !== "loading" && !project) {
      setTarget(HIMARI);
      setAgent(undefined);
      setChoices({});
    }
  }, [target, hub.status, project]);

  useEffect(() => {
    for (const mine of fresh) {
      if (!startedHere.current.has(mine.task.id)) continue;
      const where = runsIn(mine);
      if (where !== null) {
        dismiss(mine.task.id); // the session's own page shows it from here
        navigate(`/s/${where}`);
        return;
      }
      if (failedBeforeRunning(mine)) {
        setError(`The agent did not start: ${mine.lost ?? mine.task.result ?? "the task failed"}`);
        dismiss(mine.task.id);
      }
    }
  }, [fresh, navigate]);
  const elsewhere = fresh.filter((f) => !startedHere.current.has(f.task.id));

  const startHimari = async (text: string) => {
    const made = await createConversation();
    await sendMessage(made.id, text);
    await client.invalidateQueries({ queryKey: keys.conversations });
    navigate(`/c/${encodeURIComponent(made.id)}`);
  };

  const startAgent = async (name: string, text: string) => {
    const { task } = await startHubSession(name, text, { agent: chosenAgent, ...chosen(choices) });
    startedHere.current.add(task.id);
    follow(task, null, name);
  };

  const start = async (text: string) => {
    setError(null);
    try {
      if (project?.name) await startAgent(project.name, text);
      else await startHimari(text);
    } catch (failure) {
      setError(!project
        ? failure instanceof ApiError ? failure.message : "Could not start the conversation."
        : sendProblem(failure));
      throw failure; // the draft stays
    }
  };

  const tools = projects.length > 0 ? (
    <>
      <select aria-label="Send to" value={target} className={pick}
        onChange={(event) => { setTarget(event.target.value); setAgent(undefined); setChoices({}); }}>
        <option value={HIMARI}>To: HIMARI</option>
        {projects.map((p) => <option key={p.name} value={p.name}>To: {p.name}</option>)}
      </select>
      {project && agents.length > 0 && (
        <select aria-label="Agent" value={chosenAgent} className={pick}
          onChange={(event) => { setAgent(event.target.value); setChoices({}); }}>
          {agents.map((name) => <option key={name} value={name}>{name}</option>)}
        </select>
      )}
      {project && <MessageOptions projects={hub.data} agent={chosenAgent} value={choices} onChange={setChoices} />}
    </>
  ) : undefined;

  return (
    <>
      <PageHeader />
      <div className="flex flex-1 flex-col items-center justify-center px-4 pb-[12vh]">
        <div className="w-full max-w-2xl">
          <h1 className="mb-6 text-center text-2xl font-semibold tracking-tight">What should we work on?</h1>
          <Composer
            draftKey="home"
            onSend={start}
            autoFocus
            disabled={starting}
            placeholder={project
              ? `Start a ${chosenAgent ?? "agent"} session in ${project.name}`
              : "Ask HIMARI, or hand it a task"}
            tools={tools}
          />
          {starting && (
            <p role="status" className="mt-2 text-sm text-muted">Starting the agent… the session opens once it runs.</p>
          )}
          {elsewhere.map((f) => {
            const where = runsIn(f);
            return (
              <p key={f.task.id} role={failedBeforeRunning(f) ? "alert" : "status"}
                className={failedBeforeRunning(f) ? "mt-2 text-sm text-danger" : "mt-2 text-sm text-muted"}>
                {where !== null ? (
                  <>
                    The {f.task.agent ?? "agent"} session in {f.project} started.{" "}
                    <Link to={`/s/${where}`} className="underline" onClick={() => dismiss(f.task.id)}>Open it</Link>
                  </>
                ) : failedBeforeRunning(f) ? (
                  <>The agent in {f.project} did not start: {f.lost ?? f.task.result ?? "the task failed"}.</>
                ) : (
                  <>Starting an agent in {f.project}…</>
                )}
                {(where !== null || failedBeforeRunning(f)) && (
                  <button type="button" className="ml-2 text-xs text-faint underline" onClick={() => dismiss(f.task.id)}>
                    Dismiss
                  </button>
                )}
              </p>
            );
          })}
          {error && <p role="alert" className="mt-2 text-sm text-danger">{error}</p>}
        </div>
      </div>
    </>
  );
}
