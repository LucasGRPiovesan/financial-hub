import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useProjectStream } from "./use-project-stream";

class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: FakeEventSource[] = [];
  readyState = 0;
  closed = false;
  private listeners = new Map<string, Set<(event: Event) => void>>();
  constructor(
    readonly url: string,
    readonly init?: EventSourceInit,
  ) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: (event: Event) => void) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)?.add(listener);
  }
  removeEventListener(type: string, listener: (event: Event) => void) {
    this.listeners.get(type)?.delete(listener);
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
  emit(event: Event) {
    this.listeners.get(event.type)?.forEach((listener) => listener(event));
  }
  open() {
    this.readyState = 1;
    this.emit(new Event("open"));
  }
  message(type: string, id: string) {
    this.emit(new MessageEvent(type, { data: "{}", lastEventId: id }));
  }
  fail(readyState: number) {
    this.readyState = readyState;
    this.emit(new Event("error"));
  }
}

const latest = () => FakeEventSource.instances.at(-1) as FakeEventSource;
const P = "prj_1";

describe("useProjectStream", () => {
  let client: QueryClient;
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );

  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
    client = new QueryClient();
    client.setQueryData([`/v1/projects/${P}/ledger/accounts`], 1);
    client.setQueryData([`/v1/projects/${P}/payments`], 1);
    client.setQueryData([`/v1/projects/${P}/events`], 1);
    client.setQueryData(["/v1/projects/other/ledger/accounts"], 1);
  });
  afterEach(() => vi.unstubAllGlobals());

  const invalidated = (key: string) => client.getQueryState([key])?.isInvalidated === true;

  it("opens with credentials and tracks status transitions", () => {
    const { result } = renderHook(() => useProjectStream(P), { wrapper });
    expect(latest().url).toBe(`/v1/projects/${P}/stream`);
    expect(latest().init?.withCredentials).toBe(true);
    expect(result.current.status).toBe("connecting");
    act(() => latest().open());
    expect(result.current.status).toBe("open");
    act(() => latest().fail(0));
    expect(result.current.status).toBe("retrying");
    act(() => latest().fail(2));
    expect(result.current.status).toBe("closed");
  });

  it("invalidates by event type prefix, only within the project", () => {
    renderHook(() => useProjectStream(P), { wrapper });
    act(() => latest().message("ledger.entry.posted", "c1"));
    expect(invalidated(`/v1/projects/${P}/ledger/accounts`)).toBe(true);
    expect(invalidated(`/v1/projects/${P}/payments`)).toBe(false);
    expect(invalidated("/v1/projects/other/ledger/accounts")).toBe(false);
    act(() => latest().message("payment.confirmed", "c2"));
    expect(invalidated(`/v1/projects/${P}/payments`)).toBe(true);
    expect(invalidated(`/v1/projects/${P}/events`)).toBe(false);
    act(() => latest().message("schedule.due", "c3"));
    expect(invalidated(`/v1/projects/${P}/events`)).toBe(true);
  });

  it("resync invalidates every project query", () => {
    renderHook(() => useProjectStream(P), { wrapper });
    act(() => latest().emit(new MessageEvent("resync", { data: "{}" })));
    expect(invalidated(`/v1/projects/${P}/ledger/accounts`)).toBe(true);
    expect(invalidated(`/v1/projects/${P}/payments`)).toBe(true);
    expect(invalidated(`/v1/projects/${P}/events`)).toBe(true);
    expect(invalidated("/v1/projects/other/ledger/accounts")).toBe(false);
  });

  it("manual reconnect resumes with last_event_id", () => {
    const { result } = renderHook(() => useProjectStream(P), { wrapper });
    const first = latest();
    act(() => first.message("payment.created", "cur/1 2"));
    act(() => result.current.reconnect());
    expect(first.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(latest().url).toBe(`/v1/projects/${P}/stream?last_event_id=${encodeURIComponent("cur/1 2")}`);
  });

  it("closes on unmount and on project change (cursor resets)", () => {
    const { rerender, unmount } = renderHook(({ id }) => useProjectStream(id), {
      wrapper,
      initialProps: { id: P as string | null },
    });
    const first = latest();
    act(() => first.message("payment.created", "c1"));
    rerender({ id: "prj_2" });
    expect(first.closed).toBe(true);
    expect(latest().url).toBe("/v1/projects/prj_2/stream");
    const second = latest();
    unmount();
    expect(second.closed).toBe(true);
  });

  it("stays closed without a project", () => {
    const { result } = renderHook(() => useProjectStream(null), { wrapper });
    expect(result.current.status).toBe("closed");
    expect(FakeEventSource.instances).toHaveLength(0);
  });
});
