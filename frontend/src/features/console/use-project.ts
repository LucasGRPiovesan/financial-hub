import { use } from "react";

import { ProjectContext, type ProjectContextValue } from "./project-store";

export function useProject(): ProjectContextValue {
  const value = use(ProjectContext);
  if (!value) throw new Error("useProject must be used inside <ProjectProvider>");
  return value;
}
