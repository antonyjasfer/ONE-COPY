import type {
  CheckStatusResponse,
  CreateTrialResponse,
  DisplayedResponse,
  ReceiptResponse,
  SessionResponse,
  SubmitInitialResponse,
  SubmitRetryResponse,
  TrialDetailResponse,
} from "../types/api";

export class ApiHttpError extends Error {
  status: number;
  body: Record<string, unknown>;

  constructor(status: number, message: string, body: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiHttpError";
    this.status = status;
    this.body = body;
  }
}

export class ApiNetworkError extends Error {
  constructor(message = "Network connection lost. Please try again.") {
    super(message);
    this.name = "ApiNetworkError";
  }
}

const TRANSIENT_HTTP_STATUSES = new Set([502, 503, 504]);
const RETRY_DELAYS_MS = [600, 1400];

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function parseJsonSafe(response: Response): Promise<Record<string, unknown>> {
  try {
    return (await response.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

async function requestJson<T>(
  path: string,
  init: RequestInit = {},
  options: { onReconnecting?: (reconnecting: boolean) => void; retryOnNetwork?: boolean } = {}
): Promise<T> {
  const { onReconnecting, retryOnNetwork = false } = options;
  const maxAttempts = retryOnNetwork ? RETRY_DELAYS_MS.length + 1 : 1;

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try {
      const response = await fetch(path, {
        ...init,
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          ...(init.headers ?? {}),
        },
      });

      if (TRANSIENT_HTTP_STATUSES.has(response.status) && attempt < maxAttempts - 1) {
        onReconnecting?.(true);
        await sleep(RETRY_DELAYS_MS[attempt]);
        continue;
      }

      onReconnecting?.(false);

      if (!response.ok) {
        const body = await parseJsonSafe(response);
        const detail =
          typeof body.detail === "string"
            ? body.detail
            : `Request failed (${response.status})`;
        throw new ApiHttpError(response.status, detail, body);
      }

      return (await response.json()) as T;
    } catch (err) {
      if (err instanceof ApiHttpError) {
        throw err;
      }
      if (attempt < maxAttempts - 1) {
        onReconnecting?.(true);
        await sleep(RETRY_DELAYS_MS[attempt]);
        continue;
      }
      onReconnecting?.(false);
      throw new ApiNetworkError(
        err instanceof Error ? err.message : "Network connection interrupted."
      );
    }
  }

  onReconnecting?.(false);
  throw new ApiNetworkError();
}

export function computeServerOffsetMs(serverNowIso: string): number {
  const serverMs = Date.parse(serverNowIso);
  if (Number.isNaN(serverMs)) {
    return 0;
  }
  return serverMs - Date.now();
}

export function initSession(): Promise<SessionResponse> {
  return requestJson<SessionResponse>("/api/session", { method: "POST" }, { retryOnNetwork: true });
}

export function createTrial(title: string): Promise<CreateTrialResponse> {
  return requestJson<CreateTrialResponse>("/api/trials", {
    method: "POST",
    body: JSON.stringify({ title }),
  });
}

export function submitTrialInitial(
  trialId: string,
  actionId: string,
  onReconnecting?: (reconnecting: boolean) => void
): Promise<SubmitInitialResponse> {
  return requestJson<SubmitInitialResponse>(
    `/api/trials/${encodeURIComponent(trialId)}/submit`,
    {
      method: "POST",
      body: JSON.stringify({ actionId, intent: "initial" }),
    },
    { onReconnecting, retryOnNetwork: true }
  );
}

export function submitTrialRetry(
  trialId: string,
  actionId: string,
  onReconnecting?: (reconnecting: boolean) => void
): Promise<SubmitRetryResponse> {
  return requestJson<SubmitRetryResponse>(
    `/api/trials/${encodeURIComponent(trialId)}/submit`,
    {
      method: "POST",
      body: JSON.stringify({ actionId, intent: "retry" }),
    },
    { onReconnecting, retryOnNetwork: true }
  );
}

export function checkTrialStatus(
  trialId: string,
  actionId: string,
  onReconnecting?: (reconnecting: boolean) => void
): Promise<CheckStatusResponse> {
  return requestJson<CheckStatusResponse>(
    `/api/trials/${encodeURIComponent(trialId)}/check-status`,
    {
      method: "POST",
      body: JSON.stringify({ actionId }),
    },
    { onReconnecting, retryOnNetwork: true }
  );
}

export function getTrial(trialId: string): Promise<TrialDetailResponse> {
  return requestJson<TrialDetailResponse>(
    `/api/trials/${encodeURIComponent(trialId)}`,
    { method: "GET" },
    { retryOnNetwork: true }
  );
}

export function getReceipt(receiptCode: string): Promise<ReceiptResponse> {
  return requestJson<ReceiptResponse>(
    `/api/receipts/${encodeURIComponent(receiptCode)}`,
    { method: "GET" },
    { retryOnNetwork: true }
  );
}

export function markReceiptDisplayed(
  receiptCode: string,
  actionId: string,
  onReconnecting?: (reconnecting: boolean) => void
): Promise<DisplayedResponse> {
  return requestJson<DisplayedResponse>(
    `/api/receipts/${encodeURIComponent(receiptCode)}/displayed`,
    {
      method: "POST",
      body: JSON.stringify({ actionId }),
    },
    { onReconnecting, retryOnNetwork: true }
  );
}
