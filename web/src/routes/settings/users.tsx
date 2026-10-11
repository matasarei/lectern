import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, apiFetch, apiPost } from "../../api/client";
import { useIsAdmin, useSession } from "../../api/session";
import { Card, ErrorNote } from "../../components";

type Person = { login: string; role: "admin" | "member"; disabled: boolean };

const USERS = ["users"];
const FIELD = "rounded-md border border-line bg-surface px-3 py-1.5";

/** Users: who can sign in, and the admin's three things to do about it: add a person, give
 * someone a new password, disable an account. There is no sign-up and no removal: an account
 * that isn't wanted is disabled, and its projects stay on the disk. */
export function UsersPage() {
  const { t } = useTranslation();
  const session = useSession();
  const admin = useIsAdmin();
  if (!admin) {
    return (
      <Page>
        <p>{t("users.adminOnly")}</p>
      </Page>
    );
  }
  // Accounts are turned on at the server, where the first password is typed unseen.
  if (!session.accounts) {
    return (
      <Page>
        <p>{t("users.notSetUp")}</p>
      </Page>
    );
  }
  return (
    <Page>
      <People me={session.user?.login ?? null} />
      <AddPerson />
    </Page>
  );
}

function People({ me }: { me: string | null }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const people = useQuery({ queryKey: USERS, queryFn: () => apiFetch<Person[]>("/api/users") });
  // The one a new password is being typed for, and the one who last got one.
  const [asking, setAsking] = useState<string | null>(null);
  const [given, setGiven] = useState<string | null>(null);
  const setDisabled = useMutation({
    mutationFn: ({ login, disabled }: { login: string; disabled: boolean }) =>
      apiPost<Person>(`/api/users/${encodeURIComponent(login)}/disabled`, { disabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: USERS }),
  });

  if (people.isPending) return <p>{t("common.loading")}</p>;
  if (people.isError) return <ErrorNote message={whyNot(people.error, t("common.loadFailed"))} />;
  return (
    <Card title={t("users.people")}>
      <ul className="grid text-sm">
        {people.data.map((person) => (
          <li
            key={person.login}
            className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-line py-2 first:border-t-0"
          >
            <span className="min-w-0 flex-1">
              <span className="font-semibold">{person.login}</span>
              <span className="text-muted">
                {" · "}
                {t(person.role === "admin" ? "users.admin" : "users.member")}
                {person.disabled && ` · ${t("users.disabled")}`}
                {person.login === me && ` · ${t("users.you")}`}
              </span>
            </span>
            {/* One's own account isn't changed here: a new password or a disabled account
                signs that person out everywhere, and this is them. */}
            {person.login !== me && asking !== person.login && (
              <span className="flex gap-2">
                <button
                  className="btn-secondary"
                  aria-label={t("users.newPasswordFor", { name: person.login })}
                  onClick={() => {
                    setGiven(null);
                    setAsking(person.login);
                  }}
                >
                  {t("users.newPassword")}
                </button>
                <button
                  className="btn-secondary"
                  aria-label={t(person.disabled ? "users.enableUser" : "users.disableUser", {
                    name: person.login,
                  })}
                  disabled={setDisabled.isPending}
                  onClick={() =>
                    setDisabled.mutate({ login: person.login, disabled: !person.disabled })
                  }
                >
                  {t(person.disabled ? "users.enable" : "users.disable")}
                </button>
              </span>
            )}
            {asking === person.login && (
              <NewPassword
                login={person.login}
                onDone={(saved) => {
                  setAsking(null);
                  if (saved) setGiven(person.login);
                }}
              />
            )}
            {given === person.login && (
              <span className="w-full">{t("users.passwordSet", { name: person.login })}</span>
            )}
          </li>
        ))}
      </ul>
      {setDisabled.error && (
        <ErrorNote message={whyNot(setDisabled.error, t("common.loadFailed"))} />
      )}
      <p className="text-sm text-muted">{t("users.disabledWhy")}</p>
    </Card>
  );
}

/** A new password for someone who forgot theirs, typed in their row. Shown as typed: the
 * admin has to pass it on, and a hidden field would have the browser offer to keep it as the
 * admin's own. */
function NewPassword({ login, onDone }: { login: string; onDone: (saved: boolean) => void }) {
  const { t } = useTranslation();
  const [password, setPassword] = useState("");
  const save = useMutation({
    mutationFn: () => apiPost(`/api/users/${encodeURIComponent(login)}/password`, { password }),
    onSuccess: () => onDone(true),
  });
  return (
    <form
      className="grid w-full gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (password) save.mutate();
      }}
    >
      <span className="flex flex-wrap items-center justify-end gap-2">
        <input
          className={`${FIELD} min-w-48 flex-1`}
          aria-label={t("users.newPasswordFor", { name: login })}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete="off"
          spellCheck={false}
          autoFocus
          required
        />
        <button className="btn-secondary" disabled={save.isPending}>
          {t("users.save")}
        </button>
        <button
          type="button"
          className="btn-secondary"
          disabled={save.isPending}
          onClick={() => onDone(false)}
        >
          {t("users.cancel")}
        </button>
      </span>
      {save.error && <ErrorNote message={whyNot(save.error, t("common.loadFailed"))} />}
    </form>
  );
}

function AddPerson() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [admin, setAdmin] = useState(false);
  const add = useMutation({
    mutationFn: () => apiPost<Person>("/api/users", { login, password, admin }),
    onSuccess: () => {
      setLogin("");
      setPassword("");
      setAdmin(false);
      return queryClient.invalidateQueries({ queryKey: USERS });
    },
  });
  return (
    <Card title={t("users.add")}>
      <form
        className="grid gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          if (login && password) add.mutate();
        }}
      >
        <label className="grid gap-1 text-sm">
          {t("users.name")}
          <input
            className={FIELD}
            value={login}
            onChange={(event) => setLogin(event.target.value)}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            required
          />
        </label>
        <p className="text-sm text-muted">{t("users.nameRule")}</p>
        <label className="grid gap-1 text-sm">
          {t("users.password")}
          <input
            className={FIELD}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            required
          />
        </label>
        <p className="text-sm text-muted">{t("users.passwordRule")}</p>
        <label className="flex gap-2 text-sm">
          <input
            type="checkbox"
            checked={admin}
            onChange={(event) => setAdmin(event.target.checked)}
          />
          {t("users.makeAdmin")}
        </label>
        {add.error && <ErrorNote message={whyNot(add.error, t("common.loadFailed"))} />}
        <button className="btn-primary w-fit" disabled={add.isPending}>
          {t("users.addButton")}
        </button>
      </form>
    </Card>
  );
}

function Page({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  return (
    <main className="mx-auto grid max-w-3xl gap-5 px-4 py-12">
      <header className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">{t("users.title")}</h1>
        <Link to="/projects" className="link-back">
          {t("project.back")}
        </Link>
      </header>
      {children}
    </main>
  );
}

function whyNot(error: Error, otherwise: string): string {
  return error instanceof ApiError ? error.detail : otherwise;
}
