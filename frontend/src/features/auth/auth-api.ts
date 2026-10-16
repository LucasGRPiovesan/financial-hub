import { queryOptions } from "@tanstack/react-query";

import {
  demoLoginV1AuthDemoLoginPost,
  demoOperatorsV1AuthDemoOperatorsGet,
} from "../../api/generated/endpoints/auth/auth";
import type { DemoOperatorOut } from "../../api/generated/model";
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

// ---- demo mode ------------------------------------------------------------------------------------------

/**
 * Operators the evaluation environment lets you sign in as (no password). The endpoint answers 404 when demo
 * login is off; any failure is treated as "no demo", so the regular password form is shown instead.
 */
export const demoOperatorsQueryOptions = queryOptions({
  queryKey: ["auth", "demo-operators"],
  queryFn: async ({ signal }): Promise<DemoOperatorOut[]> => {
    try {
      const response = await demoOperatorsV1AuthDemoOperatorsGet({ signal });
      return response.operators;
    } catch {
      return [];
    }
  },
  staleTime: 5 * 60_000,
  retry: false,
});

/** Signs in as a demo operator; afterwards the session is indistinguishable from a password login. */
export async function demoLogin(operatorId: string): Promise<LoginResponse> {
  const response = await demoLoginV1AuthDemoLoginPost({ operator_id: operatorId });
  setCsrfToken(response.csrf_token);
  return response;
}
