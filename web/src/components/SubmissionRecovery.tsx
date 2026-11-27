import { useEffect, useMemo, useRef, useState } from "react";
import type { FailureMode, ReceiptResponse, RecoveryVariant } from "../types/api";

export type RecoveryPhase =
  | "sending"
  | "dropped"
  | "recovering"
  | "receipt"
  | "expired";

export interface RevealData {
  state: "finished" | "expired";
  failureMode: FailureMode | null;
  finalEntryCount: number | null;
  primaryConverted?: boolean;
  exposedAt?: string | null;
  checkedEmptyAt?: string | null;
  displayedAt?: string | null;
  closedAt?: string | null;
}

export interface SubmissionRecoveryProps {
  variant: RecoveryVariant | null;
  title: string;
  phase: RecoveryPhase;
  deadlineAt: string | null;
  serverOffsetMs: number;
  checkedEmpty: boolean;
  receipt: ReceiptResponse | null;
  reveal: RevealData | null;
  busyAction: "check_status" | "retry" | "displayed" | null;
  reconnecting: boolean;
  errorMessage: string | null;
  onCheckStatus: () => Promise<void> | void;
  onRetry: () => Promise<void> | void;
  onReceiptVisible: (receiptCode: string) => Promise<void> | void;
  onExpired: () => Promise<void> | void;
  onStartPractice?: () => void;
}

function isCheckStatusVariant(variant: RecoveryVariant | null): boolean {
  return variant === "check_status" || variant === "check-status-first";
}

