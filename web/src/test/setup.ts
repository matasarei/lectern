import { configure } from "@testing-library/react";
import { beforeAll } from "vitest";

// The Markdown component loads its module on first use (lazy-markdown.tsx). In a test that first
// render waited for the module, react-markdown and its plugins, to be compiled: under load (a CI
// runner, the other suite running beside it) longer than findBy*'s second, then longer than the
// test's own five. Loaded before each file's tests, with time of its own to load, the render
// finds it ready. In beforeAll, not at the top of this file: by then a test file's vi.mock of
// what it imports is in place (markdown.test.tsx mocks react-markdown).
beforeAll(async () => {
  await import("../markdown");
}, 60_000);

// How long findBy* and waitFor wait for what they look for, at most: they return as soon as it's
// there, so a passing test is no slower.
configure({ asyncUtilTimeout: 5000 });
