import type { Artifact, ProjectItems, Tab } from "../api/types";

/** The file tab: replaced in a later step. */
export function FileTab(props: {
  projectId: number;
  artifact: Artifact | undefined;
  items: ProjectItems;
  onOpen: (tab: Tab) => void;
}) {
  void props;
  return null;
}
