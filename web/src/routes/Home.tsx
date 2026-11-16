import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
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
 *  and opens the session when the hub names it. */
export function Home() {
  const navigate = useNavigate();
  const client = useQueryClient();
  const hub = useHubProjects();
  const [error, setError] = useState<string | null>(null);
  const [target, setTarget] = useState(HIMARI);
  const [agent, setAgent] = useState<string | undefined>(undefined);
  const [choices, setChoices] = useState<MessageChoices>({});
  const [starting, setStarting] = useState<number | null>(null);
  const followed = useFollowed();

  const projects = hub.status === "on" ? (hub.data?.projects ?? []).filter((p) => p.name) : [];
  const project = projects.find((p) => p.name === target);
  const agents = (hub.data?.agents ?? []).filter((a) => a.installed !== false && a.name).map((a) => a.name!);
  const chosenAgent = agent ?? project?.default_agent ?? agents[0];

  useEffect(() => {
    const mine = followed.find((f) => f.task.id === starting);
    if (!mine) return;
    const where = runsIn(mine);
    if (where !== null) {
      dismiss(mine.task.id); // the session's own page shows it from here
      setStarting(null);
      navigate(`/s/${where}`);
    } else if (failedBeforeRunning(mine)) {
      setError(`The agent did not start: ${mine.lost ?? mine.task.result ?? "the task failed"}`);
      dismiss(mine.task.id);
      setStarting(null);
    }
  }, [followed, starting, navigate]);

  const startHimari = async (text: string) => {
    const made = await createConversation();
    await sendMessage(made.id, text);
    await client.invalidateQueries({ queryKey: keys.conversations });
    navigate(`/c/${encodeURIComponent(made.id)}`);
  };

  const startAgent = async (name: string, text: string) => {
    const { task } = await startHubSession(name, text, { agent: chosenAgent, ...chosen(choices) });
    follow(task, null, name);
    setStarting(task.id);
  };

  const start = async (text: string) => {
    setError(null);
    try {
      if (target === HIMARI) await startHimari(text);
      else await startAgent(target, text);
    } catch (failure) {
      setError(target === HIMARI
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
            disabled={starting !== null}
            placeholder={project
              ? `Start a ${chosenAgent ?? "agent"} session in ${project.name}`
              : "Ask HIMARI, or hand it a task"}
            tools={tools}
          />
          {starting !== null && (
            <p role="status" className="mt-2 text-sm text-muted">Starting the agent… the session opens once it runs.</p>
          )}
          {error && <p role="alert" className="mt-2 text-sm text-danger">{error}</p>}
        </div>
      </div>
    </>
  );
}
