from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.config import Settings
from app.flagsmith_service import FlagsmithService


class StoreError(Exception):
    def __init__(self, detail: str, status_code: int, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.extra = extra or {}


class NotFoundError(StoreError):
    def __init__(self, detail: str = "Resource not found.") -> None:
        super().__init__(detail, 404)


class ForbiddenError(StoreError):
    def __init__(self, detail: str = "You do not own this resource.") -> None:
        super().__init__(detail, 403)


class ConflictError(StoreError):
    def __init__(self, detail: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail, 409, extra=extra)


def compute_allowed_actions(
    state: str,
    variant: str | None,
    checked_empty_at: datetime | None,
) -> list[str]:
    if state == "created":
        return ["submit_initial"]
    if state == "recovering":
        if variant == "check_status":
            if checked_empty_at is None:
                return ["check_status"]
            return ["retry"]
        return ["retry"]
    if state == "receipt_ready":
        return ["view_receipt", "mark_displayed"]
    return []


class TrialStore(Protocol):
    async def health_check(self) -> bool: ...

    async def get_active_trial_id(self, visitor_id: UUID, experiment: str) -> UUID | None: ...

    async def create_trial(
        self,
        visitor_id: UUID,
        experiment: str,
        enrolled_cohort: str,
        title: str,
        now: datetime,
    ) -> tuple[UUID, str, str]: ...

    async def submit_trial(
        self,
        trial_id: UUID,
        visitor_id: UUID,
        action_id: UUID,
        intent: str,
        settings: Settings,
        flagsmith: FlagsmithService,
        now: datetime,
    ) -> tuple[int, dict[str, Any]]: ...

    async def check_status(
        self,
        trial_id: UUID,
        visitor_id: UUID,
        action_id: UUID | None,
        now: datetime,
    ) -> tuple[int, dict[str, Any]]: ...

    async def get_trial(
        self,
        trial_id: UUID,
        visitor_id: UUID,
        now: datetime,
    ) -> dict[str, Any]: ...

    async def get_receipt(
        self,
        receipt_code: str,
        visitor_id: UUID,
    ) -> dict[str, Any]: ...

    async def mark_receipt_displayed(
        self,
        receipt_code: str,
        visitor_id: UUID,
        action_id: UUID,
        enrolled_cohort: str,
        flagsmith: FlagsmithService,
        now: datetime,
    ) -> tuple[int, dict[str, Any]]: ...

    async def get_summary(
        self,
        experiment: str,
        enrolled_cohort: str,
        now: datetime,
    ) -> dict[str, Any]: ...
