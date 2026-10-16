/*
 * Typed query/mutation layer over the generated request functions.
 *
 * Wraps the generated request functions (orval, bare JSON bodies) with console-specific concerns: list
 * limits, cache invalidation after mutations, and keeping the one-time API key plaintext out of the cache.
 *
 * Query keys start with the request URL (`/v1/projects/{id}/...`), which is what the realtime layer matches
 * on to invalidate live data (see lib/realtime/event-routing.ts).
 */
import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";

import {
  createApiKeyV1ProjectsProjectIdApiKeysPost,
  getListApiKeysV1ProjectsProjectIdApiKeysGetUrl,
  listApiKeysV1ProjectsProjectIdApiKeysGet,
  revokeApiKeyV1ProjectsProjectIdApiKeysKeyIdRevokePost,
} from "../../api/generated/endpoints/api-keys/api-keys";
import {
  getGetTrialBalanceV1ProjectsProjectIdLedgerTrialBalanceGetUrl,
  getTrialBalanceV1ProjectsProjectIdLedgerTrialBalanceGet,
} from "../../api/generated/endpoints/ledger/ledger";
import {
  createProjectV1ProjectsPost,
  getListProjectsV1ProjectsGetUrl,
  listProjectsV1ProjectsGet,
  updateProjectV1ProjectsProjectIdPatch,
} from "../../api/generated/endpoints/projects/projects";
import {
  activateProviderAccountV1ProjectsProjectIdProviderAccountsAccountIdActivatePost,
  createProviderAccountV1ProjectsProjectIdProviderAccountsPost,
  deactivateProviderAccountV1ProjectsProjectIdProviderAccountsAccountIdDeactivatePost,
  getGetProviderCatalogV1ProviderCatalogGetUrl,
  getListProviderAccountsV1ProjectsProjectIdProviderAccountsGetUrl,
  getProviderCatalogV1ProviderCatalogGet,
  listProviderAccountsV1ProjectsProjectIdProviderAccountsGet,
  replaceProviderCredentialsV1ProjectsProjectIdProviderAccountsAccountIdCredentialsPut,
} from "../../api/generated/endpoints/providers/providers";
import type {
  ApiKeyOut,
  CatalogEntryOut,
  CreateApiKeyRequest,
  CreateProjectRequest,
  CreateProviderAccountRequest,
  PageApiKeyOut,
  PageProjectOut,
  PageProviderAccountOut,
  ProjectOut,
  ProviderAccountOut,
  TrialBalanceOut,
  UpdateProjectRequest,
} from "../../api/generated/model";
import type { ApiError } from "../../lib/api";
import { meQueryOptions } from "../auth/auth-api";

/** Largest page the API serves (`MAX_LIMIT`); the console lists up to this many rows and says when more exist. */
export const LIST_LIMIT = 200;

// ---- query keys -----------------------------------------------------------------------------------------

const projectsKey = (): QueryKey => [getListProjectsV1ProjectsGetUrl({ limit: LIST_LIMIT })];
const trialBalanceKey = (projectId: string): QueryKey => [
  getGetTrialBalanceV1ProjectsProjectIdLedgerTrialBalanceGetUrl(projectId),
];
const apiKeysKey = (projectId: string): QueryKey => [
  getListApiKeysV1ProjectsProjectIdApiKeysGetUrl(projectId, { limit: LIST_LIMIT }),
];
const providerAccountsKey = (projectId: string): QueryKey => [
  getListProviderAccountsV1ProjectsProjectIdProviderAccountsGetUrl(projectId, { limit: LIST_LIMIT }),
];
const catalogKey = (): QueryKey => [getGetProviderCatalogV1ProviderCatalogGetUrl()];

// ---- projects -------------------------------------------------------------------------------------------

export function useProjects() {
  return useQuery<PageProjectOut, ApiError>({
    queryKey: projectsKey(),
    queryFn: ({ signal }) => listProjectsV1ProjectsGet({ limit: LIST_LIMIT }, { signal }),
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation<ProjectOut, ApiError, CreateProjectRequest>({
    mutationFn: (request) => createProjectV1ProjectsPost(request),
    onSuccess: async () => {
      // The new project is a new membership (switcher) and a new row.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: projectsKey() }),
        queryClient.invalidateQueries({ queryKey: meQueryOptions.queryKey }),
      ]);
    },
  });
}

