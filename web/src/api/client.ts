/**
 * Talking to the Lectern server.
 *
 * `lectern serve` opens the page as http://127.0.0.1:<port>/#token=<token>. The token is
 * moved into sessionStorage and removed from the address bar, then sent with every request:
 * as the X-Lectern-Token header over HTTP, and as the `lectern.token.<token>` subprotocol on
 * WebSockets (browsers can't set headers there). Server-sent events are read with fetch,
 * because EventSource can't send headers either.
 */

const STORAGE_KEY = "lectern.token";
const TOKEN_HEADER = "X-Lectern-Token";
const WS_SUBPROTOCOL = "lectern.v1";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
  }
}

/** Take the token from the URL fragment (once), keep it for this tab, and clean the URL. */
export function initToken(win: Window = window): string | null {
  const params = new URLSearchParams(win.location.hash.slice(1));
  const fromUrl = params.get("token");
  if (fromUrl) {
    win.sessionStorage.setItem(STORAGE_KEY, fromUrl);
    params.delete("token");
    const rest = params.toString();
    const url = win.location.pathname + win.location.search + (rest ? `#${rest}` : "");
    win.history.replaceState(win.history.state, "", url);
  }
  return win.sessionStorage.getItem(STORAGE_KEY);
}

export function getToken(win: Window = window): string | null {
  return win.sessionStorage.getItem(STORAGE_KEY);
}

function withToken(init: RequestInit = {}): RequestInit {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set(TOKEN_HEADER, token);
  return { ...init, headers };
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, withToken(init));
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // not JSON; keep the status text
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

/** A raw response (for file contents), with the token, throwing ApiError when not OK. */
export async function apiRaw(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(path, withToken(init));
  if (!response.ok) throw new ApiError(response.status, response.statusText);
  return response;
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/** Read a server-sent event stream; calls `onEvent` with each parsed `data:` payload, and
 * `onOpen` once the server has answered, before the first event. Returns when the stream ends. */
export async function readEvents<T>(
  path: string,
  onEvent: (event: T) => void,
  signal: AbortSignal,
  onOpen: () => void = () => {},
): Promise<void> {
  const response = await fetch(path, withToken({ signal }));
  if (!response.ok || !response.body) throw new ApiError(response.status, response.statusText);
  onOpen();
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value;
    let end: number;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const data = block
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (data) onEvent(JSON.parse(data) as T);
    }
  }
}

/** How long to wait before each reconnect: a restart takes a few seconds, a longer outage
 * isn't worth asking about every second. */
export const RECONNECT_DELAYS_MS = [1000, 2000, 5000, 10000];

/** Follow a server-sent event stream until `signal` aborts. When the stream ends or the
 * server can't be reached (a restart, an update), wait and open it again. `onConnect` runs
 * each time the stream opens, before its first event: after a restart the server's numbering
 * starts again, so whatever a reader counted from the last stream no longer applies. An error
 * answer (a wrong token, a session that's gone) ends it: a retry wouldn't change that. A 5xx
 * is retried, since a server shutting down can answer one. */
export async function followEvents<T>(
  path: string,
  onEvent: (event: T) => void,
  signal: AbortSignal,
  onConnect: () => void = () => {},
): Promise<void> {
  let failures = 0;
  while (!signal.aborted) {
    try {
      await readEvents(path, onEvent, signal, () => {
        failures = 0;
        onConnect();
      });
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof ApiError && error.status < 500) throw error;
    }
    if (signal.aborted) return;
    const delay = RECONNECT_DELAYS_MS[Math.min(failures, RECONNECT_DELAYS_MS.length - 1)];
    failures += 1;
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, delay);
      signal.addEventListener("abort", () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
    });
  }
}

export function openSocket(path: string, win: Window = window): WebSocket {
  const token = getToken(win) ?? "";
  const scheme = win.location.protocol === "https:" ? "wss" : "ws";
  return new WebSocket(`${scheme}://${win.location.host}${path}`, [
    WS_SUBPROTOCOL,
    `lectern.token.${token}`,
  ]);
}
