import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { matchesEvent, matchesProject, STREAM_EVENT_TYPES } from "./event-routing";

export type StreamStatus = "connecting" | "open" | "retrying" | "closed";

export interface ProjectStream {
  status: StreamStatus;
  /** Tears the connection down and opens a new one resuming from the last seen event id. */
  reconnect: () => void;
}

export function streamUrl(projectId: string, lastEventId: string | null): string {
  const base = `/v1/projects/${projectId}/stream`;
  return lastEventId === null ? base : `${base}?last_event_id=${encodeURIComponent(lastEventId)}`;
}

/**
 * Live project events over SSE. The browser auto-reconnects (and resends Last-Event-ID) on its own, so
 * `retrying` is just EventSource.CONNECTING after an error; a manual `reconnect()` passes `?last_event_id=`.
 */
export function useProjectStream(projectId: string | null): ProjectStream {
  const queryClient = useQueryClient();
  const [generation, setGeneration] = useState(0);
  // Status is tagged with the connection it describes, so a new connection reads "connecting" without an effect reset.
  const connectionKey = `${projectId ?? ""}#${generation}`;
  const [report, setReport] = useState<{ key: string; status: StreamStatus }>({ key: "", status: "connecting" });
  const status: StreamStatus = projectId === null ? "closed" : report.key === connectionKey ? report.status : "connecting";
  const lastEventId = useRef<string | null>(null);
  const lastProject = useRef<string | null>(null);

  const reconnect = useCallback(() => setGeneration((value) => value + 1), []);

  useEffect(() => {
    if (projectId === null) return;
    if (lastProject.current !== projectId) {
      lastProject.current = projectId;
      lastEventId.current = null;
    }
    const setStatus = (next: StreamStatus) => setReport({ key: connectionKey, status: next });
    const source = new EventSource(streamUrl(projectId, lastEventId.current), { withCredentials: true });

    const onOpen = () => setStatus("open");
    const onError = () => setStatus(source.readyState === EventSource.CLOSED ? "closed" : "retrying");
    const onEvent = (event: Event) => {
      if (!(event instanceof MessageEvent)) return;
      if (event.lastEventId !== "") lastEventId.current = event.lastEventId;
      void queryClient.invalidateQueries({
        predicate: (query) => matchesEvent(query.queryKey, projectId, event.type),
      });
    };
    const onResync = () => {
      void queryClient.invalidateQueries({ predicate: (query) => matchesProject(query.queryKey, projectId) });
    };

    source.addEventListener("open", onOpen);
    source.addEventListener("error", onError);
    source.addEventListener("resync", onResync);
    for (const type of STREAM_EVENT_TYPES) source.addEventListener(type, onEvent);

    return () => {
      source.close();
      source.removeEventListener("open", onOpen);
      source.removeEventListener("error", onError);
      source.removeEventListener("resync", onResync);
      for (const type of STREAM_EVENT_TYPES) source.removeEventListener(type, onEvent);
    };
  }, [projectId, queryClient, connectionKey]);

  return { status, reconnect };
}
