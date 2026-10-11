import { useCallback, useSyncExternalStore } from "react";

import type { Membership } from "../features/auth/auth-api";

export const PROJECT_STORAGE_KEY = "fh.project";

const listeners = new Set<() => void>();
let memoryValue: string | null = null;

function readStored(): string | null {
  try {
    return window.localStorage.getItem(PROJECT_STORAGE_KEY) ?? memoryValue;
  } catch {
    return memoryValue;
  }
}

export function selectProject(projectId: string): void {
  memoryValue = projectId;
  try {
    window.localStorage.setItem(PROJECT_STORAGE_KEY, projectId);
  } catch {
    // Storage unavailable: the selection lives in memory for this page load.
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The operator's active project among their memberships (last choice, else the first). */
export function useActiveMembership(memberships: readonly Membership[]): {
  active: Membership | null;
  select: (projectId: string) => void;
} {
  const storedId = useSyncExternalStore(subscribe, readStored, () => null);
  const select = useCallback((projectId: string) => selectProject(projectId), []);
  const active = memberships.find((membership) => membership.project_id === storedId) ?? memberships[0] ?? null;
  return { active, select };
}
