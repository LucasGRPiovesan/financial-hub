import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HealthIndicator } from "./health-indicator";

function renderIndicator() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <HealthIndicator />
    </QueryClientProvider>,
  );
}

describe("HealthIndicator", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows live when the API answers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ status: "live" }) }),
    );
    renderIndicator();
    expect(await screen.findByText(/API: live/)).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith("/health/live");
  });

  it("shows unreachable when the API fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network")));
    renderIndicator();
    expect(await screen.findByText(/API: unreachable/)).toBeInTheDocument();
  });
});
