import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { DateTime, RelativeTime } from "./date-time";
import { Money } from "./money";
import { ScopeBadge } from "./scope-badge";
import { EmptyState } from "./ui/empty-state";
import { Skeleton } from "./ui/skeleton";
import { Inbox } from "lucide-react";

describe("ScopeBadge", () => {
  it.each([
    ["implemented", "Implemented"],
    ["sandbox", "Sandbox"],
    ["simulated", "Simulated"],
    ["planned", "Planned"],
  ] as const)("labels %s as %s", (scope, label) => {
    render(<ScopeBadge scope={scope} />);
    const badge = screen.getByText(label);
    expect(badge).toBeInTheDocument();
    expect(badge.closest("[data-scope]")).toHaveAttribute("data-scope", scope);
  });

  it("does not rely on colour alone: each scope has its own border style", () => {
    const { container } = render(
      <>
        <ScopeBadge scope="implemented" />
        <ScopeBadge scope="sandbox" />
        <ScopeBadge scope="simulated" />
        <ScopeBadge scope="planned" />
      </>,
    );
    const styles = Array.from(container.querySelectorAll("[data-scope]")).map((node) =>
      /border-(solid|dashed|double|dotted)/.exec(node.className)?.[1],
    );
    expect(styles).toEqual(["solid", "dashed", "double", "dotted"]);
  });
});

describe("Money", () => {
  it("renders exact big amounts and marks negatives", () => {
    const { container } = render(
      <>
        <Money value={{ amount_minor: "9007199254740993", currency: "USD" }} locale="en-US" />
        <Money value={{ amount_minor: "-500", currency: "USD" }} locale="en-US" />
      </>,
    );
    expect(screen.getByText("$90,071,992,547,409.93")).not.toHaveAttribute("data-negative");
    const negative = screen.getByText("-$5.00");
    expect(negative).toHaveAttribute("data-negative", "true");
    expect(negative.className).toContain("text-danger");
    expect(negative.className).toContain("figures");
    expect(container.querySelectorAll(".figures")).toHaveLength(2);
  });

  it("shows a dash instead of throwing on malformed amounts", () => {
    render(<Money value={{ amount_minor: "12.5", currency: "USD" }} />);
    expect(screen.getByTitle("This amount could not be read")).toHaveTextContent("—");
  });
});

describe("DateTime", () => {
  it("shows the zone label and a machine-readable value", () => {
    render(<DateTime value="2026-10-10T19:30:00Z" locale="en-US" timeZone="America/Sao_Paulo" />);
    const time = screen.getByText(/Oct 10, 2026/);
    expect(time.closest("time")).toHaveAttribute("datetime", "2026-10-10T19:30:00Z");
    expect(screen.getByText("GMT-3")).toBeInTheDocument();
  });

  it("renders relative time with the absolute time as its title", () => {
    const recent = new Date(Date.now() - 3 * 60_000).toISOString();
    render(<RelativeTime value={recent} locale="en-US" refreshMs={0} />);
    const time = screen.getByText("3 minutes ago");
    expect(time.closest("time")).toHaveAttribute("datetime", recent);
    expect(time.closest("time")?.getAttribute("title")).toMatch(/\d{4}/);
  });
});

describe("primitives", () => {
  it("EmptyState and Skeleton have no axe violations", async () => {
    const { container } = render(
      <main>
        <h1>Page</h1>
        <EmptyState icon={Inbox} title="Nothing here yet" description="Come back later." status={<ScopeBadge scope="planned" />} />
        <Skeleton className="h-4 w-10" />
      </main>,
    );
    await expectNoAxeViolations(container);
  });
});