export function useUpdateProject(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<ProjectOut, ApiError, UpdateProjectRequest>({
    mutationFn: (request) => updateProjectV1ProjectsProjectIdPatch(projectId, request),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: projectsKey() }),
        queryClient.invalidateQueries({ queryKey: meQueryOptions.queryKey }),
      ]);
    },
  });
}

// ---- ledger ---------------------------------------------------------------------------------------------

export function useTrialBalance(projectId: string | null) {
  return useQuery<TrialBalanceOut, ApiError>({
    queryKey: projectId === null ? ["trial-balance", "none"] : trialBalanceKey(projectId),
    queryFn: ({ signal }) => {
      if (projectId === null) throw new Error("no project");
      return getTrialBalanceV1ProjectsProjectIdLedgerTrialBalanceGet(projectId, undefined, { signal });
    },
    enabled: projectId !== null,
  });
}

// ---- API keys -------------------------------------------------------------------------------------------

export function useApiKeys(projectId: string | null, enabled = true) {
  return useQuery<PageApiKeyOut, ApiError>({
    queryKey: projectId === null ? ["api-keys", "none"] : apiKeysKey(projectId),
    queryFn: ({ signal }) => {
      if (projectId === null) throw new Error("no project");
      return listApiKeysV1ProjectsProjectIdApiKeysGet(projectId, { limit: LIST_LIMIT }, { signal });
    },
    enabled: projectId !== null && enabled,
  });
}

export interface CreateApiKeyVariables {
  request: CreateApiKeyRequest;
  /** Receives the plaintext key exactly once. It is deliberately NOT part of the mutation result. */
  onKey: (plaintext: string) => void;
}

/**
 * The create response carries the one-time plaintext key. TanStack Query would keep a mutation's result in
 * the mutation cache, so the key is handed to `onKey` and stripped from what the mutation resolves with.
 */
export function useCreateApiKey(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<ApiKeyOut, ApiError, CreateApiKeyVariables>({
    mutationFn: async ({ request, onKey }) => {
      const { key, ...view } = await createApiKeyV1ProjectsProjectIdApiKeysPost(projectId, request);
      onKey(key);
      return view;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: apiKeysKey(projectId) }),
  });
}

export function useRevokeApiKey(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<ApiKeyOut, ApiError, string>({
    mutationFn: (keyId) => revokeApiKeyV1ProjectsProjectIdApiKeysKeyIdRevokePost(projectId, keyId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: apiKeysKey(projectId) }),
  });
}

// ---- provider accounts ----------------------------------------------------------------------------------

export function useProviderAccounts(projectId: string | null, enabled = true) {
  return useQuery<PageProviderAccountOut, ApiError>({
    queryKey: projectId === null ? ["provider-accounts", "none"] : providerAccountsKey(projectId),
    queryFn: ({ signal }) => {
      if (projectId === null) throw new Error("no project");
      return listProviderAccountsV1ProjectsProjectIdProviderAccountsGet(projectId, { limit: LIST_LIMIT }, { signal });
    },
    enabled: projectId !== null && enabled,
  });
}

export function useProviderCatalog() {
  return useQuery<CatalogEntryOut[], ApiError>({
    queryKey: catalogKey(),
    queryFn: ({ signal }) => getProviderCatalogV1ProviderCatalogGet({ signal }),
    staleTime: 5 * 60_000,
  });
}

export function useCreateProviderAccount(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<ProviderAccountOut, ApiError, CreateProviderAccountRequest>({
    mutationFn: (request) => createProviderAccountV1ProjectsProjectIdProviderAccountsPost(projectId, request),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: providerAccountsKey(projectId) }),
  });
}

export function useReplaceProviderCredentials(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<ProviderAccountOut, ApiError, { accountId: string; credentials: Record<string, string> }>({
    mutationFn: ({ accountId, credentials }) =>
      replaceProviderCredentialsV1ProjectsProjectIdProviderAccountsAccountIdCredentialsPut(projectId, accountId, {
          credentials,
        }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: providerAccountsKey(projectId) }),
  });
}

export function useSetProviderAccountActive(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<ProviderAccountOut, ApiError, { accountId: string; active: boolean }>({
    mutationFn: ({ accountId, active }) =>
      active
          ? activateProviderAccountV1ProjectsProjectIdProviderAccountsAccountIdActivatePost(projectId, accountId)
          : deactivateProviderAccountV1ProjectsProjectIdProviderAccountsAccountIdDeactivatePost(projectId, accountId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: providerAccountsKey(projectId) }),
  });
}
