import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SystemStatusBannerView } from "./SystemStatusBanner";

describe("SystemStatusBannerView", () => {
  it("renders nothing when the console is healthy", () => {
    const { container } = render(<SystemStatusBannerView notice={null} onRetry={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("raises an alert when the API is unreachable", () => {
    render(
      <SystemStatusBannerView
        notice={{ severity: "critical", title: "CareOS server unreachable", detail: "Retrying.", canRetry: false }}
        onRetry={() => {}}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("CareOS server unreachable");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("lets the operator reconnect live updates immediately", async () => {
    const onRetry = vi.fn();
    render(
      <SystemStatusBannerView
        notice={{ severity: "warning", title: "Live updates interrupted", detail: "Polling.", canRetry: true }}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Live updates interrupted");
    await userEvent.click(screen.getByRole("button", { name: "Reconnect now" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
