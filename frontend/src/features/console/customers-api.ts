/*
 * Customers: list (cursor pages + exact-match filters), upsert by external_ref and anonymize.
 *
 * The CPF/CNPJ never lives in the query cache: `useUpsertCustomer` is deliberately NOT a TanStack mutation
 * (those keep their variables in the mutation cache); the request goes out and only the response (which has
 * `document_masked`, never the digits) is handed back.
 */
import { useInfiniteQuery, useQueryClient, type InfiniteData, type QueryKey } from "@tanstack/react-query";
import { useState } from "react";

import {
  anonymizeCustomerV1ProjectsProjectIdCustomersCustomerIdAnonymizePost,
  getListCustomersV1ProjectsProjectIdCustomersGetUrl,
  listCustomersV1ProjectsProjectIdCustomersGet,
  upsertCustomerV1ProjectsProjectIdCustomersExternalRefPut,
} from "../../api/generated/endpoints/customers/customers";
import type { CustomerOut, PageCustomerOut, UpsertCustomerRequest } from "../../api/generated/model";
import { isApiError, type ApiError } from "../../lib/api";

export const CUSTOMERS_PAGE_SIZE = 25;

export type CustomerFilter = { by: "external_ref" | "email"; value: string } | null;

function listParams(filter: CustomerFilter) {
  return {
    limit: CUSTOMERS_PAGE_SIZE,
    ...(filter && filter.value ? { [filter.by]: filter.value } : {}),
  };
}

/** Key head = the first page's URL (project path included) so realtime/project invalidation still matches. */
const customersKey = (projectId: string, filter: CustomerFilter): QueryKey => [
  getListCustomersV1ProjectsProjectIdCustomersGetUrl(projectId, listParams(filter)),
  "pages",
];

export function useCustomers(projectId: string | null, filter: CustomerFilter, enabled = true) {
  return useInfiniteQuery<PageCustomerOut, ApiError, InfiniteData<PageCustomerOut, string | null>, QueryKey, string | null>({
    queryKey: projectId === null ? ["customers", "none"] : customersKey(projectId, filter),
    initialPageParam: null,
    queryFn: ({ signal, pageParam }) => {
      if (projectId === null) throw new Error("no project");
      return listCustomersV1ProjectsProjectIdCustomersGet(
        projectId,
        { ...listParams(filter), ...(pageParam ? { cursor: pageParam } : {}) },
        { signal },
      );
    },
    getNextPageParam: (last) => (last.has_more ? last.next_cursor : undefined),
    enabled: projectId !== null && enabled,
  });
}

export function flattenCustomers(data: InfiniteData<PageCustomerOut, string | null> | undefined): CustomerOut[] {
  return data?.pages.flatMap((page) => page.data) ?? [];
}

function useRefreshCustomers(projectId: string) {
  const queryClient = useQueryClient();
  const prefix = `/v1/projects/${projectId}/customers`;
  return () =>
    queryClient.invalidateQueries({
      predicate: (query) => typeof query.queryKey[0] === "string" && query.queryKey[0].startsWith(prefix),
    });
}

interface Submission<T> {
  isPending: boolean;
  /** Resolves with the response; rejects with the ApiError so the form can map it to fields. */
  run: (action: () => Promise<T>) => Promise<T>;
}

function useSubmission<T>(onDone: () => Promise<unknown>): Submission<T> {
  const [isPending, setPending] = useState(false);
  return {
    isPending,
    run: async (action) => {
      setPending(true);
      try {
        const result = await action();
        await onDone();
        return result;
      } finally {
        setPending(false);
      }
    },
  };
}

export function useUpsertCustomer(projectId: string) {
  const refresh = useRefreshCustomers(projectId);
  const submission = useSubmission<CustomerOut>(refresh);
  return {
    isPending: submission.isPending,
    /** `request.document` is sent once and not retained anywhere. */
    upsert: (externalRef: string, request: UpsertCustomerRequest) =>
      submission.run(() => upsertCustomerV1ProjectsProjectIdCustomersExternalRefPut(projectId, externalRef, request)),
  };
}

export function useAnonymizeCustomer(projectId: string) {
  const refresh = useRefreshCustomers(projectId);
  const submission = useSubmission<CustomerOut>(refresh);
  return {
    isPending: submission.isPending,
    anonymize: (customerId: string) =>
      submission.run(() => anonymizeCustomerV1ProjectsProjectIdCustomersCustomerIdAnonymizePost(projectId, customerId)),
  };
}

export function isConflict(error: unknown): error is ApiError {
  return isApiError(error) && error.status === 409;
}
