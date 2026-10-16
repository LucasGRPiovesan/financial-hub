import { useQuery } from "@tanstack/react-query";

import {
  getListAuditRecordsV1ProjectsProjectIdAuditRecordsGetUrl,
  listAuditRecordsV1ProjectsProjectIdAuditRecordsGet,
} from "../../api/generated/endpoints/audit/audit";
import type { PageAuditRecordOut } from "../../api/generated/model";
import type { ApiError } from "../../lib/api";

/** The newest audit records of a project (the API returns them newest first). */
export function useRecentAudit(projectId: string | null, enabled: boolean, limit = 8) {
  return useQuery<PageAuditRecordOut, ApiError>({
    queryKey:
      projectId === null ? ["audit", "none"] : [getListAuditRecordsV1ProjectsProjectIdAuditRecordsGetUrl(projectId, { limit })],
    queryFn: ({ signal }) => {
      if (projectId === null) throw new Error("no project");
      return listAuditRecordsV1ProjectsProjectIdAuditRecordsGet(projectId, { limit }, { signal });
    },
    enabled: projectId !== null && enabled,
  });
}
