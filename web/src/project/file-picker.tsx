import { useTranslation } from "react-i18next";

import type { Artifact } from "../api/types";

/** How many matches the list shows: enough to pick from, short enough to read. */
export const PICKER_LIMIT = 8;

/** The project's files whose name contains `query` (any case), minus those already chosen. */
export function matchingFiles(files: Artifact[], query: string, exclude: number[]): Artifact[] {
  const wanted = query.toLowerCase();
  return files
    .filter((f) => !exclude.includes(f.id) && f.name.toLowerCase().includes(wanted))
    .slice(0, PICKER_LIMIT);
}

/** The list a file is picked from, for "From project" and for "@" in the message box. The
 * keyboard stays where the person types (the box, or the picker's search field), so the
 * list only shows which one is active and takes clicks. */
export function FileList({
  files,
  active,
  onPick,
  loading,
}: {
  files: Artifact[];
  active: number;
  onPick: (file: Artifact) => void;
  loading: boolean;
}) {
  const { t } = useTranslation();
  return (
    <ul
      role="listbox"
      aria-label={t("chat.pickerLabel")}
      className="grid max-h-56 overflow-y-auto rounded-md border border-line bg-surface text-sm shadow"
    >
      {files.length === 0 && (
        <li className="px-3 py-1.5 text-muted">
          {loading ? t("common.loading") : t("chat.pickerEmpty")}
        </li>
      )}
      {files.map((file, i) => (
        <li
          key={file.id}
          role="option"
          aria-selected={i === active}
          className={`cursor-pointer px-3 py-1.5 ${i === active ? "bg-paper" : ""}`}
          // mousedown, not click: the box keeps its focus (and its caret) while picking
          onMouseDown={(e) => {
            e.preventDefault();
            onPick(file);
          }}
        >
          {file.name}
        </li>
      ))}
    </ul>
  );
}

/** Arrow keys, Enter and Escape on whichever field the list belongs to. Returns true when the
 * key was the list's, so the field doesn't also act on it (Enter mustn't send the message). */
export function listKey(
  key: string,
  files: Artifact[],
  active: number,
  setActive: (i: number) => void,
  onPick: (file: Artifact) => void,
  onClose: () => void,
): boolean {
  if (key === "ArrowDown") setActive(Math.min(active + 1, files.length - 1));
  else if (key === "ArrowUp") setActive(Math.max(active - 1, 0));
  else if (key === "Enter") {
    if (!files[active]) {
      onClose(); // nothing to pick: the "@" was just text, and Enter does what it usually does
      return false;
    }
    onPick(files[active]);
  } else if (key === "Escape") onClose();
  else return false;
  return true;
}
