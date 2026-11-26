import type { RecoveryVariant, TrialState } from "../types/api";

export interface JudgeFooterProps {
  variant: RecoveryVariant | null;
  visitorSuffix: string | null;
  cohort: string | null;
  trialState: TrialState | null;
  serverSynced: boolean;
  serverOffsetMs: number;
}

function formatVariationLabel(variant: RecoveryVariant | null): string {
  if (!variant) {
    return "Pending exposure";
  }
  if (variant === "check_status" || variant === "check-status-first") {
    return "check-status-first (check_status)";
  }
  return "control (retry)";
}

export function JudgeFooter({
  variant,
  visitorSuffix,
  cohort,
  trialState,
  serverSynced,
  serverOffsetMs,
}: JudgeFooterProps) {
  return (
    <footer className="judge-footer">
      <details className="judge-details">
        <summary className="judge-summary">Trial details</summary>
        <dl className="judge-grid">
          <div className="judge-row">
            <dt>Flagsmith variation</dt>
            <dd>{formatVariationLabel(variant)}</dd>
          </div>
          <div className="judge-row">
            <dt>Visitor suffix</dt>
            <dd className="mono">{visitorSuffix ?? "—"}</dd>
          </div>
          <div className="judge-row">
            <dt>Cohort</dt>
            <dd>{cohort ?? "Pending"}</dd>
          </div>
          <div className="judge-row">
            <dt>Clock origin</dt>
            <dd>
              {serverSynced
                ? `Server-synced (offset ${serverOffsetMs >= 0 ? "+" : ""}${Math.round(
                    serverOffsetMs
                  )} ms)`
                : "Awaiting server sync"}
            </dd>
          </div>
          <div className="judge-row">
            <dt>Trial state</dt>
            <dd className="mono">{trialState ?? "ready"}</dd>
          </div>
        </dl>
      </details>
    </footer>
  );
}