function formatCountdown(remainingMs: number): string {
  const totalSec = Math.max(0, Math.ceil(remainingMs / 1000));
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) {
    return "—";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function describeFailureMode(mode: FailureMode | null): string {
  if (mode === "reply_lost") {
    return "Reply lost (entry #1 was already saved on the server before the response dropped)";
  }
  if (mode === "request_lost") {
    return "Request lost (initial submission never reached the server)";
  }
  return "Unknown";
}

export function SubmissionRecovery({
  variant,
  title,
  phase,
  deadlineAt,
  serverOffsetMs,
  checkedEmpty,
  receipt,
  reveal,
  busyAction,
  reconnecting,
  errorMessage,
  onCheckStatus,
  onRetry,
  onReceiptVisible,
  onExpired,
  onStartPractice,
}: SubmissionRecoveryProps) {
  const [remainingMs, setRemainingMs] = useState<number>(() => {
    if (!deadlineAt) return 60_000;
    return Math.max(0, Date.parse(deadlineAt) - (Date.now() + serverOffsetMs));
  });
  const [timerAnnouncement, setTimerAnnouncement] = useState<string>("");

  const announcedMilestonesRef = useRef<Set<number>>(new Set());
  const expiryTriggeredRef = useRef<boolean>(false);
  const receiptCardRef = useRef<HTMLDivElement | null>(null);
  const reportedReceiptRef = useRef<string | null>(null);

  const isTerminal = phase === "expired" || reveal !== null;
  const timerRunning = Boolean(deadlineAt) && !isTerminal;

  // Server-synchronized countdown timer with polite announcements ONLY at 30s and 10s
  useEffect(() => {
    if (!deadlineAt) {
      return;
    }

    const computeRemaining = () =>
      Math.max(0, Date.parse(deadlineAt) - (Date.now() + serverOffsetMs));

    const tick = () => {
      const next = computeRemaining();
      setRemainingMs(next);

      if (!isTerminal) {
        const wholeSeconds = Math.ceil(next / 1000);
        if (wholeSeconds === 30 && !announcedMilestonesRef.current.has(30)) {
          announcedMilestonesRef.current.add(30);
          setTimerAnnouncement("30 seconds remaining in recovery window.");
        } else if (wholeSeconds === 10 && !announcedMilestonesRef.current.has(10)) {
          announcedMilestonesRef.current.add(10);
          setTimerAnnouncement("10 seconds remaining in recovery window.");
        }

        if (next <= 0 && !expiryTriggeredRef.current) {
          expiryTriggeredRef.current = true;
          void onExpired();
        }
      }
    };

    tick();
    if (!timerRunning) {
      return;
    }

    const intervalId = window.setInterval(tick, 250);
    return () => window.clearInterval(intervalId);
  }, [deadlineAt, isTerminal, onExpired, serverOffsetMs, timerRunning]);

  // Fire POST /api/receipts/{code}/displayed ONLY when receipt is visibly rendered in an active tab
  useEffect(() => {
    const code = receipt?.receiptCode;
    const node = receiptCardRef.current;
    if (phase !== "receipt" || !code || !node || reveal !== null) {
      return;
    }
    if (reportedReceiptRef.current === code) {
      return;
    }

    let isIntersecting = false;

    const tryReportVisible = () => {
      if (
        reportedReceiptRef.current !== code &&
        isIntersecting &&
        document.visibilityState === "visible"
      ) {
        reportedReceiptRef.current = code;
        void onReceiptVisible(code);
      }
    };

    const observer = new IntersectionObserver(
      (entries) => {
        isIntersecting = entries.some(
          (entry) => entry.isIntersecting && entry.intersectionRatio > 0
        );
        tryReportVisible();
      },
      { threshold: 0.1 }
    );
    observer.observe(node);

    const handleVisibilityChange = () => {
      tryReportVisible();
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      observer.disconnect();
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [onReceiptVisible, phase, receipt?.receiptCode, reveal]);

  const checkStatusFirst = useMemo(() => isCheckStatusVariant(variant), [variant]);
  const showTimer = phase !== "sending";

  return (
    <section className="panel" aria-labelledby="recovery-heading">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Submission status</p>
          <h1 id="recovery-heading" className="heading">
            {phase === "sending" && "Sending your entry..."}
            {phase === "dropped" && "We couldn't confirm your submission."}
            {phase === "recovering" && "We couldn't confirm your submission."}
            {phase === "receipt" && "Receipt verified"}
            {phase === "expired" && "Time's up"}
          </h1>
        </div>
        {showTimer && (
          <div className="timer-block">
            <span className="timer-label">
              {isTerminal ? "Window closed" : "Remaining"}
            </span>
            <span className="timer-value" aria-hidden="true">
              {formatCountdown(remainingMs)}
            </span>
          </div>
        )}
      </div>

      {/* Polite announcements for timer milestones (30s and 10s only) and phase changes */}
      <div className="sr-only" aria-live="polite" aria-atomic="true">
        {timerAnnouncement}
      </div>

      <div className="entry-summary">
        <span className="entry-summary-label">Entry title</span>
        <span className="entry-summary-title">{title}</span>
      </div>

      {reconnecting && (
        <p className="status-note" role="status" aria-live="polite">
          Reconnecting...
        </p>
      )}

      {errorMessage && (
        <p className="status-note status-note--muted" role="status" aria-live="polite">
          {errorMessage}
        </p>
      )}

      {/* Screen 2: Ambiguous connection drop (calm, NO red error icon) */}
      {phase === "sending" && (
        <div className="status-card" role="status" aria-live="polite">
          <span className="neutral-indicator" aria-hidden="true" />
          <p className="body-text">Sending...</p>
        </div>
      )}

      {phase === "dropped" && (
        <div className="status-card" role="status" aria-live="polite">
          <span className="neutral-dot" aria-hidden="true" />
          <p className="body-text">
            The connection dropped before a receipt arrived. Preparing recovery options...
          </p>
        </div>
      )}

      {/* Screen 3: Recovery branching */}
      {phase === "recovering" && (
        <div className="recovery-body" role="region" aria-live="polite">
          <p className="body-text">Your entry may already be saved.</p>

          {checkStatusFirst && !checkedEmpty && (
            <>
              <p className="muted-text">
                Check whether the server already recorded your entry before sending another copy.
              </p>
              <button
                type="button"
                className="btn-primary"
                disabled={busyAction !== null}
                onClick={() => void onCheckStatus()}
              >
                {busyAction === "check_status"
                  ? reconnecting
                    ? "Reconnecting..."
                    : "Checking status..."
                  : "Check submission status"}
              </button>
            </>
          )}

          {checkStatusFirst && checkedEmpty && (
            <>
              <p className="muted-text">
                No entry found. You can submit it again.
              </p>
              <button
                type="button"
                className="btn-primary"
                disabled={busyAction !== null}
                onClick={() => void onRetry()}
              >
                {busyAction === "retry"
                  ? reconnecting
                    ? "Reconnecting..."
                    : "Submitting..."
                  : "Submit again"}
              </button>
            </>
          )}

          {!checkStatusFirst && (
            <>
              <p className="muted-text">
                Send your entry again to make sure it reaches the server.
              </p>
              <button
                type="button"
                className="btn-primary"
                disabled={busyAction !== null}
                onClick={() => void onRetry()}
              >
                {busyAction === "retry"
                  ? reconnecting
                    ? "Reconnecting..."
                    : "Trying again..."
                  : "Try again"}
              </button>
            </>
          )}
        </div>
      )}

      {/* Screen 4: Receipt & Reveal */}
      {receipt && (phase === "receipt" || phase === "expired") && (
        <div ref={receiptCardRef} className="receipt-card" role="status" aria-live="polite">
          <div className="receipt-row">
            <span className="receipt-label">Receipt verified</span>
            <code className="receipt-code">{receipt.receiptCode}</code>
          </div>
          <div className="receipt-meta">
            <span>Saved at {formatTimestamp(receipt.savedAt)}</span>
          </div>
        </div>
      )}

      {reveal && (
        <div className="reveal-card">
          <h2 className="reveal-heading">What the server actually saved</h2>
          <dl className="reveal-list">
            <div className="reveal-item">
              <dt>Failure mode</dt>
              <dd>{describeFailureMode(reveal.failureMode)}</dd>
            </div>
            <div className="reveal-item">
              <dt>Actual saved entries</dt>
              <dd>
                {reveal.finalEntryCount === 1
                  ? "1 entry saved (clean single copy)"
                  : `${reveal.finalEntryCount ?? 0} entries saved${
                      (reveal.finalEntryCount ?? 0) >= 2 ? " (duplicate created)" : ""
                    }`}
              </dd>
            </div>
            <div className="reveal-item">
              <dt>Timeline</dt>
              <dd className="timeline-list">
                <span>Exposed: {formatTimestamp(reveal.exposedAt)}</span>
                {reveal.checkedEmptyAt && (
                  <span>Checked empty: {formatTimestamp(reveal.checkedEmptyAt)}</span>
                )}
                {reveal.displayedAt && (
                  <span>Receipt displayed: {formatTimestamp(reveal.displayedAt)}</span>
                )}
                <span>Closed: {formatTimestamp(reveal.closedAt)}</span>
              </dd>
            </div>
          </dl>

          {onStartPractice && (
            <button
              type="button"
              className="btn-secondary"
              onClick={onStartPractice}
            >
              Try a practice run
            </button>
          )}
        </div>
      )}
    </section>
  );
}
