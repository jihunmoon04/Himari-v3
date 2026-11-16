// One hub session's events, live (V3-P102 §4.2, §7.3).
//
// - First the latest range: `after = max(first_seq - 1, last_seq - RANGE)` from the session's detail, read on with
//   `next` while `more`. "Earlier" reads the range before the oldest one held, the same way.
// - Then its stream from the last seq received (the browser sends Last-Event-ID on reconnecting by itself).
// - `reset` (the first `state` with reset, a `reset` frame, or a page with `reset`), or a cursor the hub refuses
//   (409 hub_bad_cursor): what was held is gone at the hub; everything is read again.
// - A stream refused before it starts (no stream left: 503; the session is unknown: 404): the events are read
//   every POLL_MS and the stream is tried again after RETRY_MS. A first read that fails is tried again with backoff.
// - Every answer is checked against the read it belongs to: a session left, or read again, ignores what comes late.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api/client";
import { readHubEvents, readHubSession, hubSessionStreamUrl, type HubEvent, type HubSession } from "@/api/hub";
import { mergeEvents } from "./items";

const RANGE = 200;
const POLL_MS = 5_000;
const RETRY_MS = 30_000;
const FIRST_RETRY_MS = [2_000, 5_000, 10_000, 30_000];

/** Kinds after which what waits for the user may have changed (§7.3). */
export const PENDING_SIGNALS = new Set(["request", "answer", "turn_end"]);

export interface SessionEvents {
  events: HubEvent[];
  loading: boolean;
  error: string | null; // the first read failed (it is being tried again)
  earlier: boolean; // older events exist at the hub that are not held
  loadingEarlier: boolean;
  earlierError: string | null;
  live: boolean; // the stream is open (otherwise it is being read by asking)
  loadEarlier: () => void;
}

class Restart extends Error {}

async function readOn(id: number, after: number): Promise<{ events: HubEvent[]; cursor: number; first: number | null }> {
  const events: HubEvent[] = [];
  let cursor = after;
  let first: number | null = null;
  for (let pages = 0; pages < 50; pages += 1) {
    let page;
    try {
      page = await readHubEvents(id, cursor, RANGE);
    } catch (failure) {
      if (failure instanceof ApiError && failure.code === "hub_bad_cursor") throw new Restart();
      throw failure;
    }
    if (page.reset) throw new Restart(); // what the cursor pointed into was pruned
    first = page.first_seq ?? first;
    events.push(...page.events);
    cursor = page.next ?? (page.events.at(-1)?.seq ?? cursor);
    if (!page.more || !page.events.length) break;
  }
  return { events, cursor, first };
}

/** ``onSignal``: the session's listing changed (a ``state`` frame), or an event came after which what waits for
 *  the user may differ. */
export function useHubSessionEvents(id: number, onSignal: (session: HubSession | null) => void): SessionEvents {
  const [events, setEvents] = useState<HubEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [firstSeq, setFirstSeq] = useState<number | null>(null);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [earlierError, setEarlierError] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [generation, setGeneration] = useState(0); // bumped on a reset: everything is read again
  const onSignalRef = useRef(onSignal);
  onSignalRef.current = onSignal;
  const readToken = useRef(0); // the read the answers belong to; a late one of another is dropped

  useEffect(() => {
    const token = ++readToken.current;
    let ended = false;
    let source: EventSource | null = null;
    let poll: number | undefined;
    let retry: number | undefined;
    let cursor = 0;
    setEvents([]);
    setLoading(true);
    setError(null);
    setLive(false);
    setLoadingEarlier(false);
    setEarlierError(null);
    setFirstSeq(null);

    const current = () => !ended && readToken.current === token;
    const add = (incoming: HubEvent[]) => {
      if (!current() || !incoming.length) return;
      cursor = Math.max(cursor, ...incoming.map((e) => e.seq));
      setEvents((held) => mergeEvents(held, incoming));
      if (incoming.some((e) => PENDING_SIGNALS.has(e.kind))) onSignalRef.current(null);
    };
    const restart = () => current() && setGeneration((n) => n + 1);

    const readByAsking = async () => {
      try {
        const page = await readOn(id, cursor);
        add(page.events);
      } catch (failure) {
        if (failure instanceof Restart) restart(); // otherwise the next poll tries again
      }
    };

    const connect = () => {
      if (!current()) return;
      window.clearInterval(poll);
      poll = undefined;
      source = new EventSource(hubSessionStreamUrl(id, cursor), { withCredentials: true });
      source.addEventListener("open", () => current() && setLive(true));
      source.addEventListener("state", (message) => {
        try {
          const data = JSON.parse((message as MessageEvent<string>).data) as { reset?: boolean; session?: HubSession | null };
          if (data.reset) return restart();
          if (current()) onSignalRef.current(data.session ?? null);
        } catch { /* skipped */ }
      });
      source.addEventListener("event", (message) => {
        try {
          add([JSON.parse((message as MessageEvent<string>).data) as HubEvent]);
        } catch { /* skipped */ }
      });
      source.addEventListener("reset", restart);
      source.addEventListener("error", () => {
        if (current()) setLive(false);
        if (source?.readyState !== EventSource.CLOSED) return; // the browser reconnects with Last-Event-ID
        source = null;
        poll = window.setInterval(() => void readByAsking(), POLL_MS);
        retry = window.setTimeout(connect, RETRY_MS);
      });
    };

    const first = async (attempt: number) => {
      try {
        const detail = await readHubSession(id);
        if (!current()) return;
        const last = detail.last_seq ?? 0;
        const after = Math.max((detail.first_seq ?? 1) - 1, last - RANGE, 0);
        const page = await readOn(id, after);
        if (!current()) return;
        setFirstSeq(Math.max(detail.first_seq ?? 1, page.first ?? 0)); // pruned meanwhile: the later of the two
        cursor = page.cursor;
        add(page.events);
        setError(null);
        setLoading(false);
        connect();
      } catch (failure) {
        if (!current()) return;
        if (failure instanceof Restart) return restart();
        if (failure instanceof ApiError && failure.status === 404) {
          setLoading(false);
          setError("No such session.");
          return;
        }
        setLoading(false);
        setError(failure instanceof Error ? failure.message : "Could not read this session.");
        const wait = FIRST_RETRY_MS[Math.min(attempt, FIRST_RETRY_MS.length - 1)]!;
        retry = window.setTimeout(() => void first(attempt + 1), wait); // the hub may be back by then
      }
    };
    void first(0);

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
    const token = readToken.current;
    const after = Math.max(firstSeq - 1, oldest - 1 - RANGE);
    setLoadingEarlier(true);
    setEarlierError(null);
    readHubEvents(id, after, oldest - 1 - after)
      .then((page) => {
        if (readToken.current !== token) return; // another session, or read again: not ours any more
        if (page.first_seq !== null && page.first_seq > firstSeq) setFirstSeq(page.first_seq); // pruned meanwhile
        setEvents((held) => mergeEvents(held, page.events.filter((e) => e.seq < oldest)));
      })
      .catch(() => readToken.current === token && setEarlierError("Could not read the earlier events."))
      .finally(() => readToken.current === token && setLoadingEarlier(false));
  }, [earlier, firstSeq, id, loadingEarlier, oldest]);

  return { events, loading, error, earlier, loadingEarlier, earlierError, live, loadEarlier };
}
