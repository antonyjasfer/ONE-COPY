from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from flagsmith import Flagsmith
from flagsmith.analytics import EventProcessorConfig
from flagsmith.models import DefaultFlag, Flag

from app.config import Settings

logger = logging.getLogger(__name__)

VariantName = Literal["retry", "check_status"]
CohortName = str


@dataclass(frozen=True)
class FlagEvaluation:
    cohort: CohortName
    variant: VariantName
    flag_variant_key: str | None
    is_default: bool
    in_experiment: bool


def _default_flag_handler(_feature_name: str) -> DefaultFlag:
    return DefaultFlag(enabled=False, value="retry")


class FlagsmithService:
    """Process-wide Flagsmith client wrapper with local eval and instant event flush."""

    def __init__(self, settings: Settings, client: Flagsmith | None = None) -> None:
        self._experiment_name = settings.experiment_name
        self._enrolled_cohort = settings.app_cohort
        self._client = client

        if self._client is None and settings.flagsmith_environment_key:
            key = settings.flagsmith_environment_key
            use_local_eval = key.startswith("ser.")
            if not use_local_eval:
                logger.warning(
                    "FLAGSMITH_ENVIRONMENT_KEY does not start with 'ser.'; "
                    "disabling local evaluation."
                )
            event_cfg_kwargs: dict[str, object] = {
                "max_buffer_items": 100,
                "flush_interval_seconds": 1.0,
            }
            if settings.flagsmith_events_api_url:
                event_cfg_kwargs["events_api_url"] = settings.flagsmith_events_api_url

            client_kwargs: dict[str, object] = {
                "environment_key": key,
                "enable_events": True,
                "enable_local_evaluation": use_local_eval,
                "environment_refresh_interval_seconds": 15,
                "event_processor_config": EventProcessorConfig(**event_cfg_kwargs),
                "default_flag_handler": _default_flag_handler,
            }
            if settings.flagsmith_api_url:
                client_kwargs["api_url"] = settings.flagsmith_api_url

            self._client = Flagsmith(**client_kwargs)  # type: ignore[arg-type]

    @property
    def is_configured(self) -> bool:
        return self._client is not None

    def evaluate_and_expose(self, visitor_id: UUID) -> FlagEvaluation:
        """Evaluate the experiment flag and trigger Flagsmith exposure for enrolled visitors."""
        if self._client is None:
            return FlagEvaluation(
                cohort="practice",
                variant="retry",
                flag_variant_key=None,
                is_default=True,
                in_experiment=False,
            )

        try:
            flag = self._client.get_experiment_flag(
                self._experiment_name,
                identifier=str(visitor_id),
            )
        except Exception:
            logger.exception("Flagsmith evaluation failed; falling back to practice cohort.")
            return FlagEvaluation(
                cohort="practice",
                variant="retry",
                flag_variant_key=None,
                is_default=True,
                in_experiment=False,
            )

        return self.interpret_flag(flag, enrolled_cohort=self._enrolled_cohort)

    @staticmethod
    def interpret_flag(
        flag: Flag | DefaultFlag,
        *,
        enrolled_cohort: str = "live",
    ) -> FlagEvaluation:
        is_default = isinstance(flag, DefaultFlag) or bool(getattr(flag, "is_default", False))
        flag_variant = getattr(flag, "variant", None)
        experiment = getattr(flag, "experiment", None)
        in_experiment = bool(experiment and getattr(experiment, "in_experiment", False))
        raw_value = str(getattr(flag, "value", "retry") or "retry")

        is_enrolled = (
            (not is_default)
            and bool(getattr(flag, "enabled", False))
            and in_experiment
            and (flag_variant is not None)
        )

        if not is_enrolled:
            return FlagEvaluation(
                cohort="practice",
                variant="retry",
                flag_variant_key=flag_variant,
                is_default=is_default,
                in_experiment=in_experiment,
            )

        variant: VariantName = "check_status" if raw_value == "check_status" else "retry"
        return FlagEvaluation(
            cohort=enrolled_cohort,
            variant=variant,
            flag_variant_key=flag_variant,
            is_default=False,
            in_experiment=True,
        )

    def track_conversion(self, event_name: str, visitor_id: UUID) -> bool:
        if self._client is None:
            return False
        try:
            self._client.track_event(event=event_name, identifier=str(visitor_id))
            return True
        except Exception:
            logger.exception("Flagsmith track_event(%s) failed.", event_name)
            return False

    def flush(self) -> None:
        if self._client is not None:
            try:
                self._client.flush_events()
            except Exception:
                logger.exception("Error flushing Flagsmith events.")

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                logger.exception("Error closing Flagsmith client.")
