/*
 * The audit log as a cursor-paged list. The API filters by exact action and by actor id, so those two
 * narrow the request itself; free text is matched over the pages already loaded.
 */
import { useInfiniteQuery, type InfiniteData, type QueryKey } from "@tanstack/react-query";

import {
  getListAuditRecordsV1ProjectsProjectIdAuditRecordsGetUrl,
  listAuditRecordsV1ProjectsProjectIdAuditRecordsGet,
} from "../../api/generated/endpoints/audit/audit";
import type { AuditRecordOut, PageAuditRecordOut } from "../../api/generated/model";
import type { ApiError } from "../../lib/api";

export const AUDIT_PAGE_SIZE = 25;

export interface AuditFilters {
  action: string;
  actorId: string;
}

export function useAuditRecords(projectId: string | null, filters: AuditFilters) {
  const params = {
    limit: AUDIT_PAGE_SIZE,
    ...(filters.action ? { action: filters.action } : {}),
    ...(filters.actorId ? { actor_id: filters.actorId } : {}),
  };
  return useInfiniteQuery<PageAuditRecordOut, ApiError, InfiniteData<PageAuditRecordOut, string | null>, QueryKey, string | null>({
    queryKey:
      projectId === null ? ["audit-list", "none"] : [getListAuditRecordsV1ProjectsProjectIdAuditRecordsGetUrl(projectId, params), "pages"],
    initialPageParam: null,
    queryFn: ({ signal, pageParam }) => {
      if (projectId === null) throw new Error("no project");
      return listAuditRecordsV1ProjectsProjectIdAuditRecordsGet(projectId, { ...params, ...(pageParam ? { cursor: pageParam } : {}) }, { signal });
    },
    getNextPageParam: (last) => (last.has_more ? last.next_cursor : undefined),
    enabled: projectId !== null,
  });
}

export function flattenAudit(data: InfiniteData<PageAuditRecordOut, string | null> | undefined): AuditRecordOut[] {
  return data?.pages.flatMap((page) => page.data) ?? [];
}
