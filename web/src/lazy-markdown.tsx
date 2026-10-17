import { lazy, Suspense } from "react";

// The Markdown parser and its plugins are most of the page's weight: loaded when a first
// reply or file is shown, not with the page.
const Loaded = lazy(() => import("./markdown").then((m) => ({ default: m.Markdown })));

/** The Markdown component (./markdown), loaded on first use. Until then the text shows as
 * it is, so nothing waits on the download. */
export function Markdown({ text, breaks = false }: { text: string; breaks?: boolean }) {
  return (
    <Suspense fallback={<div className="text-sm leading-relaxed break-words whitespace-pre-wrap">{text}</div>}>
      <Loaded text={text} breaks={breaks} />
    </Suspense>
  );
}
