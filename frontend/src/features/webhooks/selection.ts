import { useState } from "react";

export const ALL_EVENTS = "*";

export interface EventSelection {
  all: boolean;
  /** Exact types and prefix filters (`payment.*`) the operator ticked. */
  types: string[];
}

export function selectionOf(eventTypes: readonly string[] | undefined): EventSelection {
  if (!eventTypes || eventTypes.length === 0 || eventTypes.includes(ALL_EVENTS)) return { all: true, types: [] };
  return { all: false, types: [...eventTypes] };
}

/** The request's `event_types`: `["*"]` for all events, else the ticked types (possibly empty, which the form rejects). */
export function eventTypesOf(selection: EventSelection): string[] {
  return selection.all ? [ALL_EVENTS] : selection.types;
}

/** A dialog that holds a one-time secret cannot be dismissed (Escape, outside click, X) until the operator confirms. */
export function useSecretGuard(onClosed: () => void) {
  const [warn, setWarn] = useState(false);
  return {
    warn,
    guard(next: boolean, holdsSecret: boolean): boolean {
      if (!next && holdsSecret) {
        setWarn(true);
        return false;
      }
      if (!next) setWarn(false);
      return true;
    },
    reset() {
      setWarn(false);
      onClosed();
    },
  };
}
