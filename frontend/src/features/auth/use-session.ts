import { useSuspenseQuery } from "@tanstack/react-query";

import { meQueryOptions, type MeResponse } from "./auth-api";

/** The signed-in operator and memberships (the shell has already loaded them, so this never suspends below it). */
export function useSession(): MeResponse {
  return useSuspenseQuery(meQueryOptions).data;
}
