/** Sizes as people read them: 2.7 GB, 850 MB, 37 KB, 619 bytes. Uses the browser's
 * number formatting. */
export function formatBytes(bytes: number, locale = "en"): string {
  const [value, unit] =
    bytes >= 1e9
      ? [bytes / 1e9, "gigabyte"]
      : bytes >= 1e6
        ? [bytes / 1e6, "megabyte"]
        : bytes >= 1e3
          ? [bytes / 1e3, "kilobyte"]
          : [bytes, "byte"];
  return new Intl.NumberFormat(locale, {
    style: "unit",
    unit,
    unitDisplay: unit === "byte" ? "long" : "short",
    maximumFractionDigits: value >= 100 || unit === "byte" ? 0 : 1,
  }).format(value);
}

export function percent(done: number, total: number): number {
  return total > 0 ? Math.min(100, Math.floor((done / total) * 100)) : 0;
}

/** Memory (RAM, VRAM) in 1024-based units, labelled GB the way machines are sold and the
 * OS reports them: a 48 GB Mac shows 48 GB, not 51.5. */
export function formatMemory(bytes: number, locale = "en"): string {
  const gib = bytes / 1024 ** 3;
  return new Intl.NumberFormat(locale, {
    style: "unit",
    unit: "gigabyte",
    maximumFractionDigits: gib >= 10 ? 0 : 1,
  }).format(gib);
}

/** A running time on a clock's face: 0:07, 4:32, 1:02:03. */
export function formatElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const [h, m, sec] = [Math.floor(s / 3600), Math.floor((s % 3600) / 60), s % 60];
  const two = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${two(m)}:${two(sec)}` : `${m}:${two(sec)}`;
}

/** "2 minutes ago", "yesterday", in the browser's words. */
export function formatAgo(iso: string, now: Date = new Date(), locale = "en"): string {
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return "";
  const seconds = Math.round((time - now.getTime()) / 1000);
  const format = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const steps: [Intl.RelativeTimeFormatUnit, number][] = [
    ["second", 60],
    ["minute", 60],
    ["hour", 24],
    ["day", 30],
    ["month", 12],
  ];
  let value = seconds;
  for (const [unit, size] of steps) {
    if (Math.abs(value) < size) return format.format(value, unit);
    value = Math.round(value / size);
  }
  return format.format(value, "year");
}

/** A file's type as people say it: "PDF", "Markdown", "image". */
export function fileType(name: string, mime: string): string {
  if (mime === "text/markdown") return "Markdown";
  if (mime.startsWith("image/")) return "image";
  const ext = name.includes(".") ? name.split(".").pop()!.toUpperCase() : "";
  return ext || "file";
}

/** Token counts the way the context line shows them: 950, 5.1k, 12k. */
export function formatTokens(tokens: number): string {
  if (tokens < 1000) return String(Math.round(tokens));
  const k = tokens / 1000;
  return `${k >= 10 ? Math.round(k) : Math.round(k * 10) / 10}k`;
}
