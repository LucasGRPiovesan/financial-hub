/* Billable metrics (list, create, archive) and the per-customer usage summary. */
import { useInfiniteQuery, useQuery, useQueryClient, type InfiniteData, type QueryKey } from "@tanstack/react-query";
import { useState } from "react";

import {
  archiveMetricV1ProjectsProjectIdMetricsCodeArchivePost,
  createMetricV1ProjectsProjectIdMetricsPost,
  getListMetricsV1ProjectsProjectIdMetricsGetUrl,
  getUsageSummaryV1ProjectsProjectIdUsageSummaryGetUrl,
  listMetricsV1ProjectsProjectIdMetricsGet,
  usageSummaryV1ProjectsProjectIdUsageSummaryGet,
} from "../../api/generated/endpoints/metering/metering";
import type { CreateMetricRequest, MetricOut, PageMetricOut, SummaryOut } from "../../api/generated/model";
import type { ApiError } from "../../lib/api";

export const METRICS_PAGE_SIZE = 100;
const listParams = { limit: METRICS_PAGE_SIZE };

export const AGGREGATIONS = ["sum", "count", "max", "unique_count"] as const;
export type Aggregation = (typeof AGGREGATIONS)[number];

export function useMetrics(projectId: string | null) {
  return useInfiniteQuery<PageMetricOut, ApiError, InfiniteData<PageMetricOut, string | null>, QueryKey, string | null>({
    queryKey: projectId === null ? ["metrics", "none"] : [getListMetricsV1ProjectsProjectIdMetricsGetUrl(projectId, listParams), "pages"],
    initialPageParam: null,
    queryFn: ({ signal, pageParam }) => {
      if (projectId === null) throw new Error("no project");
      return listMetricsV1ProjectsProjectIdMetricsGet(projectId, { ...listParams, ...(pageParam ? { cursor: pageParam } : {}) }, { signal });
    },
    getNextPageParam: (last) => (last.has_more ? last.next_cursor : undefined),
    enabled: projectId !== null,
  });
}

export function flattenMetrics(data: InfiniteData<PageMetricOut, string | null> | undefined): MetricOut[] {
  return data?.pages.flatMap((page) => page.data) ?? [];
}

function useRefreshMetrics(projectId: string) {
  const queryClient = useQueryClient();
  const prefix = `/v1/projects/${projectId}/metrics`;
  return () =>
    queryClient.invalidateQueries({ predicate: (query) => typeof query.queryKey[0] === "string" && query.queryKey[0].startsWith(prefix) });
}

export function useMetricMutations(projectId: string) {
  const refresh = useRefreshMetrics(projectId);
  const [isPending, setPending] = useState(false);
  async function run<T>(action: () => Promise<T>): Promise<T> {
    setPending(true);
    try {
      const result = await action();
      await refresh();
      return result;
    } finally {
      setPending(false);
    }
  }
  return {
    isPending,
    create: (request: CreateMetricRequest) => run(() => createMetricV1ProjectsProjectIdMetricsPost(projectId, request)),
    archive: (code: string) => run(() => archiveMetricV1ProjectsProjectIdMetricsCodeArchivePost(projectId, code)),
  };
}

export interface UsageQuery {
  customerId: string;
  metric: string;
  /** ISO instants, [from, to). */
  from: string;
  to: string;
}

export function useUsageSummary(projectId: string, query: UsageQuery | null) {
  return useQuery<SummaryOut, ApiError>({
    queryKey: query
      ? [getUsageSummaryV1ProjectsProjectIdUsageSummaryGetUrl(projectId, { customer_id: query.customerId, metric: query.metric, from: query.from, to: query.to })]
      : ["usage", "idle"],
    queryFn: ({ signal }) => {
      if (!query) throw new Error("no query");
      return usageSummaryV1ProjectsProjectIdUsageSummaryGet(projectId, { customer_id: query.customerId, metric: query.metric, from: query.from, to: query.to }, { signal });
    },
    enabled: query !== null,
  });
}
