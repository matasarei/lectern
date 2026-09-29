import { describe, expect, it } from "vitest";

import { formatBytes, formatMemory, percent } from "./format";

describe("formatBytes", () => {
  it("uses GB from one gigabyte up", () => {
    expect(formatBytes(2_740_937_888)).toBe("2.7 GB");
    expect(formatBytes(22_020_000_000)).toBe("22 GB");
  });
  it("uses MB below a gigabyte", () => {
    expect(formatBytes(450_000_000)).toBe("450 MB");
  });
});

describe("percent", () => {
  it("is floored, capped and safe with a zero total", () => {
    expect(percent(999, 1000)).toBe(99);
    expect(percent(2000, 1000)).toBe(100);
    expect(percent(5, 0)).toBe(0);
  });
});

describe("formatMemory", () => {
  it("uses 1024-based units so machines show their sold size", () => {
    expect(formatMemory(48 * 1024 ** 3)).toBe("48 GB");
    expect(formatMemory(8188 * 1024 ** 2)).toBe("8 GB");
    expect(formatMemory(4096 * 1024 ** 2 - 0.6 * 1024 ** 3)).toBe("3.4 GB");
  });
});
