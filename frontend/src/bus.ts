/** bus.ts — the client end of the backend's event stream.
 *
 * One EventSource-style connection to GET /api/events feeds the whole UI:
 * the avatar, the mode banner, the agent journal, download bars. Views
 * subscribe by topic; the connection reconnects itself with backoff, so a
 * backend restart (say, a model swap) heals without a page reload.
 */

import { readSSE } from "./api";

/** The shape of every backend event (mirrors seymour/events.py). */
export interface BusEvent {
  topic: string;                       // "scheduler" | "agent" | "engine" | …
  type: string;                        // what happened
  data: Record<string, any>;           // the details
  ts: number;                          // unix seconds
}

/** topic → set of handlers. "*" receives everything. */
const handlers = new Map<string, Set<(e: BusEvent) => void>>();

/** Subscribe to a topic ("*" for all). Returns an unsubscribe function. */
export function on(topic: string, handler: (e: BusEvent) => void): () => void {
  if (!handlers.has(topic)) handlers.set(topic, new Set());
  handlers.get(topic)!.add(handler);
  return () => handlers.get(topic)?.delete(handler);
}

/** Fan one event out to its topic's handlers and the wildcard's. */
function dispatch(event: BusEvent): void {
  for (const key of [event.topic, "*"]) {
    handlers.get(key)?.forEach((handler) => {
      try { handler(event); } catch (err) { console.error("bus handler", err); }
    });
  }
}

/** Open the stream and keep it open forever (reconnect with backoff). */
export function connect(): void {
  let delay = 1000;                    // backoff start: 1 s, doubling to 15 s
  const loop = async () => {
    while (true) {
      try {
        const response = await fetch("/api/events");
        delay = 1000;                  // connected: reset the backoff
        await readSSE(response, (payload) => {
          // Each data frame is one JSON event; parse and dispatch.
          try { dispatch(JSON.parse(payload)); } catch { /* ignore */ }
        });
      } catch { /* connection lost — fall through to retry */ }
      // Tell listeners the link is down (the avatar shows "sleeping").
      dispatch({ topic: "connection", type: "lost", data: {}, ts: Date.now() / 1000 });
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 2, 15000);
    }
  };
  loop();                              // fire and forget; runs for the tab's life
}
