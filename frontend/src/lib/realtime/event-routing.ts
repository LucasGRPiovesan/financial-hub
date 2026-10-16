import type { QueryKey } from "@tanstack/react-query";

/*
 * Event type -> the query keys it invalidates. Generated hooks key their queries by URL
 * (`["/v1/projects/{id}/ledger/...", ...]`), so a mapping entry matches on a path fragment of the key head.
 * First matching prefix wins; the default row covers everything else (the events explorer).
 */
export interface RouteRule {
  /** Event type prefix, e.g. "ledger." */
  prefix: string;
  /** Substring of the query key's first element identifying the affected queries. */
  keyFragment: string;
}

export const EVENT_ROUTES: readonly RouteRule[] = [
  { prefix: "ledger.", keyFragment: "/ledger" },
  { prefix: "payment.", keyFragment: "/payments" },
];

export const DEFAULT_KEY_FRAGMENT = "/events";

/**
 * Event types the backend publishes (ADR-0005 registry). Named SSE events are only delivered to listeners
 * registered by exact name, so new types must be added here (or sent without an `event:` name).
 */
export const STREAM_EVENT_TYPES: readonly string[] = [
  "ledger.entry.posted",
  "ledger.entry.reversed",
  "payment.created",
  "payment.confirmed",
  "payment.failed",
  "payment.refunded",
  "schedule.due",
  "system.test",
  "usage.recorded",
];

export function keyFragmentFor(eventType: string): string {
  return EVENT_ROUTES.find((rule) => eventType.startsWith(rule.prefix))?.keyFragment ?? DEFAULT_KEY_FRAGMENT;
}

export function projectPathFragment(projectId: string): string {
  return `/v1/projects/${projectId}`;
}

export function keyHead(queryKey: QueryKey): string {
  const head = queryKey[0];
  return typeof head === "string" ? head : "";
}

/** The Event Explorer lists every event of the project, whatever its type (detail queries are keyed `.../events/{id}`). */
const EVENTS_LIST_HEAD = /\/events(\?|$)/;

/**
 * Predicate over query keys: project-scoped queries whose path contains the event's fragment, plus the events
 * list (it shows every type).
 */
export function matchesEvent(queryKey: QueryKey, projectId: string, eventType: string): boolean {
  const head = keyHead(queryKey);
  if (!head.includes(projectPathFragment(projectId))) return false;
  return head.includes(keyFragmentFor(eventType)) || EVENTS_LIST_HEAD.test(head);
}

/** Predicate over query keys: every query of the project (used on `resync`). */
export function matchesProject(queryKey: QueryKey, projectId: string): boolean {
  return keyHead(queryKey).includes(projectPathFragment(projectId));
}
