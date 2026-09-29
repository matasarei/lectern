/** Sizes as people read them: 2.7 GB, 850 MB. Uses the browser's number formatting. */
export function formatBytes(bytes: number, locale = "en"): string {
  const gb = bytes / 1e9;
  const [value, unit] = gb >= 1 ? [gb, "gigabyte"] : [bytes / 1e6, "megabyte"];
  return new Intl.NumberFormat(locale, {
    style: "unit",
    unit,
    maximumFractionDigits: value >= 100 ? 0 : 1,
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
