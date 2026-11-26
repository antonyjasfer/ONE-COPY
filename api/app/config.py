from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from repo root or api/ if present
_ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT_DIR / ".env", override=False)
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

_DEV_COOKIE_SECRET = "dev-only-insecure-cookie-secret-change-me"
_DEV_BALANCE_SECRET = "dev-only-insecure-balance-secret-change-me"


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def _parse_origins(raw: str) -> frozenset[str]:
    return frozenset(
        origin.strip().rstrip("/").lower()
        for origin in raw.split(",")
        if origin.strip()
    )


@dataclass(frozen=True)
class Settings:
    database_url: str
    flagsmith_environment_key: str
    flagsmith_api_url: str | None
    flagsmith_events_api_url: str | None
    experiment_name: str
    recovery_window_seconds: int
    cookie_secret: str
    balance_secret: str
    app_origin: str
    environment: str
    app_cohort: str = "qa"
    cookie_name: str = "oc_visitor"
    cookie_ttl_seconds: int = 30 * 24 * 60 * 60
    allowed_origins: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_origins", _parse_origins(self.app_origin))
        if not self.app_cohort or self.app_cohort in ("practice", "pending"):
            raise RuntimeError("APP_COHORT must be a valid enrolled cohort name (e.g. 'qa' or 'live').")
        if self.is_production:
            if not self.cookie_secret or self.cookie_secret == _DEV_COOKIE_SECRET:
                raise RuntimeError("COOKIE_SECRET must be set to a strong secret in production.")
            if not self.balance_secret or self.balance_secret == _DEV_BALANCE_SECRET:
                raise RuntimeError("BALANCE_SECRET must be set to a strong secret in production.")
            if not self.database_url:
                raise RuntimeError("DATABASE_URL must be set in production.")

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"


def load_settings() -> Settings:
    environment = os.getenv("ENVIRONMENT", "development")
    default_cohort = "live" if environment.strip().lower() == "production" else "qa"
    app_cohort = os.getenv("APP_COHORT", default_cohort).strip() or default_cohort
    return Settings(
        database_url=_normalize_database_url(
            os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/one_copy")
        ),
        flagsmith_environment_key=os.getenv("FLAGSMITH_ENVIRONMENT_KEY", "").strip(),
        flagsmith_api_url=os.getenv("FLAGSMITH_API_URL", "").strip() or None,
        flagsmith_events_api_url=os.getenv("FLAGSMITH_EVENTS_API_URL", "").strip() or None,
        experiment_name=os.getenv("EXPERIMENT_NAME", "submission_recovery").strip() or "submission_recovery",
        recovery_window_seconds=int(os.getenv("RECOVERY_WINDOW_SECONDS", "60")),
        cookie_secret=os.getenv("COOKIE_SECRET", _DEV_COOKIE_SECRET),
        balance_secret=os.getenv("BALANCE_SECRET", _DEV_BALANCE_SECRET),
        app_origin=os.getenv("APP_ORIGIN", "http://localhost:3000"),
        environment=environment,
        app_cohort=app_cohort,
    )
