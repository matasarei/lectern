/** jsdom lays nothing out: gives a scrolling box the heights a test says, and keeps its
 * scrollTop within them as a browser would. Change `content` to make what's in it taller. */
export function fakeScrollBox(el: HTMLElement, content: number, height: number) {
  const box = { content, height, top: 0 };
  Object.defineProperty(el, "scrollHeight", { configurable: true, get: () => box.content });
  Object.defineProperty(el, "clientHeight", { configurable: true, get: () => box.height });
  Object.defineProperty(el, "scrollTop", {
    configurable: true,
    get: () => box.top,
    set: (v: number) => {
      box.top = Math.max(0, Math.min(v, box.content - box.height));
    },
  });
  return box;
}
