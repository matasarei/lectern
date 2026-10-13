import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  redirect,
} from "@tanstack/react-router";

import { apiFetch, getToken } from "./api/client";
import { getSession, whoAmI } from "./api/session";
import type { FirstRun } from "./api/types";
import { FirstRunPage } from "./routes/first-run";
import { PasswordPage, SignedIn } from "./routes/login";
import { ProjectPage } from "./routes/project";
import { ProjectsPage } from "./routes/projects";
import { ModelsPage } from "./routes/settings/models";
import { UsersPage } from "./routes/settings/users";

function Shell() {
  return (
    <SignedIn>
      <Outlet />
    </SignedIn>
  );
}

const rootRoute = createRootRoute({
  component: Shell,
  // Who the page is, before any of it is shown: a new tab gets its token here.
  beforeLoad: async () => {
    await whoAmI();
  },
});

// "/" goes to first run until it's finished, then to the projects.
const homeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: async () => {
    const { accounts, user } = getSession();
    // Nobody to lead anywhere: the page says how to get in, or asks to sign in.
    if (accounts ? !user : !getToken()) return;
    const run = await apiFetch<FirstRun>("/api/first-run");
    throw redirect({ to: run.step === "done" ? "/projects" : "/first-run" });
  },
});
const firstRunRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/first-run",
  component: FirstRunPage,
});
const modelsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings/models",
  component: ModelsPage,
});

const usersRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings/users",
  component: UsersPage,
});
const passwordRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings/password",
  component: PasswordPage,
});

const projectsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/projects",
  component: ProjectsPage,
  // An admin who signs in here before setup is done is led to it, as "/" would: the empty
  // list says nothing about models still to choose. Setup isn't a member's, and where nobody
  // signs in the computer's link opens "/".
  beforeLoad: async () => {
    const { accounts, user } = getSession();
    if (!accounts || user?.role !== "admin") return;
    // If the server can't say how far setup is, the projects are still worth showing.
    const run = await apiFetch<FirstRun>("/api/first-run").catch(() => null);
    if (run && run.step !== "done") throw redirect({ to: "/first-run" });
  },
});
const projectRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/projects/$projectId",
  component: ProjectPage,
});

export const routeTree = rootRoute.addChildren([
  homeRoute,
  firstRunRoute,
  modelsRoute,
  passwordRoute,
  usersRoute,
  projectsRoute,
  projectRoute,
]);
export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
