import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  redirect,
} from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { apiFetch, getToken } from "./api/client";
import type { FirstRun } from "./api/types";
import { FirstRunPage } from "./routes/first-run";
import { ModelsPage } from "./routes/settings/models";

function Shell() {
  const { t } = useTranslation();
  if (!getToken()) {
    return (
      <main className="mx-auto max-w-xl px-4 py-16">
        <h1 className="text-2xl font-semibold">{t("app.name")}</h1>
        <p className="mt-3 text-muted">{t("errors.noToken")}</p>
      </main>
    );
  }
  return <Outlet />;
}

const rootRoute = createRootRoute({ component: Shell });

// "/" goes to first run until it's finished, then to Models & memory (M1 replaces this
// with the projects screen).
const homeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: async () => {
    if (!getToken()) return;
    const run = await apiFetch<FirstRun>("/api/first-run");
    throw redirect({ to: run.step === "done" ? "/settings/models" : "/first-run" });
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

export const routeTree = rootRoute.addChildren([homeRoute, firstRunRoute, modelsRoute]);
export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
