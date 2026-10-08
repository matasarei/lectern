import "./i18n";

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Markdown } from "./markdown";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("Markdown from the model is kept harmless", () => {
  it("shows raw HTML as text, never as elements", () => {
    const { container } = render(
      <Markdown text={'<script>alert(1)</script> and <img src=x onerror="alert(2)">'} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
  });

  it("gives a javascript: or data: link no address", () => {
    const { container } = render(
      <Markdown text="[click](javascript:alert(1)) and [data](data:text/html,<b>x</b>)" />,
    );
    for (const link of container.querySelectorAll("a")) {
      const href = link.getAttribute("href") ?? "";
      expect(href).not.toMatch(/^(javascript|data):/i);
    }
    expect(container.textContent).toContain("click");
  });

  it("opens http links in a new tab without handing the page over", () => {
    render(<Markdown text="See [the docs](https://example.com/a)." />);
    const link = screen.getByRole("link", { name: "the docs" });
    expect(link.getAttribute("href")).toBe("https://example.com/a");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("doesn't load an image: it becomes a link with its alt text", () => {
    const { container } = render(<Markdown text="![a chart](https://example.com/c.png)" />);
    expect(container.querySelector("img")).toBeNull();
    const link = screen.getByRole("link", { name: /a chart/ });
    expect(link.getAttribute("href")).toBe("https://example.com/c.png");
  });
});

describe("Markdown renders what models write", () => {
  it("bold, a list and a table", () => {
    const { container } = render(
      <Markdown text={"**files** here\n\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |"} />,
    );
    expect(container.querySelector("strong")?.textContent).toBe("files");
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual(["one", "two"]);
    expect(screen.getByRole("table")).toBeTruthy();
    expect(screen.getAllByRole("cell").map((c) => c.textContent)).toEqual(["1", "2"]);
  });

  it("a fenced block shows its language and copies its code", async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const { container } = render(<Markdown text={"```python\ndef add(a, b):\n    return a + b\n```"} />);

    expect(container.querySelector("pre code")?.textContent).toBe("def add(a, b):\n    return a + b");
    expect(screen.getByText("python")).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Copy" })));
    expect(writeText).toHaveBeenCalledWith("def add(a, b):\n    return a + b");
    expect(screen.getByRole("button", { name: "Copied" })).toBeTruthy();
  });

  it("inline code stays inline, without a Copy button", () => {
    const { container } = render(<Markdown text="Run `uv sync` first." />);
    expect(container.querySelector("code")?.textContent).toBe("uv sync");
    expect(container.querySelector("pre")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("an unclosed fence, mid-stream, renders as an open code block", () => {
    const { container } = render(<Markdown text={"Here:\n\n```js\nconst a = 1;"} />);
    expect(container.querySelector("pre code")?.textContent).toContain("const a = 1;");
  });
});
