import { STREAM_EVENT_TYPES } from "../../lib/realtime/event-routing";

/** Types offered in the filter: what the stream delivers (ADR-0005 registry) plus the other types the platform records. */
export const EVENT_TYPE_OPTIONS: readonly string[] = [...STREAM_EVENT_TYPES, "payment.status_changed", "refund.requested"].sort();

/** `payment.created` -> the key under `events.types` (nested by dot), shown next to the raw type. */
export function eventTypeKey(type: string): string {
  return `events.types.${type}`;
}

export const DELIVERY_TONES: Record<string, "success" | "warning" | "danger" | "info" | "neutral"> = {
  delivered: "success",
  succeeded: "success",
  pending: "info",
  retrying: "warning",
  failed: "danger",
  dead: "danger",
};

export function deliveryTone(status: string): "success" | "warning" | "danger" | "info" | "neutral" {
  return DELIVERY_TONES[status] ?? "neutral";
}

/** The part of a ledger/payment payload worth a one-line summary in lists: none (kept raw in the detail). */
export function shortId(id: string): string {
  return id.length > 14 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
}
