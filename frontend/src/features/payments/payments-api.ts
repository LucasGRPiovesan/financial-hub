/*
 * Payments: cursor-paged list, one payment (with its refunds) and refunds.
 *
 * Query keys start with the request path, so the project-wide realtime invalidation and the post-refund
 * refresh (`/v1/projects/{id}/payments...`) reach every cached payment view.
 */
import { useInfiniteQuery, useQueries, useQuery, useQueryClient, type InfiniteData, type QueryKey } from "@tanstack/react-query";
import { useState } from "react";

import { getCustomerV1ProjectsProjectIdCustomersCustomerIdGet } from "../../api/generated/endpoints/customers/customers";
import {
  createRefundV1ProjectsProjectIdPaymentsPaymentIdRefundsPost,
  getGetPaymentV1ProjectsProjectIdPaymentsPaymentIdGetQueryKey,
  getListPaymentsV1ProjectsProjectIdPaymentsGetUrl,
  getPaymentV1ProjectsProjectIdPaymentsPaymentIdGet,
  listPaymentsV1ProjectsProjectIdPaymentsGet,
} from "../../api/generated/endpoints/payments/payments";
import type { CustomerOut, PagePaymentOut, PaymentOut, RefundOut } from "../../api/generated/model";
import type { ApiError } from "../../lib/api";

export const PAYMENTS_PAGE_SIZE = 25;

const listParams = { limit: PAYMENTS_PAGE_SIZE };

export function usePayments(projectId: string | null) {
  return useInfiniteQuery<PagePaymentOut, ApiError, InfiniteData<PagePaymentOut, string | null>, QueryKey, string | null>({
    queryKey: projectId === null ? ["payments", "none"] : [getListPaymentsV1ProjectsProjectIdPaymentsGetUrl(projectId, listParams), "pages"],
    initialPageParam: null,
    queryFn: ({ signal, pageParam }) => {
      if (projectId === null) throw new Error("no project");
      return listPaymentsV1ProjectsProjectIdPaymentsGet(projectId, { ...listParams, ...(pageParam ? { cursor: pageParam } : {}) }, { signal });
    },
    getNextPageParam: (last) => (last.has_more ? last.next_cursor : undefined),
    enabled: projectId !== null,
  });
}

export function flattenPayments(data: InfiniteData<PagePaymentOut, string | null> | undefined): PaymentOut[] {
  return data?.pages.flatMap((page) => page.data) ?? [];
}

export function usePayment(projectId: string | null, paymentId: string) {
  return useQuery<PaymentOut, ApiError>({
    queryKey: projectId === null ? ["payment", "none"] : getGetPaymentV1ProjectsProjectIdPaymentsPaymentIdGetQueryKey(projectId, paymentId),
    queryFn: ({ signal }) => {
      if (projectId === null) throw new Error("no project");
      return getPaymentV1ProjectsProjectIdPaymentsPaymentIdGet(projectId, paymentId, { signal });
    },
    enabled: projectId !== null,
  });
}

/** customer_id -> customer, fetched once per distinct id (cached, so the list and the detail share them). */
export function useCustomerLookup(projectId: string | null, customerIds: readonly string[]): ReadonlyMap<string, CustomerOut> {
  const unique = [...new Set(customerIds)];
  const results = useQueries({
    queries: unique.map((id) => ({
      queryKey: [`/v1/projects/${projectId ?? "none"}/customers/${id}`],
      queryFn: ({ signal }: { signal: AbortSignal }) => {
        if (projectId === null) throw new Error("no project");
        return getCustomerV1ProjectsProjectIdCustomersCustomerIdGet(projectId, id, { signal });
      },
      enabled: projectId !== null,
      staleTime: 60_000,
      retry: false,
    })),
  });
  const map = new Map<string, CustomerOut>();
  results.forEach((result) => {
    if (result.data) map.set(result.data.id, result.data);
  });
  return map;
}

/** A fresh key for one refund attempt (a retry of the same attempt reuses it, so the API replays instead of refunding twice). */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

export function useCreateRefund(projectId: string, paymentId: string) {
  const queryClient = useQueryClient();
  const [isPending, setPending] = useState(false);
  return {
    isPending,
    refund: async (request: { amount: number | null; reason: string | null }, idempotencyKey: string): Promise<RefundOut> => {
      setPending(true);
      try {
        const result = await createRefundV1ProjectsProjectIdPaymentsPaymentIdRefundsPost(projectId, paymentId, request, {
          headers: { "Idempotency-Key": idempotencyKey },
        });
        const prefix = `/v1/projects/${projectId}/payments`;
        await queryClient.invalidateQueries({
          predicate: (query) => typeof query.queryKey[0] === "string" && query.queryKey[0].startsWith(prefix),
        });
        return result;
      } finally {
        setPending(false);
      }
    },
  };
}
