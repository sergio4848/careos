import type { IncidentSummary } from "@careos/contracts";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { IncidentCard } from "./IncidentCard";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

function incident(overrides: Partial<IncidentSummary> = {}): IncidentSummary {
  const now = new Date().toISOString();
  return {
    id: "inc-1",
    reference: "INC-260916-A0018A1C",
    status: "CONTACTING",
    priority: "CRITICAL",
    trigger_type: "SOS_BUTTON",
    is_active: true,
    created_at: new Date(Date.now() - 42_000).toISOString(),
    updated_at: now,
    acknowledged_at: null,
    resolved_at: null,
    closed_at: null,
    service_user: { id: "su-1", display_name: "Margaret Wilson", city: "London" },
    device: { id: "dev-1", external_id: "DEV-0001", device_type: "SOS_PENDANT", battery_level: 84, signal_strength: 92 },
    assignee: null,
    next_action: {
      step_order: 2,
      action_type: "CALL_TRUSTED_CONTACT",
      label: "Call trusted contact #1 · Sarah Wilson",
      due_at: new Date(Date.now() + 12_000).toISOString(),
    },
    ...overrides,
  };
}

describe("IncidentCard", () => {
  it("renders a critical, unacknowledged SOS with the operator-critical facts", () => {
    render(<IncidentCard incident={incident({ status: "OPEN" })} canTakeOver onTakeOver={vi.fn()} />);

    const card = screen.getByRole("article", { name: "Margaret Wilson" });
    expect(card.className).toMatch(/critical/);
    expect(card.className).toMatch(/unacknowledged/);
    expect(within(card).getByText("CRITICAL")).toBeInTheDocument();
    expect(within(card).getByText("SOS button")).toBeInTheDocument();
    expect(within(card).getByText("Open")).toBeInTheDocument();
    expect(within(card).getByText("DEV-0001")).toBeInTheDocument();
    expect(within(card).getByText("Elapsed")).toBeInTheDocument();
    expect(within(card).getByText(/Call trusted contact #1 · Sarah Wilson/)).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "View incident" })).toHaveAttribute("href", "/incidents/inc-1");
  });

  it("offers take over and reports the chosen incident", async () => {
    const onTakeOver = vi.fn();
    const data = incident();
    render(<IncidentCard incident={data} canTakeOver onTakeOver={onTakeOver} />);

    await userEvent.click(screen.getByRole("button", { name: "Take over" }));
    expect(onTakeOver).toHaveBeenCalledWith(data);
  });

  it("hides take over once an operator owns the incident", () => {
    render(
      <IncidentCard
        incident={incident({
          assignee: { id: "u-1", name: "Olivia Grant" },
          acknowledged_at: new Date().toISOString(),
          status: "IN_PROGRESS",
        })}
        canTakeOver
        onTakeOver={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Take over" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View incident" })).toBeInTheDocument();
    expect(screen.getByText("Olivia Grant")).toBeInTheDocument();
    expect(screen.getByRole("article").className).not.toMatch(/unacknowledged/);
  });
});
