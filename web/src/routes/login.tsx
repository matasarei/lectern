import { useMutation } from "@tanstack/react-query";
import { Link, useNavigate, useRouter } from "@tanstack/react-router";
import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, apiPost, getToken, setToken } from "../api/client";
import { signedIn, signedOut, useSession, type Who } from "../api/session";
import { ErrorNote } from "../components";

const FIELD = "rounded-md border border-line bg-surface px-3 py-1.5";

/** The page, for whoever may see it. Where people sign in, that is someone signed in, and
 * anybody else gets the form in its place (at the same address, so signing in shows what
 * they came for). Where nobody signs in, it is whoever opened the computer's own link. */
export function SignedIn({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const session = useSession();
  if (session.accounts) return session.user ? <>{children}</> : <SignInPage />;
  if (!getToken()) {
    return (
      <main className="mx-auto max-w-xl px-4 py-16">
        <h1 className="text-2xl font-semibold">{t("app.name")}</h1>
        <p className="mt-3 text-muted">{t("errors.noToken")}</p>
      </main>
    );
  }
  return <>{children}</>;
}

function SignInPage() {
  const { t } = useTranslation();
  const router = useRouter();
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const signIn = useMutation({
    mutationFn: () => apiPost<{ user: Who; token: string }>("/api/login", { login, password }),
    onSuccess: (answer) => {
      signedIn(answer);
      // "/" leads somewhere only for someone signed in: have it decide again.
      void router.invalidate();
    },
    onError: () => setPassword(""),
  });
  return (
    <main className="mx-auto grid max-w-sm gap-5 px-4 py-16">
      <h1 className="text-2xl font-semibold">{t("signIn.title")}</h1>
      <form
        className="grid gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          if (login && password) signIn.mutate();
        }}
      >
        <label className="grid gap-1 text-sm">
          {t("signIn.name")}
          <input
            className={FIELD}
            value={login}
            onChange={(event) => setLogin(event.target.value)}
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            autoFocus
            required
          />
        </label>
        <label className="grid gap-1 text-sm">
          {t("signIn.password")}
          <input
            className={FIELD}
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {signIn.error && <ErrorNote message={whyNot(signIn.error, t("common.loadFailed"))} />}
        <button className="btn-primary w-fit" disabled={signIn.isPending}>
          {t("signIn.submit")}
        </button>
      </form>
      <p className="text-sm text-muted">{t("signIn.noSignUp")}</p>
    </main>
  );
}

/** Who is signed in, with Change password and Sign out. Nothing where nobody signs in. */
export function AccountBar() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const session = useSession();
  // Signed out here only once the server has ended the login: the cookie is its to remove,
  // and with it left a reload would bring the person back for whoever sits down next.
  const signOut = useMutation({
    mutationFn: () => apiPost("/api/logout"),
    onSuccess: () => {
      signedOut();
      void navigate({ to: "/" });
    },
  });
  if (!session.accounts || !session.user) return null;
  const { login } = session.user;
  if (login === null) return <p className="text-sm text-muted">{t("account.thisComputer")}</p>;
  return (
    <div className="grid gap-2">
      <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted">
        <span>{t("account.signedInAs", { name: login })}</span>
        <Link to="/settings/password" className="font-semibold text-accent hover:underline">
          {t("account.changePassword")}
        </Link>
        <button
          className="font-semibold text-accent hover:underline"
          disabled={signOut.isPending}
          onClick={() => signOut.mutate()}
        >
          {t("account.signOut")}
        </button>
      </p>
      {signOut.error && <ErrorNote message={t("account.signOutFailed")} />}
    </div>
  );
}

/** One's own password. The server ends every login of theirs and gives this browser a new
 * one, so the page carries on with the new token. */
export function PasswordPage() {
  const { t } = useTranslation();
  const [current, setCurrent] = useState("");
  const [fresh, setFresh] = useState("");
  const [again, setAgain] = useState("");
  const [differ, setDiffer] = useState(false);
  const change = useMutation({
    mutationFn: () => apiPost<{ token: string }>("/api/password", { current, new: fresh }),
    onSuccess: ({ token }) => {
      setToken(token);
      setCurrent("");
      setFresh("");
      setAgain("");
    },
  });
  const field = (label: string, value: string, set: (value: string) => void, isNew: boolean) => (
    <label className="grid gap-1 text-sm">
      {label}
      <input
        className={FIELD}
        type="password"
        value={value}
        onChange={(event) => set(event.target.value)}
        autoComplete={isNew ? "new-password" : "current-password"}
        required
      />
    </label>
  );
  return (
    <main className="mx-auto grid max-w-sm gap-5 px-4 py-12">
      <header className="grid gap-2">
        <Link to="/projects" className="link-back">
          {t("project.back")}
        </Link>
        <h1 className="text-2xl font-semibold">{t("account.passwordTitle")}</h1>
      </header>
      <form
        className="grid gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          change.reset();
          setDiffer(fresh !== again);
          if (current && fresh && fresh === again) change.mutate();
        }}
      >
        {field(t("account.current"), current, setCurrent, false)}
        {field(t("account.new"), fresh, setFresh, true)}
        {field(t("account.again"), again, setAgain, true)}
        <p className="text-sm text-muted">{t("account.passwordRule")}</p>
        {differ && <ErrorNote message={t("account.differ")} />}
        {change.error && <ErrorNote message={whyNot(change.error, t("common.loadFailed"))} />}
        {change.isSuccess && <p className="text-sm">{t("account.changed")}</p>}
        <button className="btn-primary w-fit" disabled={change.isPending}>
          {t("account.save")}
        </button>
      </form>
    </main>
  );
}

function whyNot(error: Error, otherwise: string): string {
  return error instanceof ApiError ? error.detail : otherwise;
}
