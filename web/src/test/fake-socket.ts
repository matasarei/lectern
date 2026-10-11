import { vi } from "vitest";

/** A WebSocket for tests: the page's event streams are sockets (see `followEvents`). A test
 * plays the server: it opens the socket, pushes events, and closes it with a code. */
export class FakeSocket {
  readyState = 0; // CONNECTING
  onopen: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  /** The page closed it (a component unmounted, a stream was given up). */
  closedByPage = false;
  readonly path: string;

  constructor(
    readonly url: string,
    readonly protocols: string[] = [],
  ) {
    // Not `new URL`: a test may have replaced URL with a stub.
    this.path = url.replace(/^wss?:\/\/[^/]+/, "");
  }

  /** The server accepted it. */
  open(): void {
    if (this.readyState !== 0) return;
    this.readyState = 1;
    this.onopen?.();
  }

  /** The server sent these events, one frame each. */
  push(...events: object[]): void {
    for (const event of events) this.onmessage?.({ data: JSON.stringify(event) });
  }

  /** The server sent a frame that isn't JSON. */
  pushText(data: string): void {
    this.onmessage?.({ data });
  }

  /** The server closed it, or went away (1006: what a browser reports for a lost or refused
   * connection). */
  drop(code = 1006, reason = ""): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.onclose?.({ code, reason });
  }

  close(): void {
    this.closedByPage = true;
    this.readyState = 3;
  }
}

export interface FakeSockets {
  /** Every socket the page opened, in order. */
  all: FakeSocket[];
  /** The sockets opened to a path (`/api/runtime/events`), in order. */
  to(path: string): FakeSocket[];
  /** The latest socket opened to a path; throws when there is none. */
  last(path: string): FakeSocket;
}

/** Replace `WebSocket` until `vi.unstubAllGlobals()`. `serve` plays the server for each new
 * socket, once the page has attached its handlers; left out, every socket is accepted and
 * stays open, silent. */
export function installFakeSockets(
  serve: (socket: FakeSocket, index: number) => void = (socket) => socket.open(),
): FakeSockets {
  const all: FakeSocket[] = [];
  vi.stubGlobal(
    "WebSocket",
    class extends FakeSocket {
      constructor(url: string, protocols?: string[]) {
        super(url, protocols);
        const index = all.push(this) - 1;
        queueMicrotask(() => serve(this, index));
      }
    },
  );
  const to = (path: string) => all.filter((socket) => socket.path === path);
  return {
    all,
    to,
    last(path) {
      const found = to(path).at(-1);
      if (!found) throw new Error(`No socket was opened to ${path}`);
      return found;
    },
  };
}
