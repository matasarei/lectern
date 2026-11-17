import { useCallback, useRef } from "react";

/** How far from the bottom still counts as at it, in pixels: a nudge of the wheel, or the last
 * line half shown, shouldn't stop the box following. */
const NEAR_BOTTOM = 48;

/** Keeps a scrolling box at its bottom while what's in it grows (a reply streaming in, a card
 * asking something, an image loading), unless the person has scrolled up to read: then it
 * leaves them where they are until they scroll back down, or `follow` is called. */
export function useStickToBottom<T extends HTMLElement>(): {
  ref: (el: T | null) => (() => void) | undefined;
  follow: () => void;
} {
  const stick = useRef(true);
  const box = useRef<T | null>(null);

  const toBottom = useCallback(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, []);

  const ref = useCallback(
    (el: T | null) => {
      box.current = el;
      if (!el) return undefined;
      stick.current = true;
      toBottom();
      const onScroll = () => {
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM;
      };
      el.addEventListener("scroll", onScroll);
      // An image grows the box when it loads, which changes nothing in the DOM; load doesn't
      // bubble, so it's caught on the way down.
      el.addEventListener("load", toBottom, true);
      const added = new MutationObserver(toBottom);
      added.observe(el, { childList: true, subtree: true, characterData: true });
      // The box itself getting shorter (the message box growing under it, the window resized).
      const resized = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(toBottom);
      resized?.observe(el);
      return () => {
        el.removeEventListener("scroll", onScroll);
        el.removeEventListener("load", toBottom, true);
        added.disconnect();
        resized?.disconnect();
        box.current = null;
      };
    },
    [toBottom],
  );

  const follow = useCallback(() => {
    stick.current = true;
    toBottom();
  }, [toBottom]);

  return { ref, follow };
}
