import type { ReactNode } from "react";

import type { Membership } from "../auth/auth-api";
import { useActiveMembership } from "../../lib/project-selection";
import { useProjectStream } from "../../lib/realtime/use-project-stream";
import { can as canWith } from "./permissions";
import { ProjectContext, type ProjectContextValue } from "./project-store";

/**
 * The project-scoped layout: the one place the realtime stream is opened, so every page under it gets live
 * query invalidation. jsdom (and very old browsers) have no EventSource; the stream then stays "closed".
 */
export function ProjectProvider({ memberships, children }: { memberships: readonly Membership[]; children: ReactNode }) {
  const { active } = useActiveMembership(memberships);
  const streamable = typeof EventSource !== "undefined";
  const stream = useProjectStream(streamable ? (active?.project_id ?? null) : null);
  const value: ProjectContextValue = {
    membership: active,
    stream,
    can: (permission) => canWith(active?.role, permission),
  };
  return <ProjectContext value={value}>{children}</ProjectContext>;
}
