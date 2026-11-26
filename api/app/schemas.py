from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

DEFAULT_TITLE = "Spidey Sense demo entry"


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    database: Literal["ok", "unavailable"]
    flagsmithConfigured: bool
    serverNow: datetime


class SessionResponse(BaseModel):
    visitorSuffix: str
    activeTrialId: UUID | None
    serverNow: datetime


class CreateTrialRequest(BaseModel):
    title: str = DEFAULT_TITLE

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> str:
        if value is None:
            return DEFAULT_TITLE
        if not isinstance(value, str):
            raise ValueError("title must be a string")
        stripped = value.strip()
        if not stripped:
            return DEFAULT_TITLE
        if len(stripped) > 80:
            raise ValueError("title must be at most 80 characters")
        return stripped


class CreateTrialResponse(BaseModel):
    trialId: UUID
    state: str
    title: str


class SubmitTrialRequest(BaseModel):
    actionId: UUID
    intent: Literal["initial", "retry"]


class CheckStatusRequest(BaseModel):
    actionId: UUID | None = None


class DisplayedRequest(BaseModel):
    actionId: UUID


class TrialDetailResponse(BaseModel):
    trialId: UUID
    state: str
    title: str
    cohort: str
    variant: str | None = None
    allowedActions: list[str]
    receiptCode: str | None = None
    exposedAt: datetime | None = None
    deadlineAt: datetime | None = None
    checkedEmptyAt: datetime | None = None
    displayedAt: datetime | None = None
    closedAt: datetime | None = None
    serverNow: datetime
    failureMode: str | None = None
    finalEntryCount: int | None = None


class ReceiptResponse(BaseModel):
    receiptCode: str
    title: str
    savedAt: datetime


class DisplayedResponse(BaseModel):
    state: str
    receiptCode: str
    failureMode: str
    finalEntryCount: int
    primaryConverted: bool
    displayedAt: datetime | None = None
    closedAt: datetime | None = None
    serverNow: datetime


class RecoveryTimeStats(BaseModel):
    p50: float | None = None
    p90: float | None = None


class ArmFailureStats(BaseModel):
    variant: str
    failureMode: str
    totalExposed: int
    finishedCount: int
    expiredCount: int
    activeCount: int
    primaryCount: int
    primaryRate: float
    duplicateCount: int
    duplicateRate: float
    abandonmentCount: int
    abandonmentRate: float
    recoveryTimeSec: RecoveryTimeStats


class SummaryResponse(BaseModel):
    experiment: str
    cohorts: dict[str, int]
    liveTotalExposed: int
    primaryRate: float
    duplicateRate: float
    abandonmentRate: float
    recoveryTimeSec: RecoveryTimeStats
    byArmAndFailureMode: list[ArmFailureStats] = Field(default_factory=list)
    serverNow: datetime
