import { describe, expect, it } from "vitest";

import { fileType, formatAgo, formatBytes, formatMemory, percent } from "./format";

describe("formatBytes", () => {
  it("uses GB from one gigabyte up", () => {
    expect(formatBytes(2_740_937_888)).toBe("2.7 GB");
    expect(formatBytes(22_020_000_000)).toBe("22 GB");
  });
  it("uses MB below a gigabyte", () => {
    expect(formatBytes(450_000_000)).toBe("450 MB");
  });
  it("uses KB and bytes for small files", () => {
    expect(formatBytes(37_060)).toBe("37.1 kB");
    expect(formatBytes(619)).toBe("619 bytes");
    expect(formatBytes(1)).toBe("1 byte");
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

describe("formatAgo", () => {
  const now = new Date("2026-09-29T12:00:00Z");
  it("says how long ago, in words", () => {
    expect(formatAgo("2026-09-29T11:58:00Z", now)).toBe("2 minutes ago");
    expect(formatAgo("2026-09-28T12:00:00Z", now)).toBe("yesterday");
    expect(formatAgo("2026-09-29T12:00:00Z", now)).toBe("now");
  });
  it("shows nothing for a date it can't read", () => {
    expect(formatAgo("", now)).toBe("");
  });
});

describe("fileType", () => {
  it("names common types", () => {
    expect(fileType("a.md", "text/markdown")).toBe("Markdown");
    expect(fileType("scan.pdf", "application/pdf")).toBe("PDF");
    expect(fileType("stamp.jpg", "image/jpeg")).toBe("image");
    expect(fileType("README", "text/plain")).toBe("file");
  });
});
