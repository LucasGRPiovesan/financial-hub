import { queryOptions } from "@tanstack/react-query";

import { apiFetch, clearCsrfToken, setCsrfToken } from "../../lib/api";

export interface Operator {
  id: string;
  email: string;
  display_name: string;
}

export type MembershipRole = string;

export interface Membership {
  project_id: string;
  project_name: string;
  role: MembershipRole;
}

export interface LoginResponse {
  operator: Operator;
  csrf_token: string;
}

export interface MeResponse {
  operator: Operator;
  memberships: Membership[];
  csrf_token: string;
}

export interface LoginCredentials {
  email: string;
  password: string;
}

export async function login(credentials: LoginCredentials): Promise<LoginResponse> {
  const response = await apiFetch<LoginResponse>("/v1/auth/login", {
    method: "POST",
    body: credentials,
    redirectOn401: false,
  });
  setCsrfToken(response.csrf_token);
  return response;
}

export async function logout(): Promise<void> {
  try {
    await apiFetch("/v1/auth/logout", { method: "POST", redirectOn401: false });
  } finally {
    clearCsrfToken();
  }
}

/** The route guard's session probe: a 401 here means "signed out", not an error to redirect on. */
export async function fetchMe(signal?: AbortSignal): Promise<MeResponse> {
  const response = await apiFetch<MeResponse>("/v1/auth/me", { redirectOn401: false, signal });
  setCsrfToken(response.csrf_token);
  return response;
}

export const meQueryOptions = queryOptions({
  queryKey: ["auth", "me"],
  queryFn: ({ signal }) => fetchMe(signal),
  staleTime: 60_000,
  retry: false,
});
