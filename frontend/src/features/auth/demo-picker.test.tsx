import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { DemoOperatorOut } from "../../api/generated/model";
import { expectNoAxeViolations } from "../../test/axe";
import { DemoPicker } from "./demo-picker";

const OPERATORS: DemoOperatorOut[] = [
  {
    id: "op-1",
    email: "ana@example.test",
    display_name: "Ana Souza",
    memberships: [{ project_id: "p-1", project_name: "Acme Payments", role: "owner" }],
  },
  { id: "op-2", email: "diego@example.test", display_name: "Diego Viewer", memberships: [] },
];

describe("DemoPicker", () => {
  it("offers one full-name Continue button per persona and reports the pick", async () => {
    const onPick = vi.fn();
    const { container } = render(<DemoPicker operators={OPERATORS} pendingId={null} disabled={false} onPick={onPick} />);
    await userEvent.click(screen.getByRole("button", { name: "Continue as Ana Souza" }));
    expect(onPick).toHaveBeenCalledWith(OPERATORS[0]);
    expect(screen.getByText("Not a member of any project yet.")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows the loading state on the clicked card only", () => {
    render(<DemoPicker operators={OPERATORS} pendingId="op-1" disabled onPick={vi.fn()} />);
    const cards = screen.getAllByRole("listitem").filter((item) => item.hasAttribute("aria-busy") || item.querySelector("button"));
    expect(cards.filter((item) => item.getAttribute("aria-busy") === "true")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Continue as Diego Viewer" })).toBeDisabled();
    expect(screen.getByText("Signing in…")).toBeInTheDocument();
  });
});
