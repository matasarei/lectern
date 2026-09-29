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

/** Read a server-sent event stream; calls `onEvent` with each parsed `data:` payload. */
export async function readEvents<T>(
  path: string,
  onEvent: (event: T) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(path, withToken({ signal }));
  if (!response.ok || !response.body) throw new ApiError(response.status, response.statusText);
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

export function openSocket(path: string, win: Window = window): WebSocket {
  const token = getToken(win) ?? "";
  const scheme = win.location.protocol === "https:" ? "wss" : "ws";
  return new WebSocket(`${scheme}://${win.location.host}${path}`, [
    WS_SUBPROTOCOL,
    `lectern.token.${token}`,
  ]);
}
