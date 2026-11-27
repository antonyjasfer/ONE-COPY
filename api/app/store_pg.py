from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

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

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema.sql"


async def create_pool(database_url: str) -> AsyncConnectionPool[Any]:
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=10,
        open=False,
        kwargs={"row_factory": dict_row, "autocommit": False},
    )
    await pool.open()
    return pool


async def run_schema_migrations(pool: AsyncConnectionPool[Any]) -> None:
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(schema_sql)


class PostgresTrialStore:
    def __init__(self, pool: AsyncConnectionPool[Any]) -> None:
        self._pool = pool

    async def health_check(self) -> bool:
        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute("SELECT 1 AS ok")
                row = await cur.fetchone()
                return bool(row and row["ok"] == 1)
        except Exception:
            return False

    async def get_active_trial_id(
        self,
        visitor_id: UUID,
        experiment: str,
        enrolled_cohort: str,
    ) -> UUID | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id
                FROM trials
                WHERE visitor_id = %s
                  AND experiment = %s
                ORDER BY
                    CASE WHEN state IN ('created', 'recovering', 'receipt_ready') THEN 0 ELSE 1 END,
                    CASE WHEN cohort = %s THEN 0 ELSE 1 END,
                    created_at DESC
                LIMIT 1
                """,
                (visitor_id, experiment, enrolled_cohort),
            )
            row = await cur.fetchone()
            return row["id"] if row else None

    async def create_trial(
        self,
        visitor_id: UUID,
        experiment: str,
        enrolled_cohort: str,
        title: str,
        now: datetime,
    ) -> tuple[UUID, str, str]:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """
                    SELECT id, cohort, state, title
                    FROM trials
                    WHERE visitor_id = %s AND experiment = %s
                    FOR UPDATE
                    """,
                    (visitor_id, experiment),
                )
                rows = await cur.fetchall()

                for row in rows:
                    if row["state"] == "created":
                        await conn.execute(
                            "UPDATE trials SET title = %s WHERE id = %s",
                            (title, row["id"]),
                        )
                        return row["id"], "created", title
                    if row["state"] in ("recovering", "receipt_ready"):
                        raise ConflictError(
                            "An active trial is already in progress.",
                            extra={"activeTrialId": str(row["id"])},
                        )

                existing_cohorts = {row["cohort"] for row in rows}
                if "practice" in existing_cohorts and enrolled_cohort in existing_cohorts:
                    raise ConflictError("Trials for this visitor have already been completed.")
                if "practice" in existing_cohorts and enrolled_cohort not in existing_cohorts:
                    raise ConflictError("Practice trial already completed for this visitor.")

                trial_id = uuid4()
                await conn.execute(
                    """
                    INSERT INTO trials (
                        id, visitor_id, experiment, cohort, state, title, created_at
                    )
                    VALUES (%s, %s, %s, 'pending', 'created', %s, %s)
                    """,
                    (trial_id, visitor_id, experiment, title, now),
                )
                return trial_id, "created", title

    async def _insert_unique_entry(
        self,
        conn: Any,
        trial_id: UUID,
        action_id: UUID,
        saved_at: datetime,
    ) -> str:
        for _ in range(10):
            code = generate_receipt_code()
            try:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO entries (id, trial_id, action_id, receipt_code, saved_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (uuid4(), trial_id, action_id, code, saved_at),
                    )
                return code
            except pg_errors.UniqueViolation:
                continue
        raise RuntimeError("Failed to allocate unique Crockford Base32 receipt code.")

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
        eval_result: FlagEvaluation | None = None
        if intent == "initial":
            async with self._pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, visitor_id, state, experiment FROM trials WHERE id = %s",
                    (trial_id,),
                )
                pre_trial = await cur.fetchone()
                if pre_trial is None:
                    raise NotFoundError("Trial not found.")
                if pre_trial["visitor_id"] != visitor_id:
                    raise ForbiddenError("You do not own this trial.")

                cur = await conn.execute(
                    "SELECT http_status, response FROM actions WHERE trial_id = %s AND action_id = %s",
                    (trial_id, action_id),
                )
                cached_action = await cur.fetchone()
                if cached_action is not None:
                    return int(cached_action["http_status"]), dict(cached_action["response"])

                if pre_trial["state"] == "created":
                    cur = await conn.execute(
                        """
                        SELECT 1 FROM trials
                        WHERE visitor_id = %s AND experiment = %s AND cohort = %s
                        LIMIT 1
                        """,
                        (visitor_id, pre_trial["experiment"], settings.app_cohort),
                    )
                    has_enrolled = await cur.fetchone() is not None
                    if has_enrolled:
                        eval_result = FlagEvaluation(
                            cohort="practice",
                            variant="retry",
                            flag_variant_key=None,
                            is_default=True,
                            in_experiment=False,
                        )
                    else:
                        eval_result = flagsmith.evaluate_and_expose(visitor_id)

        expired_commit_needed = False
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT * FROM trials WHERE id = %s FOR UPDATE",
                    (trial_id,),
                )
                trial = await cur.fetchone()
                if trial is None:
                    raise NotFoundError("Trial not found.")
                if trial["visitor_id"] != visitor_id:
                    raise ForbiddenError("You do not own this trial.")

                cur = await conn.execute(
                    "SELECT http_status, response FROM actions WHERE trial_id = %s AND action_id = %s",
                    (trial_id, action_id),
                )
                cached_action = await cur.fetchone()
                if cached_action is not None:
                    return int(cached_action["http_status"]), dict(cached_action["response"])

                if intent == "initial":
                    if trial["state"] != "created":
                        raise ConflictError("Initial submission was already recorded.")

                    assert eval_result is not None
                    cur = await conn.execute(
                        """
                        SELECT cohort FROM trials
                        WHERE visitor_id = %s AND experiment = %s AND id <> %s
                        """,
                        (visitor_id, trial["experiment"], trial_id),
                    )
                    other_cohorts = {r["cohort"] for r in await cur.fetchall()}
                    cohort = eval_result.cohort
                    variant = eval_result.variant
                    if cohort == settings.app_cohort and settings.app_cohort in other_cohorts:
                        cohort = "practice"
                        variant = "retry"
                    if cohort == "practice" and "practice" in other_cohorts:
                        raise ConflictError("Practice trial already exists for this visitor.")

                    await conn.execute(
                        """
                        INSERT INTO allocation_counters (experiment, cohort, variant, next_index)
                        VALUES (%s, %s, %s, 0)
                        ON CONFLICT (experiment, cohort, variant) DO NOTHING
                        """,
                        (trial["experiment"], cohort, variant),
                    )
                    cur = await conn.execute(
                        """
                        SELECT next_index
                        FROM allocation_counters
                        WHERE experiment = %s AND cohort = %s AND variant = %s
                        FOR UPDATE
                        """,
                        (trial["experiment"], cohort, variant),
                    )
                    counter_row = await cur.fetchone()
                    idx = int(counter_row["next_index"])
                    await conn.execute(
                        """
                        UPDATE allocation_counters
                        SET next_index = %s
                        WHERE experiment = %s AND cohort = %s AND variant = %s
                        """,
                        (idx + 1, trial["experiment"], cohort, variant),
                    )

                    failure_mode = paired_failure_mode(
                        settings.balance_secret,
                        trial["experiment"],
                        idx,
                    )
                    exposed_at = now
                    deadline_at = now + timedelta(seconds=settings.recovery_window_seconds)

                    if failure_mode == "reply_lost":
                        await self._insert_unique_entry(conn, trial_id, action_id, now)

                    await conn.execute(
                        """
                        UPDATE trials
                        SET cohort = %s,
                            variant = %s,
                            failure_mode = %s,
                            state = 'recovering',
                            exposed_at = %s,
                            deadline_at = %s
                        WHERE id = %s
                        """,
                        (cohort, variant, failure_mode, exposed_at, deadline_at, trial_id),
                    )

                    resp: dict[str, Any] = {
                        "state": "recovering",
                        "variant": variant,
                        "exposedAt": exposed_at.isoformat(),
                        "deadlineAt": deadline_at.isoformat(),
                        "serverNow": now.isoformat(),
                    }
                    await conn.execute(
                        """
                        INSERT INTO actions (trial_id, action_id, intent, http_status, response, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (trial_id, action_id, "initial", 202, Jsonb(resp), now),
                    )
                    await conn.execute(
                        """
                        INSERT INTO event_ledger (trial_id, event_name, occurred_at, dispatch_state, attempted_at)
                        VALUES (%s, '$flag_exposure', %s, %s, %s)
                        ON CONFLICT (trial_id, event_name) DO NOTHING
                        """,
                        (
                            trial_id,
                            exposed_at,
                            "sent" if cohort == settings.app_cohort else "skipped_practice",
                            exposed_at,
                        ),
                    )
                    return 202, resp

                # intent == "retry"
                if trial["state"] != "recovering":
                    if trial["state"] == "created":
                        raise ConflictError("Cannot retry before initial submission.")
                    raise ConflictError(f"Cannot retry when trial state is '{trial['state']}'.")

                cur = await conn.execute(
                    "SELECT COUNT(*) AS cnt FROM entries WHERE trial_id = %s",
                    (trial_id,),
                )
                old_count = int((await cur.fetchone())["cnt"])

                if trial["deadline_at"] is not None and now > trial["deadline_at"]:
                    await conn.execute(
                        """
                        UPDATE trials
                        SET state = 'expired',
                            closed_at = %s,
                            final_entry_count = %s
                        WHERE id = %s
                        """,
                        (now, old_count, trial_id),
                    )
                    expired_commit_needed = True
                else:
                    receipt_code = await self._insert_unique_entry(conn, trial_id, action_id, now)
                    new_count = old_count + 1
                    await conn.execute(
                        "UPDATE trials SET state = 'receipt_ready' WHERE id = %s",
                        (trial_id,),
                    )

                    if old_count == 1 and new_count == 2:
                        cur = await conn.execute(
                            """
                            INSERT INTO event_ledger (trial_id, event_name, occurred_at, dispatch_state)
                            VALUES (%s, 'duplicate_entry_created', %s, 'pending')
                            ON CONFLICT (trial_id, event_name) DO NOTHING
                            RETURNING trial_id
                            """,
                            (trial_id, now),
                        )
                        if await cur.fetchone() is not None:
                            if trial["cohort"] == settings.app_cohort:
                                sent = flagsmith.track_conversion(
                                    "duplicate_entry_created", visitor_id
                                )
                                dispatch_state = "sent" if sent else "failed"
                            else:
                                dispatch_state = "skipped_practice"
                            await conn.execute(
                                """
                                UPDATE event_ledger
                                SET dispatch_state = %s, attempted_at = %s
                                WHERE trial_id = %s AND event_name = 'duplicate_entry_created'
                                """,
                                (dispatch_state, now, trial_id),
                            )

                    resp = {
                        "state": "receipt_ready",
                        "receiptCode": receipt_code,
                    }
                    await conn.execute(
                        """
                        INSERT INTO actions (trial_id, action_id, intent, http_status, response, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (trial_id, action_id, "retry", 200, Jsonb(resp), now),
                    )
                    return 200, resp

        if expired_commit_needed:
            raise ConflictError("Trial recovery window expired.", extra={"state": "expired"})
        raise RuntimeError("Unreachable state in submit_trial.")

    async def check_status(
        self,
        trial_id: UUID,
        visitor_id: UUID,
        action_id: UUID | None,
        now: datetime,
    ) -> tuple[int, dict[str, Any]]:
        expired_commit_needed = False
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT * FROM trials WHERE id = %s FOR UPDATE",
                    (trial_id,),
                )
                trial = await cur.fetchone()
                if trial is None:
                    raise NotFoundError("Trial not found.")
                if trial["visitor_id"] != visitor_id:
                    raise ForbiddenError("You do not own this trial.")

                if action_id is not None:
                    cur = await conn.execute(
                        "SELECT http_status, response FROM actions WHERE trial_id = %s AND action_id = %s",
                        (trial_id, action_id),
                    )
                    cached_action = await cur.fetchone()
                    if cached_action is not None:
                        return int(cached_action["http_status"]), dict(cached_action["response"])

                if trial["variant"] != "check_status":
                    raise ConflictError("check-status is only available for the check_status variant.")
                if trial["state"] == "created":
                    raise ConflictError("Trial has not been submitted yet.")
                if trial["state"] in ("finished", "expired"):
                    raise ConflictError(f"Trial is already '{trial['state']}'.")

                cur = await conn.execute(
                    "SELECT receipt_code FROM entries WHERE trial_id = %s ORDER BY saved_at DESC LIMIT 1",
                    (trial_id,),
                )
                entry = await cur.fetchone()

                if trial["deadline_at"] is not None and now > trial["deadline_at"]:
                    cur_cnt = await conn.execute(
                        "SELECT COUNT(*) AS cnt FROM entries WHERE trial_id = %s",
                        (trial_id,),
                    )
                    cnt = int((await cur_cnt.fetchone())["cnt"])
                    await conn.execute(
                        """
                        UPDATE trials
                        SET state = 'expired',
                            closed_at = %s,
                            final_entry_count = %s
                        WHERE id = %s
                        """,
                        (now, cnt, trial_id),
                    )
                    expired_commit_needed = True
                else:
                    if entry is not None:
                        if trial["state"] == "recovering":
                            await conn.execute(
                                "UPDATE trials SET state = 'receipt_ready' WHERE id = %s",
                                (trial_id,),
                            )
                        resp: dict[str, Any] = {
                            "found": True,
                            "receiptCode": entry["receipt_code"],
                        }
                    else:
                        if trial["checked_empty_at"] is None:
                            await conn.execute(
                                "UPDATE trials SET checked_empty_at = %s WHERE id = %s",
                                (now, trial_id),
                            )
                        resp = {
                            "found": False,
                            "nextAction": "retry",
                        }

                    if action_id is not None:
                        await conn.execute(
                            """
                            INSERT INTO actions (trial_id, action_id, intent, http_status, response, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (trial_id, action_id, "check_status", 200, Jsonb(resp), now),
                        )
                    return 200, resp

        if expired_commit_needed:
            raise ConflictError("Trial recovery window expired.", extra={"state": "expired"})
        raise RuntimeError("Unreachable state in check_status.")

    async def get_trial(
        self,
        trial_id: UUID,
        visitor_id: UUID,
        now: datetime,
    ) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT * FROM trials WHERE id = %s FOR UPDATE",
                    (trial_id,),
                )
                trial = await cur.fetchone()
                if trial is None:
                    raise NotFoundError("Trial not found.")
                if trial["visitor_id"] != visitor_id:
                    raise ForbiddenError("You do not own this trial.")

                cur = await conn.execute(
                    "SELECT COUNT(*) AS cnt FROM entries WHERE trial_id = %s",
                    (trial_id,),
                )
                entry_agg = await cur.fetchone()
                entry_count = int(entry_agg["cnt"])

                # Also fetch deterministic latest receipt_code
                cur_latest = await conn.execute(
                    "SELECT receipt_code FROM entries WHERE trial_id = %s ORDER BY saved_at DESC LIMIT 1",
                    (trial_id,),
                )
                latest_row = await cur_latest.fetchone()
                latest_code = latest_row["receipt_code"] if latest_row else None

                state = trial["state"]
                closed_at = trial["closed_at"]
                final_entry_count = trial["final_entry_count"]

                if state in ("recovering", "receipt_ready") and trial["deadline_at"] is not None and now > trial["deadline_at"]:
                    state = "expired"
                    closed_at = now
                    final_entry_count = entry_count
                    await conn.execute(
                        """
                        UPDATE trials
                        SET state = 'expired',
                            closed_at = %s,
                            final_entry_count = %s
                        WHERE id = %s
                        """,
                        (closed_at, final_entry_count, trial_id),
                    )

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
                    "closedAt": closed_at,
                    "serverNow": now,
                    "failureMode": trial["failure_mode"] if is_terminal else None,
                    "finalEntryCount": (
                        (final_entry_count if final_entry_count is not None else entry_count)
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
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT e.receipt_code, e.saved_at, t.title, t.visitor_id
                FROM entries e
                JOIN trials t ON t.id = e.trial_id
                WHERE e.receipt_code = %s
                """,
                (normalized,),
            )
            row = await cur.fetchone()
            if row is None:
                raise NotFoundError("Receipt not found.")
            if row["visitor_id"] != visitor_id:
                raise ForbiddenError("You do not own this receipt.")
            return {
                "receiptCode": row["receipt_code"],
                "title": row["title"],
                "savedAt": row["saved_at"],
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
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """
                    SELECT e.trial_id, e.receipt_code, t.visitor_id
                    FROM entries e
                    JOIN trials t ON t.id = e.trial_id
                    WHERE e.receipt_code = %s
                    """,
                    (normalized,),
                )
                entry_row = await cur.fetchone()
                if entry_row is None:
                    raise NotFoundError("Receipt not found.")
                if entry_row["visitor_id"] != visitor_id:
                    raise ForbiddenError("You do not own this receipt.")

                trial_id = entry_row["trial_id"]
                cur = await conn.execute(
                    "SELECT * FROM trials WHERE id = %s FOR UPDATE",
                    (trial_id,),
                )
                trial = await cur.fetchone()

                cur = await conn.execute(
                    "SELECT http_status, response FROM actions WHERE trial_id = %s AND action_id = %s",
                    (trial_id, action_id),
                )
                cached_action = await cur.fetchone()
                if cached_action is not None:
                    return int(cached_action["http_status"]), dict(cached_action["response"])

                cur = await conn.execute(
                    "SELECT COUNT(*) AS cnt FROM entries WHERE trial_id = %s",
                    (trial_id,),
                )
                entry_count = int((await cur.fetchone())["cnt"])

                if trial["state"] in ("finished", "expired"):
                    final_cnt = (
                        int(trial["final_entry_count"])
                        if trial["final_entry_count"] is not None
                        else entry_count
                    )
                    resp: dict[str, Any] = {
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
                    await conn.execute(
                        """
                        INSERT INTO actions (trial_id, action_id, intent, http_status, response, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (trial_id, action_id, "displayed", 200, Jsonb(resp), now),
                    )
                    return 200, resp

                within_deadline = (
                    trial["deadline_at"] is None or now <= trial["deadline_at"]
                )
                if within_deadline:
                    new_state = "finished"
                    displayed_at: datetime | None = now
                    closed_at = now
                    await conn.execute(
                        """
                        UPDATE trials
                        SET state = 'finished',
                            displayed_at = %s,
                            closed_at = %s,
                            final_entry_count = %s
                        WHERE id = %s
                        """,
                        (displayed_at, closed_at, entry_count, trial_id),
                    )
                    should_fire_primary = entry_count == 1
                else:
                    new_state = "expired"
                    displayed_at = None
                    closed_at = now
                    await conn.execute(
                        """
                        UPDATE trials
                        SET state = 'expired',
                            closed_at = %s,
                            final_entry_count = %s
                        WHERE id = %s
                        """,
                        (closed_at, entry_count, trial_id),
                    )
                    should_fire_primary = False

                if should_fire_primary:
                    cur = await conn.execute(
                        """
                        INSERT INTO event_ledger (trial_id, event_name, occurred_at, dispatch_state)
                        VALUES (%s, 'single_entry_receipt_60s', %s, 'pending')
                        ON CONFLICT (trial_id, event_name) DO NOTHING
                        RETURNING trial_id
                        """,
                        (trial_id, now),
                    )
                    if await cur.fetchone() is not None:
                        if trial["cohort"] == enrolled_cohort:
                            sent = flagsmith.track_conversion(
                                "single_entry_receipt_60s", visitor_id
                            )
                            dispatch_state = "sent" if sent else "failed"
                        else:
                            dispatch_state = "skipped_practice"
                        await conn.execute(
                            """
                            UPDATE event_ledger
                            SET dispatch_state = %s, attempted_at = %s
                            WHERE trial_id = %s AND event_name = 'single_entry_receipt_60s'
                            """,
                            (dispatch_state, now, trial_id),
                        )

                resp = {
                    "state": new_state,
                    "receiptCode": normalized,
                    "failureMode": trial["failure_mode"],
                    "finalEntryCount": entry_count,
                    "primaryConverted": should_fire_primary,
                    "displayedAt": displayed_at.isoformat() if displayed_at else None,
                    "closedAt": closed_at.isoformat(),
                    "serverNow": now.isoformat(),
                }
                await conn.execute(
                    """
                    INSERT INTO actions (trial_id, action_id, intent, http_status, response, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (trial_id, action_id, "displayed", 200, Jsonb(resp), now),
                )
                return 200, resp

    async def get_summary(
        self,
        experiment: str,
        enrolled_cohort: str,
        now: datetime,
    ) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT cohort, COUNT(*) AS cnt
                FROM trials
                WHERE experiment = %s
                GROUP BY cohort
                """,
                (experiment,),
            )
            cohorts = {row["cohort"]: int(row["cnt"]) for row in await cur.fetchall()}

            cur = await conn.execute(
                """
                WITH live_trials AS (
                    SELECT
                        t.id,
                        t.variant,
                        t.failure_mode,
                        t.state,
                        t.exposed_at,
                        t.deadline_at,
                        t.displayed_at,
                        t.final_entry_count,
                        COALESCE(e.cnt, 0) AS entry_count
                    FROM trials t
                    LEFT JOIN (
                        SELECT trial_id, COUNT(*) AS cnt
                        FROM entries
                        GROUP BY trial_id
                    ) e ON e.trial_id = t.id
                    WHERE t.experiment = %s
                      AND t.cohort = %s
                      AND t.exposed_at IS NOT NULL
                )
                SELECT
                    variant,
                    failure_mode,
                    COUNT(*) AS total_exposed,
                    COUNT(*) FILTER (WHERE state = 'finished') AS finished_count,
                    COUNT(*) FILTER (
                        WHERE state = 'expired'
                           OR (state IN ('recovering', 'receipt_ready') AND deadline_at IS NOT NULL AND %s > deadline_at)
                    ) AS abandonment_count,
                    COUNT(*) FILTER (
                        WHERE state IN ('recovering', 'receipt_ready')
                          AND (deadline_at IS NULL OR %s <= deadline_at)
                    ) AS active_count,
                    COUNT(*) FILTER (
                        WHERE state = 'finished' AND COALESCE(final_entry_count, entry_count) = 1
                    ) AS primary_count,
                    COUNT(*) FILTER (
                        WHERE COALESCE(final_entry_count, entry_count) >= 2
                    ) AS duplicate_count,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (displayed_at - exposed_at))
                    ) FILTER (WHERE state = 'finished' AND displayed_at IS NOT NULL) AS p50_sec,
                    PERCENTILE_CONT(0.9) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (displayed_at - exposed_at))
                    ) FILTER (WHERE state = 'finished' AND displayed_at IS NOT NULL) AS p90_sec
                FROM live_trials
                GROUP BY variant, failure_mode
                ORDER BY variant, failure_mode
                """,
                (experiment, enrolled_cohort, now, now),
            )
            group_rows = await cur.fetchall()

            cur_overall = await conn.execute(
                """
                WITH live_trials AS (
                    SELECT
                        t.id,
                        t.state,
                        t.exposed_at,
                        t.deadline_at,
                        t.displayed_at,
                        t.final_entry_count,
                        COALESCE(e.cnt, 0) AS entry_count
                    FROM trials t
                    LEFT JOIN (
                        SELECT trial_id, COUNT(*) AS cnt
                        FROM entries
                        GROUP BY trial_id
                    ) e ON e.trial_id = t.id
                    WHERE t.experiment = %s
                      AND t.cohort = %s
                      AND t.exposed_at IS NOT NULL
                )
                SELECT
                    COUNT(*) AS total_exposed,
                    COUNT(*) FILTER (
                        WHERE state = 'expired'
                           OR (state IN ('recovering', 'receipt_ready') AND deadline_at IS NOT NULL AND %s > deadline_at)
                    ) AS abandonment_count,
                    COUNT(*) FILTER (
                        WHERE state = 'finished' AND COALESCE(final_entry_count, entry_count) = 1
                    ) AS primary_count,
                    COUNT(*) FILTER (
                        WHERE COALESCE(final_entry_count, entry_count) >= 2
                    ) AS duplicate_count,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (displayed_at - exposed_at))
                    ) FILTER (WHERE state = 'finished' AND displayed_at IS NOT NULL) AS p50_sec,
                    PERCENTILE_CONT(0.9) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (displayed_at - exposed_at))
                    ) FILTER (WHERE state = 'finished' AND displayed_at IS NOT NULL) AS p90_sec
                FROM live_trials
                """,
                (experiment, enrolled_cohort, now),
            )
            ov = await cur_overall.fetchone()

        total_exposed = int(ov["total_exposed"]) if ov else 0
        primary_total = int(ov["primary_count"]) if ov else 0
        dup_total = int(ov["duplicate_count"]) if ov else 0
        ab_total = int(ov["abandonment_count"]) if ov else 0

        by_arm_failure: list[dict[str, Any]] = []
        for row in group_rows:
            tot = int(row["total_exposed"])
            prim = int(row["primary_count"])
            dup = int(row["duplicate_count"])
            ab = int(row["abandonment_count"])
            by_arm_failure.append(
                {
                    "variant": row["variant"],
                    "failureMode": row["failure_mode"],
                    "totalExposed": tot,
                    "finishedCount": int(row["finished_count"]),
                    "expiredCount": ab,
                    "activeCount": int(row["active_count"]),
                    "primaryCount": prim,
                    "primaryRate": round(prim / tot, 4) if tot else 0.0,
                    "duplicateCount": dup,
                    "duplicateRate": round(dup / tot, 4) if tot else 0.0,
                    "abandonmentCount": ab,
                    "abandonmentRate": round(ab / tot, 4) if tot else 0.0,
                    "recoveryTimeSec": {
                        "p50": round(float(row["p50_sec"]), 3) if row["p50_sec"] is not None else None,
                        "p90": round(float(row["p90_sec"]), 3) if row["p90_sec"] is not None else None,
                    },
                }
            )

        return {
            "experiment": experiment,
            "cohorts": cohorts,
            "liveTotalExposed": total_exposed,
            "primaryRate": round(primary_total / total_exposed, 4) if total_exposed else 0.0,
            "duplicateRate": round(dup_total / total_exposed, 4) if total_exposed else 0.0,
            "abandonmentRate": round(ab_total / total_exposed, 4) if total_exposed else 0.0,
            "recoveryTimeSec": {
                "p50": round(float(ov["p50_sec"]), 3) if ov and ov["p50_sec"] is not None else None,
                "p90": round(float(ov["p90_sec"]), 3) if ov and ov["p90_sec"] is not None else None,
            },
            "byArmAndFailureMode": by_arm_failure,
            "serverNow": now,
        }
