import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EscalationDelayBanner, FreshnessIndicator } from "./DataHealth";

describe("EscalationDelayBanner", () => {
  it("is hidden while escalation runs on time", () => {
    const { container, rerender } = render(<EscalationDelayBanner overdue={0} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<EscalationDelayBanner overdue={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("tells operators to act manually when steps are overdue", () => {
    render(<EscalationDelayBanner overdue={3} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Automated escalation is delayed");
    expect(alert).toHaveTextContent("3 escalation steps are overdue");
    expect(alert).toHaveTextContent("manually");
  });
});

describe("FreshnessIndicator", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-17T10:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("flags the board as stale when refreshes stop succeeding", () => {
    const { container } = render(<FreshnessIndicator updatedAt={Date.now()} />);
    const indicator = container.querySelector("p")!;
    expect(indicator).toHaveAttribute("data-stale", "false");
    expect(indicator).toHaveTextContent("Updated just now");

    act(() => {
      vi.advanceTimersByTime(31_000);
    });
    expect(indicator).toHaveAttribute("data-stale", "true");
    expect(indicator).toHaveTextContent("Data may be out of date · last updated 31 s ago");
    expect(screen.getByRole("alert")).toHaveTextContent("Operations data may be out of date.");
  });
});
