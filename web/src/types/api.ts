export type TrialState =
  | "created"
  | "recovering"
  | "receipt_ready"
  | "finished"
  | "expired";

export type BackendVariant = "retry" | "check_status";
export type RecoveryVariant =
  | BackendVariant
  | "control"
  | "check-status-first";
export type FailureMode = "reply_lost" | "request_lost";

export interface SessionResponse {
  visitorSuffix: string;
  activeTrialId: string | null;
  serverNow: string;
}

export interface CreateTrialResponse {
  trialId: string;
  state: TrialState;
  title: string;
}

export interface SubmitInitialResponse {
  state: "recovering";
  variant: BackendVariant;
  exposedAt: string;
  deadlineAt: string;
  serverNow: string;
}

export interface SubmitRetryResponse {
  state: "receipt_ready";
  receiptCode: string;
}

export type CheckStatusResponse =
  | { found: true; receiptCode: string }
  | { found: false; nextAction: "retry" };

export interface TrialDetailResponse {
  trialId: string;
  state: TrialState;
  title: string;
  cohort: string;
  variant: BackendVariant | null;
  allowedActions: string[];
  receiptCode: string | null;
  exposedAt: string | null;
  deadlineAt: string | null;
  checkedEmptyAt: string | null;
  displayedAt: string | null;
  closedAt: string | null;
  serverNow: string;
  failureMode: FailureMode | null;
  finalEntryCount: number | null;
}

export interface ReceiptResponse {
  receiptCode: string;
  title: string;
  savedAt: string;
}

export interface DisplayedResponse {
  state: "finished" | "expired";
  receiptCode: string;
  failureMode: FailureMode;
  finalEntryCount: number;
  primaryConverted: boolean;
  displayedAt: string | null;
  closedAt: string | null;
  serverNow: string;
}
