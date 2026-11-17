import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { fakeScrollBox } from "../test/scroll-box";
import { useStickToBottom } from "./useStickToBottom";

function Box({ lines, onFollow }: { lines: string[]; onFollow?: (follow: () => void) => void }) {
  const { ref, follow } = useStickToBottom<HTMLDivElement>();
  onFollow?.(follow);
  return (
    <div ref={ref} data-testid="box">
      {lines.map((line, i) => (
        <p key={i}>{line}</p>
      ))}
    </div>
  );
}

/** Mounts the box, gives it geometry, and returns it with a way to grow it. */
async function mount(onFollow?: (follow: () => void) => void) {
  const view = render(<Box lines={["one"]} onFollow={onFollow} />);
  const el = view.getByTestId("box");
  const box = fakeScrollBox(el, 1000, 300);
  el.scrollTop = 700; // at the bottom, as the hook leaves a box it has just been given
  const grow = async (lines: string[], content: number) => {
    box.content = content;
    await act(async () => view.rerender(<Box lines={lines} onFollow={onFollow} />));
  };
  return { el, box, grow };
}

afterEach(cleanup);

describe("useStickToBottom", () => {
  it("follows what's added while the box is at its bottom", async () => {
    const { box, grow } = await mount();
    await grow(["one", "two"], 1400);
    expect(box.top).toBe(1100);
  });

  it("follows text growing inside what's there, as a streamed reply does", async () => {
    const { el, box } = await mount();
    box.content = 1250;
    await act(async () => {
      el.querySelector("p")!.firstChild!.textContent = "one, and more";
    });
    expect(box.top).toBe(950);
  });

  it("leaves the person where they scrolled up to", async () => {
    const { el, box, grow } = await mount();
    el.scrollTop = 200;
    el.dispatchEvent(new Event("scroll"));
    await grow(["one", "two"], 1400);
    expect(box.top).toBe(200);
  });

  it("follows again once they scroll back to the bottom", async () => {
    const { el, box, grow } = await mount();
    el.scrollTop = 200;
    el.dispatchEvent(new Event("scroll"));
    el.scrollTop = 690; // near enough
    el.dispatchEvent(new Event("scroll"));
    await grow(["one", "two"], 1400);
    expect(box.top).toBe(1100);
  });

  it("follows again when asked to, wherever they were", async () => {
    let follow = () => {};
    const { el, box, grow } = await mount((f) => (follow = f));
    el.scrollTop = 200;
    el.dispatchEvent(new Event("scroll"));
    act(() => follow());
    expect(box.top).toBe(700);
    await grow(["one", "two"], 1400);
    expect(box.top).toBe(1100);
  });

  it("follows an image that loads after it was added", async () => {
    const { el, box } = await mount();
    const img = document.createElement("img");
    await act(async () => el.appendChild(img));
    box.content = 1600;
    img.dispatchEvent(new Event("load"));
    expect(box.top).toBe(1300);
  });
});
