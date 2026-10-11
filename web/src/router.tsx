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

const passwordRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings/password",
  component: PasswordPage,
});

const projectsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/projects",
  component: ProjectsPage,
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
  projectsRoute,
  projectRoute,
]);
export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
