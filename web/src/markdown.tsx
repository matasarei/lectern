import { memo, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/** How long a Copy button says "Copied". */
const COPIED_MS = 1500;

/** Only addresses a click can't turn against the page: no `javascript:`, no `data:`, nothing
 * relative to Lectern's own API. Anything else loses its address and shows as plain text. */
export function safeUrl(url: string): string | undefined {
  return /^(https?:|mailto:)/i.test(url.trim()) ? url : undefined;
}

/** The text inside a hast element (a code block's code), for the Copy button. */
type HastNode = { type: string; value?: string; children?: HastNode[] };
function textOf(node: HastNode | undefined): string {
  if (!node) return "";
  if (node.type === "text") return node.value ?? "";
  return (node.children ?? []).map(textOf).join("");
}

/** Copies `text`: through the clipboard API where the browser offers it (https, localhost),
 * else by selecting it in a hidden field, which also works over plain http on the LAN. */
async function copyText(text: string): Promise<void> {
  if (navigator.clipboard) return navigator.clipboard.writeText(text);
  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.append(field);
  field.select();
  try {
    // deprecated, but the only way to copy where the clipboard API isn't offered
    if (!document.execCommand("copy")) throw new Error("copy refused");
  } finally {
    field.remove();
  }
}

function CodeBlock({ code, language }: { code: string; language: string | null }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState<"copied" | "failed" | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    try {
      await copyText(code);
      setCopied("copied");
    } catch {
      setCopied("failed");
    }
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(null), COPIED_MS);
  };
  return (
    <div className="my-2 overflow-hidden rounded-md border border-line bg-paper">
      <div className="flex items-center gap-2 border-b border-line px-3 py-1 text-xs text-muted">
        {language && <span>{language}</span>}
        <button className="ml-auto hover:text-ink" onClick={() => void copy()}>
          {copied === "copied"
            ? t("markdown.copied")
            : copied === "failed"
              ? t("markdown.copyFailed")
              : t("markdown.copy")}
        </button>
      </div>
      <pre className="overflow-x-auto px-3 py-2 font-mono text-xs leading-relaxed">
        <code>{code}</code>
      </pre>
    </div>
  );
}

const components: Components = {
  pre: ({ node }) => {
    const code = node?.children[0] as (HastNode & { properties?: { className?: string[] } }) | undefined;
    const language = code?.properties?.className
      ?.find((c) => c.startsWith("language-"))
      ?.slice("language-".length);
    return <CodeBlock code={textOf(code).replace(/\n$/, "")} language={language ?? null} />;
  },
  code: ({ children }) => (
    <code className="rounded bg-paper px-1 py-0.5 font-mono text-[0.85em]">{children}</code>
  ),
  a: ({ href, children }) =>
    href ? (
      <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent underline">
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
  img: ImageLink,
  h1: ({ children }) => <h3 className="mt-3 mb-1 text-base font-semibold">{children}</h3>,
  h2: ({ children }) => <h3 className="mt-3 mb-1 text-base font-semibold">{children}</h3>,
  h3: ({ children }) => <h4 className="mt-3 mb-1 text-sm font-semibold">{children}</h4>,
  h4: ({ children }) => <h4 className="mt-2 mb-1 text-sm font-semibold">{children}</h4>,
  h5: ({ children }) => <h4 className="mt-2 mb-1 text-sm font-semibold">{children}</h4>,
  h6: ({ children }) => <h4 className="mt-2 mb-1 text-sm font-semibold">{children}</h4>,
  p: ({ children }) => <p className="my-1.5">{children}</p>,
  ul: ({ children }) => <ul className="my-1.5 list-disc pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="my-1.5 list-decimal pl-5">{children}</ol>,
  li: ({ children }) => <li className="my-0.5">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-line pl-3 text-muted">{children}</blockquote>
  ),
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto">
      <table className="border-collapse text-left">{children}</table>
    </div>
  ),
  th: ({ children }) => <th className="border border-line bg-paper px-2 py-1 font-semibold">{children}</th>,
  td: ({ children }) => <td className="border border-line px-2 py-1">{children}</td>,
  hr: () => <hr className="my-3 border-line" />,
};

/** An image in a reply is never loaded (that would be a request nobody chose): it shows as
 * a link to it, named by its alt text. */
function ImageLink({ src, alt }: { src?: string | Blob; alt?: string }) {
  const { t } = useTranslation();
  const label = t("markdown.image", { alt: alt || src || "" });
  if (typeof src !== "string" || !src) return <span>{label}</span>;
  return (
    <a href={src} target="_blank" rel="noopener noreferrer" className="text-accent underline">
      {label}
    </a>
  );
}

/** Text the model wrote, as Markdown (GitHub's flavour: tables, task lists, strikethrough).
 * Raw HTML in it stays text, links keep only http(s) and mailto addresses, and images aren't
 * fetched. Memoised: every streamed token re-renders the chat, and parsing each stored reply
 * again for it would make a long chat stutter. */
export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="text-sm leading-relaxed break-words">
      <ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={safeUrl} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
});
