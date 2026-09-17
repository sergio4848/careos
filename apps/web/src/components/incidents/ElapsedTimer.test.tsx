import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ElapsedTimer } from "./ElapsedTimer";

describe("ElapsedTimer", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-16T10:00:04Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("ticks every second from the incident creation time", () => {
    render(<ElapsedTimer since="2026-09-16T10:00:00Z" />);
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(screen.getByRole("time")).toHaveTextContent("00:05");
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(screen.getByRole("time")).toHaveTextContent("00:06");
    expect(screen.getByRole("time")).toHaveAttribute("aria-label", "Elapsed 6 seconds");
  });

  it("freezes at the resolution time", () => {
    render(<ElapsedTimer since="2026-09-16T10:00:00Z" until="2026-09-16T10:01:30Z" />);
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(screen.getByRole("time")).toHaveTextContent("01:30");
  });
});
