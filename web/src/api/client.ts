/**
 * Talking to the Lectern server.
 *
 * `lectern serve` opens the page as http://127.0.0.1:<port>/#token=<token>. The token is
 * moved into sessionStorage and removed from the address bar, then sent with every request:
 * as the X-Lectern-Token header over HTTP, and as the `lectern.token.<token>` subprotocol on
 * WebSockets (browsers can't set headers there). The server's event streams are followed on
 * WebSockets (`followEvents`).
 *
 * Where people sign in, the token is the login's own instead: it comes with the answer to
 * signing in, or to asking who this browser is (`./session`), and is kept the same way.
 */

const STORAGE_KEY = "lectern.token";
const TOKEN_HEADER = "X-Lectern-Token";
const WS_SUBPROTOCOL = "lectern.v1";
/** Signing in, and asking who this browser is. A 401 here is an answer, not a login lost. */
const SIGNING_IN = "/api/login";

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

export function setToken(token: string | null, win: Window = window): void {
  if (token === null) win.sessionStorage.removeItem(STORAGE_KEY);
  else win.sessionStorage.setItem(STORAGE_KEY, token);
}

let notSignedIn: () => unknown = () => {};
/** Done when the listener has dealt with the last 401 it was told of. */
let dealtWith: Promise<unknown> = Promise.resolve();

/** Have `listener` told whenever the server answers 401: the login ended, or the token is
 * wrong. One listener: whoever keeps track of the sign-in (`./session`), which asks the
 * server who the page is now. If it returns a promise, a stream that was refused waits for
 * it before giving up (`followEvents`). */
export function onNotSignedIn(listener: () => unknown): void {
  notSignedIn = listener;
}

function reportRefusal(path: string, response: Response): void {
  if (response.status !== 401 || path === SIGNING_IN) return;
  tellNotSignedIn();
}

function tellNotSignedIn(): void {
  dealtWith = Promise.resolve(notSignedIn()).catch(() => {});
}

/** Whether the page has another token than `had` once the listener has dealt with the last
 * 401: the login was replaced under this tab (signed in again, or a password changed, in
 * another tab of this browser), and a stream can go on with the new one. */
