import { describe, expect, it } from "vitest";

import { MAX_SENTENCE, sentences } from "./speech-text";

describe("sentences", () => {
  it("reads paragraphs and headings sentence by sentence", () => {
    expect(sentences("# Totals\n\nThe sum is 59. It was computed.\n\nDone!")).toEqual([
      "Totals",
      "The sum is 59.",
      "It was computed.",
      "Done!",
    ]);
  });

  it("leaves code blocks out, and reads inline code", () => {
    const reply = "Run this:\n\n```python\nprint(1)\n```\n\nThen call `total()` again.";
    expect(sentences(reply)).toEqual(["Run this:", "Then call total() again."]);
  });

  it("reads raw HTML the way the chat shows it: the tags dropped, an inline tag's text kept", () => {
    expect(sentences("<div>hidden block</div>\n\nSome <b>bold</b> text.")).toEqual(["Some bold text."]);
  });

  it("reads a link's text and an image's description, not their addresses", () => {
    expect(sentences("See [the lease](https://example.com/lease.pdf) and ![a signed page](p.png).")).toEqual([
      "See the lease and a signed page.",
    ]);
  });

  it("reads each list item and quote", () => {
    expect(sentences("- First point\n- Second point\n\n> Quoted line")).toEqual([
      "First point",
      "Second point",
      "Quoted line",
    ]);
  });

  it("reads a table row by row, its cells joined", () => {
    const table = "| Month | Rent |\n|---|---|\n| May | 900 |\n| June | 950 |";
    expect(sentences(table)).toEqual(["Month, Rent", "May, 900", "June, 950"]);
  });

  it("reads Cyrillic text", () => {
    expect(sentences("Привет! Как дела? Всё хорошо.")).toEqual(["Привет!", "Как дела?", "Всё хорошо."]);
  });

  it("cuts a long sentence at a comma, every piece within the limit", () => {
    const clause = "a clause of several words that goes on";
    const long = `${Array.from({ length: 30 }, () => clause).join(", ")}.`;
    const parts = sentences(long);
    expect(parts.length).toBeGreaterThan(1);
    expect(parts.every((p) => p.length <= MAX_SENTENCE)).toBe(true);
    expect(parts[0].endsWith(",")).toBe(true);
    expect(parts.join(" ")).toBe(long);
  });

  it("cuts a long run with no spaces at the limit", () => {
    const parts = sentences("x".repeat(MAX_SENTENCE * 2 + 10));
    expect(parts.map((p) => p.length)).toEqual([MAX_SENTENCE, MAX_SENTENCE, 10]);
  });

  it("has nothing to say for a reply of only code, rules or punctuation", () => {
    expect(sentences("```\nx = 1\n```\n\n---\n\n…")).toEqual([]);
    expect(sentences("")).toEqual([]);
  });
});
