"""QA-only smoke script that sends 30 simulated identities to Flagsmith Development.

Refuses to run when ENVIRONMENT=production or APP_COHORT=live.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "api"))

load_dotenv(REPO_ROOT / ".env", override=True)

from app.config import load_settings  # noqa: E402
from app.flagsmith_service import FlagsmithService  # noqa: E402


def main() -> None:
    settings = load_settings()
    if settings.is_production or settings.app_cohort == "live":
        raise RuntimeError(
            "QA-only script: refuses to run with ENVIRONMENT=production or APP_COHORT=live."
        )
    if not settings.flagsmith_environment_key:
        raise RuntimeError("FLAGSMITH_ENVIRONMENT_KEY is missing in .env")
    if not settings.flagsmith_environment_key.startswith("ser."):
        raise RuntimeError(
            "FLAGSMITH_ENVIRONMENT_KEY must start with 'ser.' for local evaluation."
        )

    fs = FlagsmithService(settings)
    counts: dict[str, dict[str, int]] = {
        "control": {"exposed": 0, "single_entry_receipt_60s": 0, "duplicate_entry_created": 0},
        "check-status-first": {
            "exposed": 0,
            "single_entry_receipt_60s": 0,
            "duplicate_entry_created": 0,
        },
        "other": {"exposed": 0},
    }

    try:
        for idx in range(30):
            visitor_id = uuid4()
            ev = fs.evaluate_and_expose(visitor_id)
            if ev.cohort != settings.app_cohort:
                counts["other"]["exposed"] += 1
                continue

            arm_key = ev.flag_variant_key or (
                "check-status-first" if ev.variant == "check_status" else "control"
            )
            bucket = counts.setdefault(
                arm_key,
                {"exposed": 0, "single_entry_receipt_60s": 0, "duplicate_entry_created": 0},
            )
            bucket["exposed"] += 1

            if arm_key == "control":
                event = (
                    "single_entry_receipt_60s" if idx % 2 == 0 else "duplicate_entry_created"
                )
            else:
                event = (
                    "duplicate_entry_created" if idx % 10 == 0 else "single_entry_receipt_60s"
                )

            if fs.track_conversion(event, visitor_id):
                bucket[event] += 1
    finally:
        fs.flush()
        fs.close()

    print("QA smoke test flushed (0 dropped):")
    print(counts)


if __name__ == "__main__":
    main()