async function tokenReplaced(had: string | null): Promise<boolean> {
  await dealtWith;
  const now = getToken();
  return now !== null && now !== had;
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
    reportRefusal(path, response);
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
  if (!response.ok) {
    reportRefusal(path, response);
    throw new ApiError(response.status, response.statusText);
  }
  return response;
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/** An event the page couldn't take: its JSON didn't parse, or its handler threw. Following
 * the stream again would replay it, so a stream that hits one stops, as it always did. */
export class EventError extends Error {
  constructor(readonly reason: unknown) {
    super(`An event couldn't be handled: ${String(reason)}`);
  }
}

/** How long to wait before each reconnect: a restart takes a few seconds, a longer outage
 * isn't worth asking about every second. */
export const RECONNECT_DELAYS_MS = [1000, 2000, 5000, 10000];

/** Close codes 4000-4999 are the server's own: 4000 + the HTTP status it would have answered
 * (4404: the chat isn't there). */
const APP_CLOSE = 4000;

/** How one socket ended: closed by the server or lost (`code`), or given up on abort. */
type Ended = { code: number; reason: string } | "aborted";

/** Follow one event stream on a socket until it closes or `signal` aborts. Each frame is one
 * event as JSON. Rejects with EventError for an event the page can't take. */
function socketEvents<T>(
  path: string,
  onEvent: (event: T) => void,
  signal: AbortSignal,
  onOpen: () => void,
): Promise<Ended> {
  return new Promise((resolve, reject) => {
    const socket = openSocket(path);
    const end = () => {
      signal.removeEventListener("abort", abort);
      socket.onopen = socket.onmessage = socket.onclose = null;
    };
    const abort = () => {
      end();
      socket.close();
      resolve("aborted");
    };
    signal.addEventListener("abort", abort, { once: true });
    socket.onopen = onOpen;
    socket.onmessage = (message) => {
      try {
        onEvent(JSON.parse(String(message.data)) as T);
      } catch (error) {
        end();
        socket.close();
        reject(new EventError(error));
      }
    };
    socket.onclose = (event) => {
      end();
      resolve({ code: event.code, reason: event.reason });
    };
  });
}

/** A request the server answers at once to any page it lets in. */
const STILL_LET_IN = "/api/runtime";

/** The server's refusal of this page (a wrong token), or null when it lets the page in or
 * can't be reached. Asked over HTTP, where a refusal comes with a status. */
async function turnedAway(signal: AbortSignal): Promise<ApiError | null> {
  try {
    await apiFetch(STILL_LET_IN, { signal });
    return null;
  } catch (error) {
    const refused = error instanceof ApiError && (error.status === 401 || error.status === 403);
    return refused ? error : null;
  }
}

/** Follow one of the server's event streams until `signal` aborts. Over a WebSocket, not an
 * HTTP response: a browser allows six HTTP/1.1 connections to a host, shared by every tab,
 * and streams held open as responses used them up (two project tabs were enough, and every
 * other request then waited); sockets don't count against the six.
 *
 * When the socket closes or the server can't be reached (a restart, an update), wait and
 * open it again. `onConnect` runs each time it opens, before its first event, with `again`
 * true when it's a reopen (the time to catch up on what happened while it was closed): after
 * a restart the server's numbering starts again, so whatever a reader counted from the last
 * stream no longer applies. A close with the server's own code for an error answer (a session
 * that's gone: 4404) ends it, and so does an event the page can't take (EventError): a retry
 * wouldn't change either. The server's code for a 5xx is retried.
 *
 * The server closes an open socket as not signed in (4401) when the login it was opened with
 * has ended. That is said to whoever keeps track of the sign-in, as a 401 over HTTP is: the
 * page asks who it is now and shows the form, or has a new token and the stream goes on.
 *
 * A socket refused before it opened (a wrong token) looks to a browser like a server that is
 * down. So after a socket that never opened the server is asked over HTTP: refused there
 * too, the stream ends (a tab left open with an old token would otherwise knock every ten
 * seconds for good); let in, or no answer, it is tried again. One refusal isn't the end:
 * the login replaced under this tab (signed in again, or a password changed, in another tab
 * of this browser). Whoever keeps track of the sign-in has the new token once it has asked,
 * and the socket is opened again with it; the page is still there, and would otherwise go
 * on without its events until reloaded. */
export async function followEvents<T>(
  path: string,
  onEvent: (event: T) => void,
  signal: AbortSignal,
  onConnect: (again: boolean) => void = () => {},
): Promise<void> {
  let failures = 0;
  let opened = false;
  while (!signal.aborted) {
    let reached = false;
    const token = getToken();
    const ended = await socketEvents(path, onEvent, signal, () => {
      reached = true;
      failures = 0;
      onConnect(opened);
      opened = true;
    });
    if (ended === "aborted") return;
    const status = ended.code - APP_CLOSE;
    if (status === 401) {
      tellNotSignedIn();
      const replaced = await tokenReplaced(token);
      if (signal.aborted) return;
      if (replaced) continue; // at once, as below
    }
    if (status >= 0 && status < 500) throw new ApiError(status, ended.reason);
    if (!reached) {
      const refusal = await turnedAway(signal);
      if (signal.aborted) return;
      if (refusal) {
        const replaced = await tokenReplaced(token);
        if (signal.aborted) return;
        if (!replaced) throw refusal;
        continue; // a new token: at once, and it is refused in its turn if that one is wrong
      }
    }
    const delay = RECONNECT_DELAYS_MS[Math.min(failures, RECONNECT_DELAYS_MS.length - 1)];
    failures += 1;
    await new Promise<void>((resolve) => {
      const stop = () => {
        clearTimeout(timer);
        resolve();
      };
      // Removed when the wait ends: a long outage would otherwise leave one per attempt.
      const timer = setTimeout(() => {
        signal.removeEventListener("abort", stop);
        resolve();
      }, delay);
      signal.addEventListener("abort", stop, { once: true });
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
