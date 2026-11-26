from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.allocation import (
    generate_receipt_code,
    normalize_receipt_code,
    paired_failure_mode,
)
from app.config import Settings
from app.flagsmith_service import FlagEvaluation, FlagsmithService
from app.store import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    compute_allowed_actions,
)


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


class InMemoryTrialStore:
    """Faithful in-memory TrialStore implementation for fast offline unit tests."""

    def __init__(self) -> None:
        self.trials: dict[UUID, dict[str, Any]] = {}
        self.entries: list[dict[str, Any]] = []
        self.actions: dict[tuple[UUID, UUID], tuple[int, dict[str, Any]]] = {}
        self.allocation_counters: dict[tuple[str, str, str], int] = {}
        self.event_ledger: dict[tuple[UUID, str], dict[str, Any]] = {}

    async def health_check(self) -> bool:
        return True

    async def get_active_trial_id(self, visitor_id: UUID, experiment: str) -> UUID | None:
        matches = [
            t
            for t in self.trials.values()
            if t["visitor_id"] == visitor_id
            and t["experiment"] == experiment
            and t["state"] in ("created", "recovering", "receipt_ready")
        ]
        matches.sort(key=lambda t: t["created_at"], reverse=True)
        return matches[0]["id"] if matches else None

    async def create_trial(
        self,
        visitor_id: UUID,
        experiment: str,
        enrolled_cohort: str,
        title: str,
        now: datetime,
    ) -> tuple[UUID, str, str]:
        visitor_trials = [
            t
            for t in self.trials.values()
            if t["visitor_id"] == visitor_id and t["experiment"] == experiment
        ]
        for t in visitor_trials:
            if t["state"] == "created":
                t["title"] = title
                return t["id"], "created", title
            if t["state"] in ("recovering", "receipt_ready"):
                raise ConflictError(
                    "An active trial is already in progress.",
                    extra={"activeTrialId": str(t["id"])},
                )

        cohorts = {t["cohort"] for t in visitor_trials}
        if "practice" in cohorts and enrolled_cohort in cohorts:
            raise ConflictError("Trials for this visitor have already been completed.")
        if "practice" in cohorts and enrolled_cohort not in cohorts:
            raise ConflictError("Practice trial already completed for this visitor.")

        trial_id = uuid4()
        self.trials[trial_id] = {
            "id": trial_id,
            "visitor_id": visitor_id,
            "experiment": experiment,
            "cohort": "pending",
            "variant": None,
            "failure_mode": None,
            "state": "created",
            "title": title,
            "created_at": now,
            "exposed_at": None,
            "deadline_at": None,
            "checked_empty_at": None,
            "displayed_at": None,
            "closed_at": None,
            "final_entry_count": None,
        }
        return trial_id, "created", title

    def _new_receipt_code(self) -> str:
        existing = {e["receipt_code"] for e in self.entries}
        while True:
            code = generate_receipt_code()
            if code not in existing:
                return code

    async def submit_trial(
        self,
        trial_id: UUID,
        visitor_id: UUID,
        action_id: UUID,
        intent: str,
        settings: Settings,
        flagsmith: FlagsmithService,
        now: datetime,
    ) -> tuple[int, dict[str, Any]]:
        trial = self.trials.get(trial_id)
        if trial is None:
            raise NotFoundError("Trial not found.")
        if trial["visitor_id"] != visitor_id:
            raise ForbiddenError("You do not own this trial.")

        if (trial_id, action_id) in self.actions:
            status_code, resp = self.actions[(trial_id, action_id)]
            return status_code, dict(resp)

        if intent == "initial":
            if trial["state"] != "created":
                raise ConflictError("Initial submission was already recorded.")

            other_cohorts = {
                t["cohort"]
                for t in self.trials.values()
                if t["visitor_id"] == visitor_id
                and t["experiment"] == trial["experiment"]
                and t["id"] != trial_id
            }
            if settings.app_cohort in other_cohorts:
                eval_result = FlagEvaluation(
                    cohort="practice",
                    variant="retry",
                    flag_variant_key=None,
                    is_default=True,
                    in_experiment=False,
                )
            else:
                eval_result = flagsmith.evaluate_and_expose(visitor_id)

            cohort = eval_result.cohort
            variant = eval_result.variant
            if cohort == "practice" and "practice" in other_cohorts:
                raise ConflictError("Practice trial already exists for this visitor.")

            counter_key = (trial["experiment"], cohort, variant)
            idx = self.allocation_counters.get(counter_key, 0)
            self.allocation_counters[counter_key] = idx + 1

            failure_mode = paired_failure_mode(
                settings.balance_secret, trial["experiment"], idx
            )
            exposed_at = now
            deadline_at = now + timedelta(seconds=settings.recovery_window_seconds)

            if failure_mode == "reply_lost":
                self.entries.append(
                    {
                        "id": uuid4(),
                        "trial_id": trial_id,
                        "action_id": action_id,
                        "receipt_code": self._new_receipt_code(),
                        "saved_at": now,
                    }
                )

            trial["cohort"] = cohort
            trial["variant"] = variant
            trial["failure_mode"] = failure_mode
            trial["state"] = "recovering"
            trial["exposed_at"] = exposed_at
            trial["deadline_at"] = deadline_at

            resp = {
                "state": "recovering",
                "variant": variant,
                "exposedAt": exposed_at.isoformat(),
                "deadlineAt": deadline_at.isoformat(),
                "serverNow": now.isoformat(),
            }
            self.actions[(trial_id, action_id)] = (202, resp)
            self.event_ledger.setdefault(
                (trial_id, "$flag_exposure"),
                {
                    "trial_id": trial_id,
                    "event_name": "$flag_exposure",
                    "occurred_at": exposed_at,
                    "dispatch_state": "sent" if cohort == settings.app_cohort else "skipped_practice",
                    "attempted_at": exposed_at,
                },
            )
            return 202, dict(resp)

        # intent == "retry"
        if trial["state"] != "recovering":
            if trial["state"] == "created":
                raise ConflictError("Cannot retry before initial submission.")
            raise ConflictError(f"Cannot retry when trial state is '{trial['state']}'.")

        trial_entries = [e for e in self.entries if e["trial_id"] == trial_id]
        old_count = len(trial_entries)

        if trial["deadline_at"] is not None and now > trial["deadline_at"]:
            trial["state"] = "expired"
            trial["closed_at"] = now
            trial["final_entry_count"] = old_count
            raise ConflictError("Trial recovery window expired.", extra={"state": "expired"})

        code = self._new_receipt_code()
        self.entries.append(
            {
                "id": uuid4(),
                "trial_id": trial_id,
                "action_id": action_id,
                "receipt_code": code,
                "saved_at": now,
            }
        )
        new_count = old_count + 1
        trial["state"] = "receipt_ready"

        if old_count == 1 and new_count == 2:
            if (trial_id, "duplicate_entry_created") not in self.event_ledger:
                if trial["cohort"] == settings.app_cohort:
                    sent = flagsmith.track_conversion("duplicate_entry_created", visitor_id)
                    dispatch_state = "sent" if sent else "failed"
                else:
                    dispatch_state = "skipped_practice"
                self.event_ledger[(trial_id, "duplicate_entry_created")] = {
                    "trial_id": trial_id,
                    "event_name": "duplicate_entry_created",
                    "occurred_at": now,
                    "dispatch_state": dispatch_state,
                    "attempted_at": now,
                }

        resp = {"state": "receipt_ready", "receiptCode": code}
        self.actions[(trial_id, action_id)] = (200, resp)
        return 200, dict(resp)

    async def check_status(
        self,
        trial_id: UUID,
        visitor_id: UUID,
        action_id: UUID | None,
        now: datetime,
    ) -> tuple[int, dict[str, Any]]:
        trial = self.trials.get(trial_id)
        if trial is None:
            raise NotFoundError("Trial not found.")
        if trial["visitor_id"] != visitor_id:
            raise ForbiddenError("You do not own this trial.")

        if action_id is not None and (trial_id, action_id) in self.actions:
            status_code, resp = self.actions[(trial_id, action_id)]
            return status_code, dict(resp)

        if trial["variant"] != "check_status":
            raise ConflictError("check-status is only available for the check_status variant.")
        if trial["state"] == "created":
            raise ConflictError("Trial has not been submitted yet.")
        if trial["state"] in ("finished", "expired"):
            raise ConflictError(f"Trial is already '{trial['state']}'.")

        trial_entries = sorted(
            [e for e in self.entries if e["trial_id"] == trial_id],
            key=lambda e: e["saved_at"],
            reverse=True,
        )

        if trial["deadline_at"] is not None and now > trial["deadline_at"]:
            trial["state"] = "expired"
            trial["closed_at"] = now
            trial["final_entry_count"] = len(trial_entries)
            raise ConflictError("Trial recovery window expired.", extra={"state": "expired"})

        if trial_entries:
            if trial["state"] == "recovering":
                trial["state"] = "receipt_ready"
            resp: dict[str, Any] = {
                "found": True,
                "receiptCode": trial_entries[0]["receipt_code"],
            }
        else:
            if trial["checked_empty_at"] is None:
                trial["checked_empty_at"] = now
            resp = {"found": False, "nextAction": "retry"}

        if action_id is not None:
            self.actions[(trial_id, action_id)] = (200, resp)
        return 200, dict(resp)

    async def get_trial(
        self,
        trial_id: UUID,
        visitor_id: UUID,
        now: datetime,
    ) -> dict[str, Any]:
        trial = self.trials.get(trial_id)
        if trial is None:
            raise NotFoundError("Trial not found.")
        if trial["visitor_id"] != visitor_id:
            raise ForbiddenError("You do not own this trial.")

        trial_entries = sorted(
            [e for e in self.entries if e["trial_id"] == trial_id],
            key=lambda e: e["saved_at"],
            reverse=True,
        )
        entry_count = len(trial_entries)
        latest_code = trial_entries[0]["receipt_code"] if trial_entries else None

        if (
            trial["state"] in ("recovering", "receipt_ready")
            and trial["deadline_at"] is not None
            and now > trial["deadline_at"]
        ):
            trial["state"] = "expired"
            trial["closed_at"] = now
            trial["final_entry_count"] = entry_count

        state = trial["state"]
        is_terminal = state in ("finished", "expired")
        reveal_receipt = state in ("receipt_ready", "finished", "expired")

        return {
            "trialId": trial["id"],
            "state": state,
            "title": trial["title"],
            "cohort": trial["cohort"],
            "variant": trial["variant"],
            "allowedActions": compute_allowed_actions(
                state, trial["variant"], trial["checked_empty_at"]
            ),
            "receiptCode": latest_code if reveal_receipt else None,
            "exposedAt": trial["exposed_at"],
            "deadlineAt": trial["deadline_at"],
            "checkedEmptyAt": trial["checked_empty_at"],
            "displayedAt": trial["displayed_at"],
            "closedAt": trial["closed_at"],
            "serverNow": now,
            "failureMode": trial["failure_mode"] if is_terminal else None,
            "finalEntryCount": (
                (trial["final_entry_count"] if trial["final_entry_count"] is not None else entry_count)
                if is_terminal
                else None
            ),
        }

    async def get_receipt(
        self,
        receipt_code: str,
        visitor_id: UUID,
    ) -> dict[str, Any]:
        normalized = normalize_receipt_code(receipt_code)
        entry = next((e for e in self.entries if e["receipt_code"] == normalized), None)
        if entry is None:
            raise NotFoundError("Receipt not found.")
        trial = self.trials[entry["trial_id"]]
        if trial["visitor_id"] != visitor_id:
            raise ForbiddenError("You do not own this receipt.")
        return {
            "receiptCode": entry["receipt_code"],
            "title": trial["title"],
            "savedAt": entry["saved_at"],
        }

    async def mark_receipt_displayed(
        self,
        receipt_code: str,
        visitor_id: UUID,
        action_id: UUID,
        enrolled_cohort: str,
        flagsmith: FlagsmithService,
        now: datetime,
    ) -> tuple[int, dict[str, Any]]:
        normalized = normalize_receipt_code(receipt_code)
        entry = next((e for e in self.entries if e["receipt_code"] == normalized), None)
        if entry is None:
            raise NotFoundError("Receipt not found.")
        trial = self.trials[entry["trial_id"]]
        if trial["visitor_id"] != visitor_id:
            raise ForbiddenError("You do not own this receipt.")

        trial_id = trial["id"]
        if (trial_id, action_id) in self.actions:
            status_code, resp = self.actions[(trial_id, action_id)]
            return status_code, dict(resp)

        entry_count = sum(1 for e in self.entries if e["trial_id"] == trial_id)

        if trial["state"] in ("finished", "expired"):
            final_cnt = (
                trial["final_entry_count"]
                if trial["final_entry_count"] is not None
                else entry_count
            )
            resp = {
                "state": trial["state"],
                "receiptCode": normalized,
                "failureMode": trial["failure_mode"],
                "finalEntryCount": final_cnt,
                "primaryConverted": trial["state"] == "finished" and final_cnt == 1,
                "displayedAt": (
                    trial["displayed_at"].isoformat() if trial["displayed_at"] else None
                ),
                "closedAt": trial["closed_at"].isoformat() if trial["closed_at"] else None,
                "serverNow": now.isoformat(),
            }
            self.actions[(trial_id, action_id)] = (200, resp)
            return 200, dict(resp)

        within_deadline = trial["deadline_at"] is None or now <= trial["deadline_at"]
        if within_deadline:
            trial["state"] = "finished"
            trial["displayed_at"] = now
            trial["closed_at"] = now
            trial["final_entry_count"] = entry_count
            should_fire_primary = entry_count == 1
        else:
            trial["state"] = "expired"
            trial["closed_at"] = now
            trial["final_entry_count"] = entry_count
            should_fire_primary = False

        if should_fire_primary and (trial_id, "single_entry_receipt_60s") not in self.event_ledger:
            if trial["cohort"] == enrolled_cohort:
                sent = flagsmith.track_conversion("single_entry_receipt_60s", visitor_id)
                dispatch_state = "sent" if sent else "failed"
            else:
                dispatch_state = "skipped_practice"
            self.event_ledger[(trial_id, "single_entry_receipt_60s")] = {
                "trial_id": trial_id,
                "event_name": "single_entry_receipt_60s",
                "occurred_at": now,
                "dispatch_state": dispatch_state,
                "attempted_at": now,
            }

        resp = {
            "state": trial["state"],
            "receiptCode": normalized,
            "failureMode": trial["failure_mode"],
            "finalEntryCount": entry_count,
            "primaryConverted": should_fire_primary,
            "displayedAt": trial["displayed_at"].isoformat() if trial["displayed_at"] else None,
            "closedAt": trial["closed_at"].isoformat(),
            "serverNow": now.isoformat(),
        }
        self.actions[(trial_id, action_id)] = (200, resp)
        return 200, dict(resp)

    async def get_summary(
        self,
        experiment: str,
        enrolled_cohort: str,
        now: datetime,
    ) -> dict[str, Any]:
        exp_trials = [t for t in self.trials.values() if t["experiment"] == experiment]
        cohorts: dict[str, int] = {}
        for t in exp_trials:
            cohorts[t["cohort"]] = cohorts.get(t["cohort"], 0) + 1

        live_trials = [
            t
            for t in exp_trials
            if t["cohort"] == enrolled_cohort and t["exposed_at"] is not None
        ]

        def entry_cnt(tid: UUID) -> int:
            return sum(1 for e in self.entries if e["trial_id"] == tid)

        def is_abandoned(t: dict[str, Any]) -> bool:
            if t["state"] == "expired":
                return True
            if (
                t["state"] in ("recovering", "receipt_ready")
                and t["deadline_at"] is not None
                and now > t["deadline_at"]
            ):
                return True
            return False

        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for t in live_trials:
            groups.setdefault((t["variant"], t["failure_mode"]), []).append(t)

        by_arm_failure: list[dict[str, Any]] = []
        for (variant, failure_mode), items in sorted(groups.items()):
            tot = len(items)
            finished = sum(1 for t in items if t["state"] == "finished")
            abandoned = sum(1 for t in items if is_abandoned(t))
            active = sum(
                1
                for t in items
                if t["state"] in ("recovering", "receipt_ready") and not is_abandoned(t)
            )
            primary = sum(
                1
                for t in items
                if t["state"] == "finished"
                and (t["final_entry_count"] if t["final_entry_count"] is not None else entry_cnt(t["id"])) == 1
            )
            dup = sum(
                1
                for t in items
                if (t["final_entry_count"] if t["final_entry_count"] is not None else entry_cnt(t["id"])) >= 2
            )
            durations = sorted(
                (t["displayed_at"] - t["exposed_at"]).total_seconds()
                for t in items
                if t["state"] == "finished" and t["displayed_at"] is not None
            )
            p50 = _percentile(durations, 0.5)
            p90 = _percentile(durations, 0.9)
            by_arm_failure.append(
                {
                    "variant": variant,
                    "failureMode": failure_mode,
                    "totalExposed": tot,
                    "finishedCount": finished,
                    "expiredCount": abandoned,
                    "activeCount": active,
                    "primaryCount": primary,
                    "primaryRate": round(primary / tot, 4) if tot else 0.0,
                    "duplicateCount": dup,
                    "duplicateRate": round(dup / tot, 4) if tot else 0.0,
                    "abandonmentCount": abandoned,
                    "abandonmentRate": round(abandoned / tot, 4) if tot else 0.0,
                    "recoveryTimeSec": {
                        "p50": round(p50, 3) if p50 is not None else None,
                        "p90": round(p90, 3) if p90 is not None else None,
                    },
                }
            )

        tot_live = len(live_trials)
        prim_all = sum(g["primaryCount"] for g in by_arm_failure)
        dup_all = sum(g["duplicateCount"] for g in by_arm_failure)
        ab_all = sum(g["abandonmentCount"] for g in by_arm_failure)
        all_durations = sorted(
            (t["displayed_at"] - t["exposed_at"]).total_seconds()
            for t in live_trials
            if t["state"] == "finished" and t["displayed_at"] is not None
        )
        ov_p50 = _percentile(all_durations, 0.5)
        ov_p90 = _percentile(all_durations, 0.9)

        return {
            "experiment": experiment,
            "cohorts": cohorts,
            "liveTotalExposed": tot_live,
            "primaryRate": round(prim_all / tot_live, 4) if tot_live else 0.0,
            "duplicateRate": round(dup_all / tot_live, 4) if tot_live else 0.0,
            "abandonmentRate": round(ab_all / tot_live, 4) if tot_live else 0.0,
            "recoveryTimeSec": {
                "p50": round(ov_p50, 3) if ov_p50 is not None else None,
                "p90": round(ov_p90, 3) if ov_p90 is not None else None,
            },
            "byArmAndFailureMode": by_arm_failure,
            "serverNow": now,
        }
