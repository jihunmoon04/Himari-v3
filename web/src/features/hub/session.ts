// One hub session's events, live (V3-P102 §4.2, §7.3).
//
// - First the latest range: `after = max(first_seq - 1, last_seq - RANGE)` from the session's detail, read on with
//   `next` while `more`. "Earlier" reads the range before the oldest one held, the same way.
// - Then its stream from the last seq received (the browser sends Last-Event-ID on reconnecting by itself).
// - `reset` (the first `state` with reset, or a `reset` frame): what was held is gone at the hub; start again.
// - A stream refused before it starts (no stream left: 503; the session is unknown: 404): the events are read
//   every POLL_MS and the stream is tried again after RETRY_MS.
import { useCallback, useEffect, useRef, useState } from "react";
import { readHubEvents, readHubSession, hubSessionStreamUrl, type HubEvent, type HubSession } from "@/api/hub";
import { mergeEvents } from "./items";

const RANGE = 200;
const POLL_MS = 5_000;
const RETRY_MS = 30_000;

export interface SessionEvents {
  events: HubEvent[];
  loading: boolean;
  error: string | null;
  earlier: boolean; // older events exist at the hub that are not held
  loadingEarlier: boolean;
  live: boolean; // the stream is open (otherwise it is being read by asking)
  loadEarlier: () => void;
}

async function readOn(id: number, after: number): Promise<{ events: HubEvent[]; cursor: number }> {
  const events: HubEvent[] = [];
  let cursor = after;
  for (let pages = 0; pages < 50; pages += 1) {
    const page = await readHubEvents(id, cursor, RANGE);
    events.push(...page.events);
    cursor = page.next ?? (page.events.at(-1)?.seq ?? cursor);
    if (!page.more || !page.events.length) break;
  }
  return { events, cursor };
}

export function useHubSessionEvents(id: number, onState: (session: HubSession | null) => void): SessionEvents {
  const [events, setEvents] = useState<HubEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [firstSeq, setFirstSeq] = useState<number | null>(null);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [live, setLive] = useState(false);
  const [generation, setGeneration] = useState(0); // bumped on a reset: everything is read again
  const onStateRef = useRef(onState);
  onStateRef.current = onState;

  useEffect(() => {
    let ended = false;
    let source: EventSource | null = null;
    let poll: number | undefined;
    let retry: number | undefined;
    let cursor = 0;
    setEvents([]);
    setLoading(true);
    setError(null);
    setLive(false);

    const add = (incoming: HubEvent[]) => {
      if (ended || !incoming.length) return;
      cursor = Math.max(cursor, ...incoming.map((e) => e.seq));
      setEvents((held) => mergeEvents(held, incoming));
    };
    const restart = () => !ended && setGeneration((n) => n + 1);

    const readByAsking = async () => {
      try {
        const page = await readOn(id, cursor);
        add(page.events);
      } catch {
        // the next poll tries again
      }
    };

    const connect = () => {
      if (ended) return;
      window.clearInterval(poll);
      source = new EventSource(hubSessionStreamUrl(id, cursor), { withCredentials: true });
      source.addEventListener("open", () => setLive(true));
      source.addEventListener("state", (message) => {
        try {
          const data = JSON.parse((message as MessageEvent<string>).data) as { reset?: boolean; session?: HubSession | null };
          if (data.reset) return restart();
          onStateRef.current(data.session ?? null);
        } catch { /* skipped */ }
      });
      source.addEventListener("event", (message) => {
        try {
          add([JSON.parse((message as MessageEvent<string>).data) as HubEvent]);
        } catch { /* skipped */ }
      });
      source.addEventListener("reset", restart);
      source.addEventListener("error", () => {
        setLive(false);
        if (source?.readyState !== EventSource.CLOSED) return; // the browser reconnects with Last-Event-ID
        source = null;
        poll = window.setInterval(() => void readByAsking(), POLL_MS);
        retry = window.setTimeout(connect, RETRY_MS);
      });
    };

    (async () => {
      try {
        const detail = await readHubSession(id);
        if (ended) return;
        const first = detail.first_seq ?? 1;
        const last = detail.last_seq ?? 0;
        const after = Math.max(first - 1, last - RANGE, 0);
        const page = await readOn(id, after);
        if (ended) return;
        setFirstSeq(first);
        cursor = page.cursor;
        add(page.events);
        setLoading(false);
        connect();
      } catch (failure) {
        if (ended) return;
        setLoading(false);
        setError(failure instanceof Error ? failure.message : "Could not read this session.");
      }
    })();

    return () => {
      ended = true;
      source?.close();
      window.clearInterval(poll);
      window.clearTimeout(retry);
    };
  }, [id, generation]);

  const oldest = events[0]?.seq ?? null;
  const earlier = firstSeq !== null && oldest !== null && oldest > firstSeq;

  const loadEarlier = useCallback(() => {
    if (!earlier || oldest === null || firstSeq === null || loadingEarlier) return;
    const after = Math.max(firstSeq - 1, oldest - 1 - RANGE);
    setLoadingEarlier(true);
    readHubEvents(id, after, oldest - 1 - after)
      .then((page) => setEvents((held) => mergeEvents(held, page.events.filter((e) => e.seq < oldest))))
      .catch(() => setError("Could not read the earlier events."))
      .finally(() => setLoadingEarlier(false));
  }, [earlier, firstSeq, id, loadingEarlier, oldest]);

  return { events, loading, error, earlier, loadingEarlier, live, loadEarlier };
}
