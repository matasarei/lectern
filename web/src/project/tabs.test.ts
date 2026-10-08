import { describe, expect, it } from "vitest";

import { addTab, openTab } from "./tabs";

const chat = { kind: "session" as const, id: 1 };
const ocr = { kind: "session" as const, id: 2 };

describe("addTab", () => {
  it("adds a tab and keeps the one showing", () => {
    expect(addTab({ open: [chat], active: chat }, ocr)).toEqual({ open: [chat, ocr], active: chat });
    expect(addTab({ open: [], active: null }, ocr)).toEqual({ open: [ocr], active: null });
  });

  it("leaves the tabs as they are when it's already open", () => {
    const tabs = { open: [chat, ocr], active: chat };
    expect(addTab(tabs, ocr)).toBe(tabs);
  });

  it("differs from openTab, which shows it", () => {
    expect(openTab({ open: [chat], active: chat }, ocr).active).toEqual(ocr);
  });
});
