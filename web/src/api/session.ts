/**
 * Who this page is.
 *
 * An install where nobody signs in is opened with its link: the page is that computer's own
 * user, and nothing here changes what it does. Where people sign in, the browser holds a
 * cookie naming its login, and the page asks who that is (`GET /api/login`): the answer also
 * brings the login's token, which a new tab doesn't have yet.
 *
 * The server decides who may do what. This is for what the page shows: the sign-in form, the
 * person's name, and no buttons for what only an admin can do.
 */

import type { QueryClient } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";

import { ApiError, apiFetch, getToken, onNotSignedIn, setToken } from "./client";

export type Who = {
  /** The sign-in name. None for the page opened with the computer's own link. */
  login: string | null;
  role: "admin" | "member";
};

export type Session = {
  /** Whether people sign in here. */
  accounts: boolean;
  /** Nobody: not signed in, or the server didn't say. */
  user: Who | null;
};

type Answer = Session & { token: string | null };

const NOBODY: Session = { accounts: false, user: null };

let session = NOBODY;
let known = false; // whether the server has been asked and answered
let asking: Promise<Session> | null = null;
const listeners = new Set<() => void>();

export function getSession(): Session {
  return session;
}

export function setSession(next: Session): void {
  session = next;
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useSession(): Session {
  return useSyncExternalStore(subscribe, getSession);
}

/** Whether the server has said who this page is. Until it has, nobody being signed in says
 * nothing: not even whether people sign in here. */
export function useAnswered(): boolean {
  return useSyncExternalStore(subscribe, () => known);
}

/** Whether the page shows what only an admin can do. A page that doesn't know who it is
 * shows it, as before accounts: the server refuses what isn't allowed. */
export function useIsAdmin(): boolean {
  return useSession().user?.role !== "member";
}

/** Who this page is, asked once. Before the first page is shown. */
export function whoAmI(): Promise<Session> {
  return known ? Promise.resolve(session) : askAgain();
}

/** Ask the server who this page is now. One question at a time, however many ask. */
export function askAgain(): Promise<Session> {
  asking ??= ask().finally(() => {
    asking = null;
  });
  return asking;
}

async function ask(): Promise<Session> {
  let answer: Answer;
  try {
    answer = await apiFetch<Answer>("/api/login");
  } catch (error) {
    // Refused: nobody signs in here, and the link's token is missing or wrong. Anything
    // else (no answer, a server error) says nothing about who this is: nobody is signed out
    // over it, and the next page asks again.
    if (error instanceof ApiError && error.status === 401) take(NOBODY, null);
    return session;
  }
  take({ accounts: answer.accounts, user: answer.user }, answer.token);
  return session;
}

/** The server's answer. Listeners are told only of a change: a page that fetches again when
 * told would otherwise ask, be refused and be told again, for as long as the server refused
 * it. The first answer is a change even when it says what was assumed: it is now known. */
function take(next: Session, token: string | null): void {
  const first = !known;
  known = true;
  const ended = session.user !== null && next.user === null;
  const newToken = token !== null && token !== getToken();
  if (newToken) setToken(token);
  if (first || newToken || JSON.stringify(next) !== JSON.stringify(session)) setSession(next);
  // Ended somewhere else (signed out in another browser, a new password, disabled): this tab
  // found out by asking, and one with nothing to ask goes on showing the person's page.
  if (ended) tabs?.postMessage(CHANGED);
}

/** Signed in at the form: who, and the token of the new login. */
export function signedIn(answer: { user: Who; token: string }): void {
  known = true;
  setToken(answer.token);
  setSession({ accounts: true, user: answer.user });
  tabs?.postMessage(CHANGED);
}

/** Signed out here: the page returns to the form. */
export function signedOut(): void {
  setToken(null);
  setSession({ accounts: true, user: null });
  tabs?.postMessage(CHANGED);
}

const CHANGED = "changed"; // all a tab says: who it is now is the server's to tell
let tabs: BroadcastChannel | null = null;

/** The tabs of a browser share its cookie. When one signs in or out, or learns from the
 * server that its login has ended, it tells the others, and each asks again who it is: left
 * alone, a tab goes on showing the last person's page until its next request. A tab told so
 * that finds itself nobody says it in its turn, once: after that nothing changes, and
 * nothing more is said. Returns how to stop. */
export function followOtherTabs(): () => void {
  if (typeof BroadcastChannel === "undefined") return () => {};
  const channel = new BroadcastChannel("lectern.session");
  channel.addEventListener("message", () => void askAgain());
  tabs = channel;
  return () => {
    channel.close();
    if (tabs === channel) tabs = null;
  };
}

/** As when the page was opened: nobody, and not asked yet. For the tests. */
export function forgetSession(): void {
  known = false;
  session = NOBODY;
}

/** Keep what the page has fetched in step with who it is. When nobody is signed in any more
 * it is dropped, so the next person doesn't see it; when the person or the token changed, it
 * is dropped and what is on screen fetched again. Returns how to stop. */
export function keepInStep(queries: QueryClient): () => void {
  return subscribe(() => {
    if (session.user === null) queries.clear();
    else void queries.resetQueries();
  });
}

onNotSignedIn(askAgain);
