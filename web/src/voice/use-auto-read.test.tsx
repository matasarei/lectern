import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AUTO_READ_KEY, useAutoRead } from "./use-auto-read";

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("useAutoRead", () => {
  it("is off at first, and kept in this browser once switched on or off", () => {
    const { result } = renderHook(() => useAutoRead());
    expect(result.current[0]).toBe(false);

    act(() => result.current[1](true));
    expect(result.current[0]).toBe(true);
    expect(window.localStorage.getItem(AUTO_READ_KEY)).toBe("1");

    act(() => result.current[1](false));
    expect(window.localStorage.getItem(AUTO_READ_KEY)).toBeNull();
  });

  it("works for the page when the browser refuses storage", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    const { result } = renderHook(() => useAutoRead());
    expect(result.current[0]).toBe(false);

    act(() => result.current[1](true));
    expect(result.current[0]).toBe(true);
  });
});
