/** api.ts — every conversation with the backend, in one place.
 *
 * Two kinds of traffic:
 *   - plain JSON fetches (get/post/del below), and
 *   - SSE streams, which need careful FRAME BUFFERING: a network chunk is
 *     NOT an SSE frame — one chunk may hold three frames or half of one.
 *     readSSE() owns that logic so no view ever reimplements it wrong.
 */

/** GET a JSON endpoint (throws on HTTP errors with the server's message). */
export async function get<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await errorText(response));
  return response.json();
}

/** POST a JSON body (or nothing) and parse the JSON reply. */
export async function post<T>(url: string, body?: unknown): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(await errorText(response));
  return response.json();
}

/** PUT — used by the soul editor. */
export async function put<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(await errorText(response));
  return response.json();
}

/** DELETE. */
export async function del<T>(url: string): Promise<T> {
  const response = await fetch(url, { method: "DELETE" });
  if (!response.ok) throw new Error(await errorText(response));
  return response.json();
}

/** Pull a useful message out of an error response (FastAPI puts it in
 *  {"detail": …}); fall back to the status line. */
async function errorText(response: Response): Promise<string> {
  try {
    const body = await response.json();
    return body.detail ?? `${response.status} ${response.statusText}`;
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

/** Consume an SSE response body, invoking onFrame for each `data:` payload.
 *
 * The buffering here is the part that matters (and the part that breaks
 * silently if skipped): split on the blank line that terminates a frame,
 * and keep any trailing partial in the buffer for the next chunk.
 */
export async function readSSE(
  response: Response,
  onFrame: (payload: string) => void,
): Promise<void> {
  if (!response.ok || !response.body) throw new Error(await errorText(response));
  // Bytes → text → our own frame splitter.
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";          // keep the trailing partial
    for (const frame of frames) {
      // Only data frames matter; comments (": heartbeat") are ignored.
      if (frame.startsWith("data: ")) onFrame(frame.slice(6));
    }
  }
}
