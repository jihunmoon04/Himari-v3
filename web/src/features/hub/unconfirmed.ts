// A project request or a folder removal sent with a client key (V3-P103 §4.1, H13), kept until its answer is known.
//
// Before it is sent the key, the key's time (this server's clock), the route and the body go to this tab's
// sessionStorage (they hold a device path: never localStorage). With the answer, the request's id is followed first
// (requests.track) and only then the entry goes. When the hub's answer was lost (``hub_unknown``, or this server
// could not be reached after it may have relayed it) the entry stays: the page shows it as unconfirmed, even after
// a reload, and it may be sent again with the same key, time and body, which gives back the same request (H13).
// Whether it is too late is the hub's call (``key_time``): ``client_key_expired`` and the like end the resending,
// and the entry can then only be discarded after the open requests and the projects are read again.
//
// Without storage (a private window) nothing outlives the page and the table of §4.1 applies as it is.
import { useSyncExternalStore } from "react";
import { ApiError } from "@/api/client";
import {
  makeProjectRequest,
  removeProjectFolder,
  type ClientKey,
  type HubProjectRequest,
  type ProjectRequestBody,
} from "@/api/hub";
import { requests as store, sessionKeyValue, type KeyValue, type RequestStore } from "./requests";

export const UNCONFIRMED_KEY = "himari.hub.unconfirmed";

export type Route = "request" | "remove";
export type RemoveBody = { project: string; runner: string };

export interface Unconfirmed extends ClientKey {
  route: Route;
  body: ProjectRequestBody | RemoveBody;
  at: number; // when it was first sent (this browser's clock: shown only)
  /** ``refused``: the hub said no to the key itself or to the request on a resend: only discarding is left. */
  refused?: string | null;
}

/** A refusal of the key itself: the request can no longer be found or made with it. */
export const keyRefusal = (error: unknown) =>
  error instanceof ApiError && error.code === "hub_refused" && /^client_key_\w+/.test(error.message);

/** The answer was lost on the way: it may have been done. Besides ``hub_unknown`` and a connection that broke, a
 *  5xx that is not this server's own (no error code of its own: a proxy's or a crash's) says nothing either. */
export const answerLost = (error: unknown) =>
  error instanceof ApiError && (error.code === "hub_unknown" || error.status === 0
    || (error.status >= 500 && error.code === `http_${error.status}`));

/** The request of a reply that should hold one; a reply without one (empty, cut, not JSON) is a lost answer. */
export function requestOf(reply: { request?: HubProjectRequest } | null | undefined): HubProjectRequest {
  const request = reply?.request;
  if (!request || !Number.isInteger(request.id) || !Number.isInteger(request.rev) || typeof request.state !== "string") {
    throw new ApiError(0, "offline", "The server's answer was not readable.");
  }
  return request;
}

export class UnconfirmedList {
  private items: Unconfirmed[];
  private listeners = new Set<() => void>();

