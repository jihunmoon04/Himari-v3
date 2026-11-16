// An EventSource that the caller closes; frames come parsed. The browser reconnects by itself (sending
// Last-Event-ID) unless the caller closes it.
export type Frame = { event: string; data: Record<string, unknown>; id: string };

export function openStream(url: string, events: readonly string[], onFrame: (frame: Frame) => void,
                           onError?: () => void): () => void {
  const source = new EventSource(url, { withCredentials: true });
  for (const name of events) {
    source.addEventListener(name, (message) => {
      const event = message as MessageEvent<string>;
      let data: Record<string, unknown> = {};
      try {
        const parsed: unknown = JSON.parse(event.data);
        if (parsed && typeof parsed === "object") data = parsed as Record<string, unknown>;
      } catch {
        return;
      }
      onFrame({ event: name, data, id: event.lastEventId });
    });
  }
  if (onError) source.addEventListener("error", onError);
  return () => source.close();
}
