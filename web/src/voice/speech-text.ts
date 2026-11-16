import type { Nodes, Parents, RootContent } from "mdast";
import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import { unified } from "unified";

/** The longest piece sent to be spoken at once (the server takes up to 1,000 characters). A
 * longer sentence is cut at a comma, or else a space, before this. */
export const MAX_SENTENCE = 400;

const parser = unified().use(remarkParse).use(remarkGfm);

/** What a reply says aloud, as sentences in order. Read from the Markdown the chat shows, the
 * way a person reads the page: headings, paragraphs, list items, quotes, and tables row by row
 * ("cell, cell, cell"); link text, an image's description and inline code are read; code
 * blocks and raw HTML (which the chat doesn't show either) are not. */
export function sentences(markdown: string): string[] {
  const blocks: string[] = [];
  collectBlocks(parser.parse(markdown).children, blocks);
  return blocks.flatMap(splitSentences).flatMap(cutLong);
}

function collectBlocks(nodes: RootContent[], out: string[]): void {
  for (const node of nodes) {
    switch (node.type) {
      case "heading":
      case "paragraph":
        out.push(inline(node));
        break;
      case "list":
      case "listItem":
      case "blockquote":
        collectBlocks(node.children, out);
        break;
      case "table":
        for (const row of node.children) {
          out.push(row.children.map(inline).filter(Boolean).join(", "));
        }
        break;
      default:
        break; // code blocks, raw HTML, rules, definitions, footnotes: not read
    }
  }
}

/** The words of an inline run: text, inline code, link text, image descriptions. */
function inline(node: Nodes): string {
  switch (node.type) {
    case "text":
    case "inlineCode":
      return node.value;
    case "image":
      return node.alt ?? "";
    case "break":
      return " ";
    case "html":
      return "";
    default:
      return "children" in node ? (node as Parents).children.map(inline).join("") : "";
  }
}

const segmenter = new Intl.Segmenter(undefined, { granularity: "sentence" });

function splitSentences(block: string): string[] {
  const text = block.replace(/\s+/g, " ").trim();
  return [...segmenter.segment(text)]
    .map((part) => part.segment.trim())
    .filter((part) => /[\p{L}\p{N}]/u.test(part)); // "—", "…" alone: nothing to say
}

function cutLong(sentence: string): string[] {
  const parts: string[] = [];
  let rest = sentence;
  while (rest.length > MAX_SENTENCE) {
    const head = rest.slice(0, MAX_SENTENCE);
    const comma = Math.max(head.lastIndexOf(", "), head.lastIndexOf("; "));
    const space = head.lastIndexOf(" ");
    const at = comma > MAX_SENTENCE / 2 ? comma + 1 : space > 0 ? space : MAX_SENTENCE;
    parts.push(rest.slice(0, at).trim());
    rest = rest.slice(at).trim();
  }
  if (rest) parts.push(rest);
  return parts;
}
