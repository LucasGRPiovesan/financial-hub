import { createContext } from "react";

import type { Membership } from "../auth/auth-api";
import type { ProjectStream } from "../../lib/realtime/use-project-stream";
import type { Permission } from "./permissions";

export interface ProjectContextValue {
  /** The operator's active project (last choice, else the first); null when they have none. */
  membership: Membership | null;
  stream: ProjectStream;
  can: (permission: Permission) => boolean;
}

export const ProjectContext = createContext<ProjectContextValue | null>(null);
