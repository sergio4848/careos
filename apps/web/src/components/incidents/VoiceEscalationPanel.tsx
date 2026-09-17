"use client";

import type { CallStatus, UrgencySignal, VoiceCallView } from "@careos/contracts";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorNotice, Panel } from "@/components/ui/Panel";
import { useEscalateNow, useIncidentCalls, useStopCall } from "@/lib/api/queries";
import { humanise } from "@/lib/format";

import styles from "./VoiceEscalationPanel.module.css";

type Tone = "critical" | "warning" | "success" | "info" | "neutral";

/** Operator-facing live labels for the internal call lifecycle. */
export const CALL_STATUS_LABELS: Record<CallStatus, { label: string; tone: Tone; live: boolean }> = {
  QUEUED: { label: "CALLING", tone: "info", live: true },
  INITIATED: { label: "CALLING", tone: "info", live: true },
  RINGING: { label: "RINGING", tone: "warning", live: true },
  ANSWERED: { label: "CONNECTED", tone: "success", live: true },
  IN_PROGRESS: { label: "CONNECTED", tone: "success", live: true },
  COMPLETED: { label: "ENDED", tone: "neutral", live: false },
  NO_ANSWER: { label: "NO ANSWER", tone: "warning", live: false },
  BUSY: { label: "BUSY", tone: "warning", live: false },
  FAILED: { label: "FAILED", tone: "critical", live: false },
  CANCELLED: { label: "STOPPED", tone: "neutral", live: false },
  TIMED_OUT: { label: "TIMED OUT", tone: "warning", live: false },
};

const URGENCY_TONE: Record<UrgencySignal, Tone> = {
  NONE: "neutral",
  ASSISTANCE_REQUESTED: "warning",
  POTENTIAL_EMERGENCY: "critical",
};

const AI_STATE_LABEL: Record<string, string> = {
  STARTED: "AI SESSION ACTIVE",
  COMPLETED: "AI SESSION COMPLETE",
  FAILED: "AI UNAVAILABLE",
  TIMED_OUT: "AI TIMED OUT",
};

function formatDuration(seconds: number | null): string | null {
  if (seconds === null) return null;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")} min`;
}

export function VoiceEscalationView({
  calls,
  canControl,
  onStopCall,
  onEscalateNow,
  stoppingId,
  escalating,
}: {
  calls: VoiceCallView[];
  canControl: boolean;
  onStopCall: (callId: string) => void;
  onEscalateNow: () => void;
  stoppingId: string | null;
  escalating: boolean;
}) {
  return (
    <div>
      {canControl ? (
        <div className={styles.header}>
          <Button size="sm" variant="danger" loading={escalating} onClick={onEscalateNow}>
            Escalate now
          </Button>
        </div>
      ) : null}
      {calls.length === 0 ? (
        <EmptyState>No automated calls yet. The escalation plan drives them.</EmptyState>
      ) : (
        <ul className={styles.list}>
          {calls.map((call) => {
            const status = CALL_STATUS_LABELS[call.status];
            return (
              <li key={call.id} className={styles.call}>
                <div className={styles.head}>
                  <span className={styles.target}>{call.target_label}</span>
                  {call.to_number_masked ? (
                    <span className={styles.number}>{call.to_number_masked}</span>
                  ) : null}
                  <span className={styles.spacer} />
                  <Badge tone={status.tone} strong={status.live}>
                    {status.label}
                  </Badge>
                  {canControl && status.live ? (
                    <Button
                      size="sm"
                      variant="secondary"
                      loading={stoppingId === call.id}
                      onClick={() => onStopCall(call.id)}
                    >
                      Stop automated call
                    </Button>
                  ) : null}
                </div>
                <div className={styles.meta}>
                  <span>Provider {call.provider}</span>
                  {call.attempt > 1 ? <span>Attempt {call.attempt}</span> : null}
                  {formatDuration(call.duration_seconds) ? (
                    <span className="tabular">{formatDuration(call.duration_seconds)}</span>
                  ) : null}
                  {call.failure_category ? <span>{humanise(call.failure_category)}</span> : null}
                  {call.structured_response ? (
                    <span>Keypad: {humanise(call.structured_response)}</span>
                  ) : null}
                  {call.acknowledged ? <span>Confirmed by the person</span> : null}
                </div>
                {call.ai ? (
                  <div className={styles.advisory}>
                    <span className={styles.advisoryLabel}>{call.ai.disclaimer}</span>
                    <div className={styles.advisoryFacts}>
                      <span>{AI_STATE_LABEL[call.ai.status] ?? call.ai.status}</span>
                      {call.ai.urgency_signal ? (
                        <Badge tone={URGENCY_TONE[call.ai.urgency_signal]}>
                          {humanise(call.ai.urgency_signal)}
                        </Badge>
                      ) : null}
                      {call.ai.contact_established !== null ? (
                        <span>
                          {call.ai.contact_established ? "Contact established" : "No contact"}
                        </span>
                      ) : null}
                      {call.ai.requested_human_help ? <span>Asked for human help</span> : null}
                    </div>
                    {call.ai.summary ? (
                      <p className={styles.advisorySummary}>{call.ai.summary}</p>
                    ) : null}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/** Live voice escalation for one incident: calls, provider state and AI advisories. */
export function VoiceEscalationPanel({
  incidentId,
  canControl,
}: {
  incidentId: string;
  canControl: boolean;
}) {
  const calls = useIncidentCalls(incidentId);
  const stop = useStopCall();
  const escalate = useEscalateNow();
  return (
    <Panel id="voice-escalation" title="Voice escalation">
      {calls.error ? <ErrorNotice error={calls.error} /> : null}
      <VoiceEscalationView
        calls={calls.data ?? []}
        canControl={canControl}
        onStopCall={(callId) => stop.mutate({ incidentId, callId })}
        onEscalateNow={() => escalate.mutate({ incidentId })}
        stoppingId={stop.isPending ? (stop.variables?.callId ?? null) : null}
        escalating={escalate.isPending}
      />
    </Panel>
  );
}
