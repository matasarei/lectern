import { describe, expect, it, vi } from "vitest";

import type { Artifact } from "../api/types";
import { listKey } from "./file-picker";

const file = (id: number) => ({ id, name: `f${id}.md` }) as Artifact;

describe("listKey", () => {
  it("never moves the active option below the first, even with nothing listed", () => {
    const setActive = vi.fn();
    listKey("ArrowDown", [], 0, setActive, vi.fn(), vi.fn());
    listKey("ArrowUp", [file(1)], 0, setActive, vi.fn(), vi.fn());
    expect(setActive.mock.calls).toEqual([[0], [0]]);
  });

  it("stops at the last option", () => {
    const setActive = vi.fn();
    listKey("ArrowDown", [file(1), file(2)], 1, setActive, vi.fn(), vi.fn());
    expect(setActive).toHaveBeenCalledWith(1);
  });
});
