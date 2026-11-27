import { useCallback, useEffect, useRef, useState } from "react";
import { JudgeFooter } from "./components/JudgeFooter";
import {
  SubmissionRecovery,
  type RecoveryPhase,
  type RevealData,
} from "./components/SubmissionRecovery";
import {
  ApiHttpError,
  ApiNetworkError,
  checkTrialStatus,
  computeServerOffsetMs,
  createTrial,
  getReceipt,
  getTrial,
  initSession,
  markReceiptDisplayed,
  submitTrialInitial,
  submitTrialRetry,
} from "./lib/api";
import type {
  ReceiptResponse,
  RecoveryVariant,
  TrialDetailResponse,
  TrialState,
} from "./types/api";

const DEFAULT_TITLE = "Spidey Sense demo entry";

function newActionId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export function App() {
  const [sessionReady, setSessionReady] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [visitorSuffix, setVisitorSuffix] = useState<string | null>(null);
  const [serverSynced, setServerSynced] = useState(false);
  const [serverOffsetMs, setServerOffsetMs] = useState(0);

  const [title, setTitle] = useState(DEFAULT_TITLE);
  const [trialId, setTrialId] = useState<string | null>(null);
  const [trialState, setTrialState] = useState<TrialState | null>(null);
  const [cohort, setCohort] = useState<string | null>(null);
  const [variant, setVariant] = useState<RecoveryVariant | null>(null);
  const [exposedAt, setExposedAt] = useState<string | null>(null);
  const [deadlineAt, setDeadlineAt] = useState<string | null>(null);
  const [checkedEmptyAt, setCheckedEmptyAt] = useState<string | null>(null);
  const [checkedEmpty, setCheckedEmpty] = useState(false);

  const [phase, setPhase] = useState<"ready" | RecoveryPhase>("ready");
  const [receipt, setReceipt] = useState<ReceiptResponse | null>(null);
  const [reveal, setReveal] = useState<RevealData | null>(null);
  const [busyAction, setBusyAction] = useState<
    "initial" | "check_status" | "retry" | "displayed" | null
  >(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [practiceNotice, setPracticeNotice] = useState(false);

  // Separate actionId refs per deliberate user intent so real network errors
  // reuse the exact same actionId, while deliberate new actions get a fresh UUID.
  const initialActionIdRef = useRef<string | null>(null);
  const checkActionIdRef = useRef<string | null>(null);
  const retryActionIdRef = useRef<string | null>(null);
  const displayedActionIdRef = useRef<string | null>(null);
  const dropTimerRef = useRef<number | null>(null);

  const applyServerTime = useCallback((serverNow: string) => {
    setServerOffsetMs(computeServerOffsetMs(serverNow));
    setServerSynced(true);
  }, []);

  const hydrateFromTrialDetail = useCallback(
    async (detail: TrialDetailResponse) => {
      applyServerTime(detail.serverNow);
      setTrialId(detail.trialId);
      setTrialState(detail.state);
      setTitle(detail.title);
      setCohort(detail.cohort === "pending" ? null : detail.cohort);
      setVariant(detail.variant);
      setExposedAt(detail.exposedAt);
      setDeadlineAt(detail.deadlineAt);
      setCheckedEmptyAt(detail.checkedEmptyAt);
      setCheckedEmpty(Boolean(detail.checkedEmptyAt));

      if (detail.state === "created") {
        setPhase("ready");
        return;
      }

      if (detail.state === "recovering") {
        setPhase("recovering");
        return;
      }

      if (detail.receiptCode) {
        try {
          const rec = await getReceipt(detail.receiptCode);
          setReceipt(rec);
        } catch {
          setReceipt({
            receiptCode: detail.receiptCode,
            title: detail.title,
            savedAt: detail.displayedAt ?? detail.closedAt ?? detail.serverNow,
          });
        }
      }

      if (detail.state === "receipt_ready") {
        setPhase("receipt");
        return;
      }

      // Terminal states ("finished" | "expired")
      setReveal({
        state: detail.state === "expired" ? "expired" : "finished",
        failureMode: detail.failureMode,
        finalEntryCount: detail.finalEntryCount,
        primaryConverted: detail.state === "finished" && detail.finalEntryCount === 1,
        exposedAt: detail.exposedAt,
        checkedEmptyAt: detail.checkedEmptyAt,
        displayedAt: detail.displayedAt,
        closedAt: detail.closedAt,
      });
      setPhase(detail.state === "expired" ? "expired" : "receipt");
    },
    [applyServerTime]
  );

  const bootstrapSession = useCallback(async () => {
    setSessionError(null);
    try {
      const session = await initSession();
      applyServerTime(session.serverNow);
      setVisitorSuffix(session.visitorSuffix);

      if (session.activeTrialId) {
        const detail = await getTrial(session.activeTrialId);
        await hydrateFromTrialDetail(detail);
      }
      setSessionReady(true);
    } catch (err) {
      setSessionError(
        err instanceof Error
          ? err.message
          : "Unable to connect to the submission portal."
      );
    }
  }, [applyServerTime, hydrateFromTrialDetail]);

  useEffect(() => {
    void bootstrapSession();
    return () => {
      if (dropTimerRef.current !== null) {
        window.clearTimeout(dropTimerRef.current);
      }
    };
  }, [bootstrapSession]);

  const syncTrialFromServer = useCallback(
    async (id: string) => {
      const detail = await getTrial(id);
      await hydrateFromTrialDetail(detail);
    },
    [hydrateFromTrialDetail]
  );

  const handleSubmitEntry = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!sessionReady || busyAction !== null) {
      return;
    }

    setErrorMessage(null);
    setBusyAction("initial");

    try {
      const normalizedTitle = title.trim() || DEFAULT_TITLE;
      setTitle(normalizedTitle);

      let activeId = trialId;
      if (!activeId || trialState !== "created") {
        try {
          const created = await createTrial(normalizedTitle);
          activeId = created.trialId;
          setTrialId(created.trialId);
          setTrialState(created.state);
          setTitle(created.title);
        } catch (err) {
          if (
            err instanceof ApiHttpError &&
            err.status === 409 &&
            typeof err.body.activeTrialId === "string"
          ) {
            await syncTrialFromServer(err.body.activeTrialId);
            setBusyAction(null);
            return;
          }
          throw err;
        }
      }

      if (!initialActionIdRef.current) {
        initialActionIdRef.current = newActionId();
      }
      const actionId = initialActionIdRef.current;

      setPhase("sending");
      const res = await submitTrialInitial(activeId, actionId, setReconnecting);
      initialActionIdRef.current = null;

      applyServerTime(res.serverNow);
      setVariant(res.variant);
      setExposedAt(res.exposedAt);
      setDeadlineAt(res.deadlineAt);
      setTrialState("recovering");

      // Refresh trial detail in background? No: Screen 2 shows identical "dropped" beat first,
      // then opens Screen 3 recovery. Note: cohort is populated lazily when resumed or terminal.
      setPhase("dropped");
      dropTimerRef.current = window.setTimeout(() => {
        setPhase((current) => (current === "dropped" ? "recovering" : current));
      }, 850);
    } catch (err) {
      if (err instanceof ApiNetworkError) {
        setPhase("ready");
        setErrorMessage("Connection interrupted while sending. Click Submit entry to retry safely.");
      } else if (err instanceof ApiHttpError) {
        setPhase("ready");
        setErrorMessage(err.message);
      } else {
        setPhase("ready");
        setErrorMessage("Unexpected error submitting entry.");
      }
    } finally {
      setReconnecting(false);
      setBusyAction(null);
    }
  };

  const handleCheckStatus = useCallback(async () => {
    if (!trialId || busyAction !== null) {
      return;
    }

    setErrorMessage(null);
    setBusyAction("check_status");
    if (!checkActionIdRef.current) {
      checkActionIdRef.current = newActionId();
    }

    try {
      const res = await checkTrialStatus(
        trialId,
        checkActionIdRef.current,
        setReconnecting
      );
      checkActionIdRef.current = null;

      if (res.found) {
        setTrialState("receipt_ready");
        const rec = await getReceipt(res.receiptCode);
        setReceipt(rec);
        setPhase("receipt");
      } else {
        setCheckedEmpty(true);
        setCheckedEmptyAt((prev) => prev ?? new Date().toISOString());
      }
    } catch (err) {
      if (err instanceof ApiHttpError) {
        checkActionIdRef.current = null;
        if (err.status === 409 && err.body.state === "expired") {
          await syncTrialFromServer(trialId);
        } else {
          setErrorMessage(err.message);
        }
      } else if (err instanceof ApiNetworkError) {
        setErrorMessage("Connection interrupted. Click Check submission status to retry.");
      }
    } finally {
      setReconnecting(false);
      setBusyAction(null);
    }
  }, [busyAction, syncTrialFromServer, trialId]);

  const handleRetry = useCallback(async () => {
    if (!trialId || busyAction !== null) {
      return;
    }

    setErrorMessage(null);
    setBusyAction("retry");
    if (!retryActionIdRef.current) {
      retryActionIdRef.current = newActionId();
    }

    try {
      const res = await submitTrialRetry(
        trialId,
        retryActionIdRef.current,
        setReconnecting
      );
      retryActionIdRef.current = null;
      setTrialState("receipt_ready");
      const rec = await getReceipt(res.receiptCode);
      setReceipt(rec);
      setPhase("receipt");
    } catch (err) {
      if (err instanceof ApiHttpError) {
        retryActionIdRef.current = null;
        if (err.status === 409 && err.body.state === "expired") {
          await syncTrialFromServer(trialId);
        } else {
          setErrorMessage(err.message);
        }
      } else if (err instanceof ApiNetworkError) {
        setErrorMessage("Connection interrupted. Click again to retry safely.");
      }
    } finally {
      setReconnecting(false);
      setBusyAction(null);
    }
  }, [busyAction, syncTrialFromServer, trialId]);

  const handleReceiptVisible = useCallback(
    async (code: string) => {
      setBusyAction("displayed");
      if (!displayedActionIdRef.current) {
        displayedActionIdRef.current = newActionId();
      }

      try {
        const res = await markReceiptDisplayed(
          code,
          displayedActionIdRef.current,
          setReconnecting
        );
        displayedActionIdRef.current = null;
        applyServerTime(res.serverNow);
        setTrialState(res.state);
        setReveal({
          state: res.state,
          failureMode: res.failureMode,
          finalEntryCount: res.finalEntryCount,
          primaryConverted: res.primaryConverted,
          exposedAt,
          checkedEmptyAt,
          displayedAt: res.displayedAt,
          closedAt: res.closedAt,
        });
        if (res.state === "expired") {
          setPhase("expired");
        }
        // Refresh cohort from trial detail so JudgeFooter displays exact cohort
        if (trialId && !cohort) {
          try {
            const detail = await getTrial(trialId);
            setCohort(detail.cohort === "pending" ? null : detail.cohort);
          } catch {
            // Non-blocking metadata refresh
          }
        }
      } catch (err) {
        if (err instanceof ApiHttpError) {
          displayedActionIdRef.current = null;
          setErrorMessage(err.message);
        } else if (err instanceof ApiNetworkError) {
          setErrorMessage("Reconnecting to confirm receipt visibility...");
        }
      } finally {
        setReconnecting(false);
        setBusyAction(null);
      }
    },
    [applyServerTime, checkedEmptyAt, cohort, exposedAt, trialId]
  );

  const handleExpired = useCallback(async () => {
    if (!trialId) {
      return;
    }
    try {
      await syncTrialFromServer(trialId);
    } catch {
      setPhase("expired");
    }
  }, [syncTrialFromServer, trialId]);

  const handleStartPractice = useCallback(() => {
    if (dropTimerRef.current !== null) {
      window.clearTimeout(dropTimerRef.current);
      dropTimerRef.current = null;
    }
    initialActionIdRef.current = null;
    checkActionIdRef.current = null;
    retryActionIdRef.current = null;
    displayedActionIdRef.current = null;
    setTrialId(null);
    setTrialState(null);
    setCohort(null);
    setVariant(null);
    setExposedAt(null);
    setDeadlineAt(null);
    setCheckedEmptyAt(null);
    setCheckedEmpty(false);
    setReceipt(null);
    setReveal(null);
    setErrorMessage(null);
    setTitle(DEFAULT_TITLE);
    setPracticeNotice(true);
    setPhase("ready");
  }, []);

  return (
    <div className="page-shell">
      <main className="column">
        {phase === "ready" ? (
          <section className="panel" aria-labelledby="ready-heading">
            <div className="panel-header">
              <div>
                <p className="eyebrow">Submission portal</p>
                <h1 id="ready-heading" className="heading">
                  One minute before the deadline.
                </h1>
              </div>
              <div className="timer-block">
                <span className="timer-label">Recovery window</span>
                <span className="timer-value">1:00</span>
              </div>
            </div>

            <p className="muted-text">
              Clock starts only if your confirmation drops during recovery.
            </p>

            {sessionError && (
              <div className="status-note status-note--muted" role="status" aria-live="polite">
                <span>{sessionError} </span>
                <button
                  type="button"
                  className="btn-link"
                  onClick={() => void bootstrapSession()}
                >
                  Retry connection
                </button>
              </div>
            )}

            {practiceNotice && (
              <p className="status-note status-note--muted" role="status">
                Practice mode — your live experiment trial is already recorded.
              </p>
            )}

            {errorMessage && (
              <p className="status-note status-note--muted" role="status" aria-live="polite">
                {errorMessage}
              </p>
            )}

            <form className="ready-form" onSubmit={(e) => void handleSubmitEntry(e)}>
              <label htmlFor="entry-title" className="field-label">
                Submission title
              </label>
              <input
                id="entry-title"
                name="title"
                type="text"
                className="text-input"
                maxLength={80}
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                disabled={!sessionReady || busyAction !== null}
                placeholder={DEFAULT_TITLE}
              />

              <button
                type="submit"
                className="btn-primary"
                disabled={!sessionReady || busyAction !== null}
              >
                {!sessionReady
                  ? "Preparing portal..."
                  : busyAction === "initial"
                    ? reconnecting
                      ? "Reconnecting..."
                      : "Submitting..."
                    : "Submit entry"}
              </button>
            </form>
          </section>
        ) : (
          <SubmissionRecovery
            variant={variant}
            title={title}
            phase={phase}
            deadlineAt={deadlineAt}
            serverOffsetMs={serverOffsetMs}
            checkedEmpty={checkedEmpty}
            receipt={receipt}
            reveal={reveal}
            busyAction={busyAction === "initial" ? null : busyAction}
            reconnecting={reconnecting}
            errorMessage={errorMessage}
            onCheckStatus={handleCheckStatus}
            onRetry={handleRetry}
            onReceiptVisible={handleReceiptVisible}
            onExpired={handleExpired}
            onStartPractice={handleStartPractice}
          />
        )}

        <JudgeFooter
          variant={variant}
          visitorSuffix={visitorSuffix}
          cohort={cohort}
          trialState={trialState}
          serverSynced={serverSynced}
          serverOffsetMs={serverOffsetMs}
        />
      </main>
    </div>
  );
}

export default App;
