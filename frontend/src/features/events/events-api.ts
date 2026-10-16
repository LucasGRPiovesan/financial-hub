/*
 * Event Explorer reads: cursor-paged list (newest first), one event with its deliveries, and its causality.
 *
 * Query keys start with the request path, so the project stream's invalidation (`/v1/projects/{id}/events...`,
 * see lib/realtime/event-routing.ts) refreshes the list and any open detail when something new is published.
 */
import { useInfiniteQuery, useQuery, type InfiniteData, type QueryKey } from "@tanstack/react-query";

import {
  getEventCausalityV1ProjectsProjectIdEventsEventIdCausalityGet,
  getGetEventCausalityV1ProjectsProjectIdEventsEventIdCausalityGetUrl,
  getGetProjectEventV1ProjectsProjectIdEventsEventIdGetUrl,
  getListProjectEventsV1ProjectsProjectIdEventsGetUrl,
  getProjectEventV1ProjectsProjectIdEventsEventIdGet,
  listProjectEventsV1ProjectsProjectIdEventsGet,
} from "../../api/generated/endpoints/events/events";
import type { CausalityOut, EventDetailOut, EventListOut, EventOut } from "../../api/generated/model";
import type { ApiError } from "../../lib/api";

export const EVENTS_PAGE_SIZE = 25;

function listParams(type: string) {
  return { limit: EVENTS_PAGE_SIZE, ...(type ? { type } : {}) };
}

export function useEvents(projectId: string | null, type: string) {
  return useInfiniteQuery<EventListOut, ApiError, InfiniteData<EventListOut, string | null>, QueryKey, string | null>({
    queryKey: projectId === null ? ["events", "none"] : [getListProjectEventsV1ProjectsProjectIdEventsGetUrl(projectId, listParams(type)), "pages"],
    initialPageParam: null,
    queryFn: ({ signal, pageParam }) => {
      if (projectId === null) throw new Error("no project");
      return listProjectEventsV1ProjectsProjectIdEventsGet(projectId, { ...listParams(type), ...(pageParam ? { cursor: pageParam } : {}) }, { signal });
    },
    getNextPageParam: (last) => (last.has_more ? last.next_cursor : undefined),
    enabled: projectId !== null,
  });
}

export function flattenEvents(data: InfiniteData<EventListOut, string | null> | undefined): EventOut[] {
  return data?.pages.flatMap((page) => page.data) ?? [];
}

/** The envelope and its deliveries (one request). */
export function useEvent(projectId: string | null, eventId: string) {
  return useQuery<EventDetailOut, ApiError>({
    queryKey: projectId === null ? ["event", "none"] : [getGetProjectEventV1ProjectsProjectIdEventsEventIdGetUrl(projectId, eventId)],
    queryFn: ({ signal }) => {
      if (projectId === null) throw new Error("no project");
      return getProjectEventV1ProjectsProjectIdEventsEventIdGet(projectId, eventId, { signal });
    },
    enabled: projectId !== null,
  });
}

export function useCausality(projectId: string | null, eventId: string) {
  return useQuery<CausalityOut, ApiError>({
    queryKey: projectId === null ? ["causality", "none"] : [getGetEventCausalityV1ProjectsProjectIdEventsEventIdCausalityGetUrl(projectId, eventId)],
    queryFn: ({ signal }) => {
      if (projectId === null) throw new Error("no project");
      return getEventCausalityV1ProjectsProjectIdEventsEventIdCausalityGet(projectId, eventId, { signal });
    },
    enabled: projectId !== null,
  });
}
