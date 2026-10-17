import type { Tab, Tabs } from "../api/types";

/** The tab bar's rules. Overview is not a tab here: it's always first, can't be closed,
 * and `active: null` means it's showing. */

export function sameTab(a: Tab | null, b: Tab | null): boolean {
  return a !== null && b !== null && a.kind === b.kind && a.id === b.id;
}

/** A tab as the bar keeps it: an OCR tab's page is for the one time it's opened at it, not
 * for every time it's shown again from the bar. */
function kept(tab: Tab): Tab {
  return { kind: tab.kind, id: tab.id };
}

export function openTab(tabs: Tabs, tab: Tab): Tabs {
  const open = tabs.open.some((t) => sameTab(t, tab)) ? tabs.open : [...tabs.open, kept(tab)];
  return { open, active: tab };
}

/** Adds a tab without showing it: something the chat started, while the person stays put. */
export function addTab(tabs: Tabs, tab: Tab): Tabs {
  return tabs.open.some((t) => sameTab(t, tab)) ? tabs : { ...tabs, open: [...tabs.open, kept(tab)] };
}

/** Closing the active tab shows its right-hand neighbour, else the left, else Overview. */
export function closeTab(tabs: Tabs, tab: Tab): Tabs {
  const index = tabs.open.findIndex((t) => sameTab(t, tab));
  if (index < 0) return tabs;
  const open = tabs.open.filter((_, i) => i !== index);
  if (!sameTab(tabs.active, tab)) return { open, active: tabs.active };
  return { open, active: open[index] ?? open[index - 1] ?? null };
}

export function showOverview(tabs: Tabs): Tabs {
  return { ...tabs, active: null };
}
