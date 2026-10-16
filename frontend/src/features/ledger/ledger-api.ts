/*
 * Ledger reads: the journal (cursor-paged), one entry with its postings, and the chart of accounts.
 * The trial balance is shared with the Overview (`useTrialBalance` in features/console/api.ts).
 */
import { useInfiniteQuery, useQuery, type InfiniteData, type QueryKey } from "@tanstack/react-query";

import {
  getEntryV1ProjectsProjectIdLedgerEntriesEntryIdGet,
  getGetEntryV1ProjectsProjectIdLedgerEntriesEntryIdGetQueryKey,
  getListAccountsV1ProjectsProjectIdLedgerAccountsGetUrl,
  getListEntriesV1ProjectsProjectIdLedgerEntriesGetUrl,
  listAccountsV1ProjectsProjectIdLedgerAccountsGet,
  listEntriesV1ProjectsProjectIdLedgerEntriesGet,
} from "../../api/generated/endpoints/ledger/ledger";
import type {
  AccountOut,
  AccountType,
  EntryDetailOut,
  EntryOut,
  EntryStatus,
  PageAccountOut,
  PageEntryOut,
} from "../../api/generated/model";
import type { ApiError } from "../../lib/api";

export const ENTRIES_PAGE_SIZE = 25;
export const ACCOUNTS_PAGE_SIZE = 50;

export interface EntryFilters {
  status: EntryStatus | "";
  recipe: string;
}

export function useEntries(projectId: string | null, filters: EntryFilters) {
  const params = {
    limit: ENTRIES_PAGE_SIZE,
    ...(filters.status ? { status: filters.status } : {}),
    ...(filters.recipe ? { recipe: filters.recipe } : {}),
  };
  return useInfiniteQuery<PageEntryOut, ApiError, InfiniteData<PageEntryOut, string | null>, QueryKey, string | null>({
    queryKey: projectId === null ? ["ledger-entries", "none"] : [getListEntriesV1ProjectsProjectIdLedgerEntriesGetUrl(projectId, params), "pages"],
    initialPageParam: null,
    queryFn: ({ signal, pageParam }) => {
      if (projectId === null) throw new Error("no project");
      return listEntriesV1ProjectsProjectIdLedgerEntriesGet(projectId, { ...params, ...(pageParam ? { cursor: pageParam } : {}) }, { signal });
    },
    getNextPageParam: (last) => (last.has_more ? last.next_cursor : undefined),
    enabled: projectId !== null,
  });
}

export function flattenEntries(data: InfiniteData<PageEntryOut, string | null> | undefined): EntryOut[] {
  return data?.pages.flatMap((page) => page.data) ?? [];
}

export function useEntry(projectId: string | null, entryId: string, enabled = true) {
  return useQuery<EntryDetailOut, ApiError>({
    queryKey: projectId === null ? ["ledger-entry", "none"] : getGetEntryV1ProjectsProjectIdLedgerEntriesEntryIdGetQueryKey(projectId, entryId),
    queryFn: ({ signal }) => {
      if (projectId === null) throw new Error("no project");
      return getEntryV1ProjectsProjectIdLedgerEntriesEntryIdGet(projectId, entryId, { signal });
    },
    enabled: projectId !== null && enabled,
    staleTime: 60_000,
  });
}

export function useAccounts(projectId: string | null, type: AccountType | "") {
  const params = { limit: ACCOUNTS_PAGE_SIZE, ...(type ? { type } : {}) };
  return useInfiniteQuery<PageAccountOut, ApiError, InfiniteData<PageAccountOut, string | null>, QueryKey, string | null>({
    queryKey: projectId === null ? ["ledger-accounts", "none"] : [getListAccountsV1ProjectsProjectIdLedgerAccountsGetUrl(projectId, params), "pages"],
    initialPageParam: null,
    queryFn: ({ signal, pageParam }) => {
      if (projectId === null) throw new Error("no project");
      return listAccountsV1ProjectsProjectIdLedgerAccountsGet(projectId, { ...params, ...(pageParam ? { cursor: pageParam } : {}) }, { signal });
    },
    getNextPageParam: (last) => (last.has_more ? last.next_cursor : undefined),
    enabled: projectId !== null,
  });
}

export function flattenAccounts(data: InfiniteData<PageAccountOut, string | null> | undefined): AccountOut[] {
  return data?.pages.flatMap((page) => page.data) ?? [];
}