  constructor(private readonly storage: () => KeyValue | null) {
    this.items = this.load();
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = () => this.items;

  private set(items: Unconfirmed[]) {
    this.items = items;
    try {
      const storage = this.storage();
      if (storage) {
        if (items.length) storage.setItem(UNCONFIRMED_KEY, JSON.stringify(items));
        else storage.removeItem(UNCONFIRMED_KEY);
      }
    } catch {
      // no storage: kept for this page only
    }
    for (const listener of this.listeners) listener();
  }

  put(entry: Unconfirmed) {
    this.set([...this.items.filter((item) => item.client_key !== entry.client_key), entry]);
  }

  drop(key: string) {
    if (this.items.some((item) => item.client_key === key)) this.set(this.items.filter((item) => item.client_key !== key));
  }

  // -- sends on their way: an entry being sent is neither discarded nor sent a second time -------------------------
  private inFlight = new Set<string>();
  private flying: ReadonlySet<string> = new Set();

  getSending = () => this.flying;

  /** False when this key is being sent already. */
  startSending(key: string): boolean {
    if (this.inFlight.has(key)) return false;
    this.inFlight.add(key);
    this.tell();
    return true;
  }

  endSending(key: string) {
    if (this.inFlight.delete(key)) this.tell();
  }

  /** The user's discard: never while its send is on its way (its answer may yet be lost: §4.1). */
  discard(key: string): boolean {
    if (this.inFlight.has(key)) return false;
    this.drop(key);
    return true;
  }

  private tell() {
    this.flying = new Set(this.inFlight);
    for (const listener of this.listeners) listener();
  }

  reset() {
    this.set([]);
  }

  private load(): Unconfirmed[] {
    try {
      const raw: unknown = JSON.parse(this.storage()?.getItem(UNCONFIRMED_KEY) ?? "[]");
      if (!Array.isArray(raw)) return [];
      return raw.filter((item): item is Unconfirmed => {
        const u = item as Unconfirmed | null;
        return !!u && typeof u.client_key === "string" && typeof u.key_time === "string"
          && (u.route === "request" || u.route === "remove") && !!u.body && typeof u.body === "object";
      });
    } catch {
      return [];
    }
  }
}

export const unconfirmed = new UnconfirmedList(sessionKeyValue);

export function useUnconfirmed(): readonly Unconfirmed[] {
  return useSyncExternalStore(unconfirmed.subscribe, unconfirmed.getSnapshot);
}

/** The keys being sent now. */
export function useUnconfirmedSending(): ReadonlySet<string> {
  return useSyncExternalStore(unconfirmed.subscribe, unconfirmed.getSending);
}

export interface Senders {
  request: (body: ProjectRequestBody & ClientKey) => Promise<{ request: HubProjectRequest }>;
  remove: (body: RemoveBody & ClientKey) => Promise<{ request: HubProjectRequest }>;
}

const realSenders: Senders = { request: (body) => makeProjectRequest(body), remove: (body) => removeProjectFolder(body) };

/** Send ``entry`` (the first time, or again with the same key): the request on success; the error otherwise. On a
 *  lost answer the entry stays; on a refusal of a first send it goes (nothing was made); on a refusal of a resend it
 *  stays, marked, to be discarded. */
export async function sendKeyed(entry: Unconfirmed, again: boolean, list: UnconfirmedList = unconfirmed,
                                requestStore: RequestStore = store, senders: Senders = realSenders,
                                ): Promise<{ request: HubProjectRequest } | { error: unknown }> {
  if (!list.startSending(entry.client_key)) {
    return { error: new ApiError(409, "sending", "It is being sent already.") };
  }
  list.put(again ? { ...entry, refused: null } : entry);
  const key = { client_key: entry.client_key, key_time: entry.key_time };
  try {
    const request = requestOf(entry.route === "request"
      ? await senders.request({ ...(entry.body as ProjectRequestBody), ...key })
      : await senders.remove({ ...(entry.body as RemoveBody), ...key }));
    requestStore.merge(request);
    void requestStore.track(request.id); // followed (and kept in this tab) before the entry goes (§5)
    list.drop(entry.client_key);
    return { request };
  } catch (error) {
    if (answerLost(error)) return { error };
    if (again || keyRefusal(error)) {
      list.put({ ...entry, refused: error instanceof ApiError ? error.message : "Refused." });
    } else {
      list.drop(entry.client_key); // refused or never sent: nothing was made
    }
    return { error };
  } finally {
    list.endSending(entry.client_key);
  }
}

/** An unconfirmed one for the same thing (route, kind, project, device): while it waits nothing new is sent for it,
 *  since a new key would make a second request if the first was done (§4.1). */
export const unconfirmedFor = (items: readonly Unconfirmed[], route: Route, project: string, runner: string,
                               kind?: string) =>
  items.find((u) => u.route === route && u.body.project === project && u.body.runner === runner
    && (kind === undefined || (u.body as ProjectRequestBody).kind === kind));

const sending = new Set<string>(); // route|project|runner of a send on its way (from asking for its key on)

/** Run ``send`` as the only send for this route, project and device: claimed at once, before anything is awaited, so
 *  two controls (a page's Remove and a card's Ask again) never send it with two keys. Null when one is on its way. */
export async function onlySend<T>(route: Route, project: string, runner: string, send: () => Promise<T>
                                  ): Promise<T | null> {
  const key = JSON.stringify([route, project, runner]);
  if (sending.has(key)) return null;
  sending.add(key);
  try {
    return await send();
  } finally {
    sending.delete(key);
  }
}

/** Requests of the same kind, project and device among the open ones (a hub without H13: what a lost answer may
 *  have made). */
export const sameAs = (entry: Unconfirmed, open: readonly HubProjectRequest[]) => {
  const kind = entry.route === "remove" ? "remove_folder" : (entry.body as ProjectRequestBody).kind;
  return open.filter((r) => r.kind === kind && r.project === entry.body.project && r.runner === entry.body.runner);
};
