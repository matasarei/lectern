import { createRootRoute, createRoute, createRouter, Outlet } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { getToken } from "./api/client";

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

function Home() {
  const { t } = useTranslation();
  return (
    <main className="mx-auto max-w-xl px-4 py-16">
      <h1 className="text-2xl font-semibold">{t("app.name")}</h1>
      <p className="mt-3 text-muted">{t("app.tagline")}</p>
    </main>
  );
}

const rootRoute = createRootRoute({ component: Shell });
const homeRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: Home });

export const routeTree = rootRoute.addChildren([homeRoute]);
export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
