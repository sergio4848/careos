import type { VoiceCallView } from "@careos/contracts";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CALL_STATUS_LABELS, VoiceEscalationView } from "./VoiceEscalationPanel";

function call(overrides: Partial<VoiceCallView> = {}): VoiceCallView {
  return {
    id: "call-1",
    target_type: "SERVICE_USER",
    target_label: "Service user",
    to_number_masked: "+44*******018",
    provider: "twilio",
    direction: "OUTBOUND",
    status: "IN_PROGRESS",
    acknowledged: false,
    structured_response: null,
    attempt: 1,
    started_at: "2026-09-17T10:00:00Z",
    answered_at: "2026-09-17T10:00:10Z",
    ended_at: null,
    duration_seconds: null,
    failure_category: null,
    ai: {
      status: "COMPLETED",
      urgency_signal: "ASSISTANCE_REQUESTED",
      contact_established: true,
      requested_human_help: true,
      language: "en-GB",
      summary: "Margaret answered and requested assistance.",
      provider: "openai-realtime",
      disclaimer: "AI ADVISORY — HUMAN REVIEW REQUIRED",
    },
    ...overrides,
  };
}

const noop = () => {};

describe("VoiceEscalationView", () => {
  it("shows live call state, masked number and the clearly-labelled AI advisory", () => {
    render(
      <VoiceEscalationView
        calls={[call()]}
        canControl
        onStopCall={noop}
        onEscalateNow={noop}
        stoppingId={null}
        escalating={false}
      />,
    );
    expect(screen.getByText("CONNECTED")).toBeInTheDocument();
    expect(screen.getByText("+44*******018")).toBeInTheDocument();
    expect(screen.getByText("AI ADVISORY — HUMAN REVIEW REQUIRED")).toBeInTheDocument();
    expect(screen.getByText("Assistance requested")).toBeInTheDocument();
    expect(screen.getByText("Margaret answered and requested assistance.")).toBeInTheDocument();
    expect(screen.getByText("Asked for human help")).toBeInTheDocument();
  });

  it("maps every internal status to an operator label", () => {
    const expectations: Array<[VoiceCallView["status"], string]> = [
      ["QUEUED", "CALLING"],
      ["RINGING", "RINGING"],
      ["IN_PROGRESS", "CONNECTED"],
      ["COMPLETED", "ENDED"],
      ["FAILED", "FAILED"],
      ["NO_ANSWER", "NO ANSWER"],
      ["CANCELLED", "STOPPED"],
      ["TIMED_OUT", "TIMED OUT"],
    ];
    for (const [status, label] of expectations) {
      expect(CALL_STATUS_LABELS[status].label).toBe(label);
    }
  });

  it("offers stop and escalate controls to operators — and nothing that auto-resolves", async () => {
    const onStop = vi.fn();
    const onEscalate = vi.fn();
    render(
      <VoiceEscalationView
        calls={[call()]}
        canControl
        onStopCall={onStop}
        onEscalateNow={onEscalate}
        stoppingId={null}
        escalating={false}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Stop automated call" }));
    expect(onStop).toHaveBeenCalledWith("call-1");
    await userEvent.click(screen.getByRole("button", { name: "Escalate now" }));
    expect(onEscalate).toHaveBeenCalledTimes(1);
    // The AI is never offered a resolution shortcut.
    for (const forbidden of [/mark safe/i, /auto.?resolve/i, /ai.*close/i]) {
      expect(screen.queryByRole("button", { name: forbidden })).not.toBeInTheDocument();
    }
  });

  it("hides controls from read-only users and on ended calls", () => {
    render(
      <VoiceEscalationView
        calls={[call({ status: "COMPLETED", duration_seconds: 35, ai: null })]}
        canControl={false}
        onStopCall={noop}
        onEscalateNow={noop}
        stoppingId={null}
        escalating={false}
      />,
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText("ENDED")).toBeInTheDocument();
    expect(screen.getByText("0:35 min")).toBeInTheDocument();
  });

  it("explains structured keypad responses and failures", () => {
    render(
      <VoiceEscalationView
        calls={[
          call({
            id: "call-2",
            target_label: "Trusted contact · Sarah Wilson (Daughter)",
            status: "COMPLETED",
            structured_response: "REQUEST_OPERATOR",
            ai: null,
          }),
          call({ id: "call-3", status: "FAILED", failure_category: "provider_error", ai: null }),
        ]}
        canControl={false}
        onStopCall={noop}
        onEscalateNow={noop}
        stoppingId={null}
        escalating={false}
      />,
    );
    expect(screen.getByText("Trusted contact · Sarah Wilson (Daughter)")).toBeInTheDocument();
    expect(screen.getByText("Keypad: Request operator")).toBeInTheDocument();
    expect(screen.getByText("FAILED")).toBeInTheDocument();
    expect(screen.getByText("Provider error")).toBeInTheDocument();
  });
});
