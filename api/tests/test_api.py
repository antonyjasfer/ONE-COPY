from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from flagsmith.models import DefaultFlag, ExperimentMetadata, Flag

from app.allocation import CROCKFORD_ALPHABET, paired_failure_mode
from app.config import Settings
from app.flagsmith_service import FlagsmithService
from app.main import create_app
from tests.fake_store import InMemoryTrialStore

ORIGIN = "http://localhost:3000"


class FakeFlagsmithSDK:
    """Deterministic Flagsmith SDK stand-in recording exposures & custom events."""

    def __init__(self, flag_to_return: Flag | DefaultFlag) -> None:
        self.flag_to_return = flag_to_return
        self.exposures: list[tuple[str, str, str | None]] = []
        self.events: list[tuple[str, str]] = []
        self.closed = False

    def get_experiment_flag(self, feature_name: str, identifier: str, traits=None):
        f = self.flag_to_return
        if (
            isinstance(f, Flag)
            and f.enabled
            and f.experiment
            and f.experiment.in_experiment
            and f.variant is not None
        ):
            self.exposures.append((feature_name, identifier, f.variant))
        return f

    def track_event(self, event: str, identifier: str | None = None, **kwargs: Any) -> None:
        assert identifier is not None
        self.events.append((event, identifier))

    def close(self) -> None:
        self.closed = True


class MutableClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 11, 27, 9, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current = self.current + timedelta(seconds=seconds)


def make_flag(variant_key: str, value: str, *, in_experiment: bool = True, enabled: bool = True) -> Flag:
    return Flag(
        enabled=enabled,
        value=value,
        feature_id=1,
        feature_name="submission_recovery",
        variant=variant_key,
        experiment=ExperimentMetadata(id=10, name="submission_recovery", in_experiment=in_experiment),
    )


def make_settings(
    *,
    balance_secret: str = "test-balance-secret",
    environment: str = "development",
    app_cohort: str | None = None,
) -> Settings:
    resolved_cohort = app_cohort or ("live" if environment == "production" else "qa")
    return Settings(
        database_url="postgresql://postgres:postgres@localhost:5432/one_copy",
        flagsmith_environment_key="",
        flagsmith_api_url=None,
        flagsmith_events_api_url=None,
        experiment_name="submission_recovery",
        recovery_window_seconds=60,
        cookie_secret="test-cookie-secret",
        balance_secret=balance_secret,
        app_origin=ORIGIN,
        environment=environment,
        app_cohort=resolved_cohort,
    )


def find_secret_for_first_pair(first_mode: str) -> str:
    """Find a balance_secret whose pair_index=0 slot=0 yields `first_mode`."""
    for i in range(100):
        secret = f"secret-{i}"
        if paired_failure_mode(secret, "submission_recovery", 0) == first_mode:
            return secret
    raise RuntimeError("Could not find secret")


def build_client(
    flag: Flag | DefaultFlag,
    *,
    first_mode: str = "reply_lost",
    environment: str = "development",
):
    secret = find_secret_for_first_pair(first_mode)
    settings = make_settings(balance_secret=secret, environment=environment)
    store = InMemoryTrialStore()
    sdk = FakeFlagsmithSDK(flag)
    fs = FlagsmithService(settings, client=sdk)  # type: ignore[arg-type]
    clock = MutableClock()
    app = create_app(settings=settings, store=store, flagsmith=fs, now_fn=clock)
    return app, store, sdk, clock


def start_session_and_trial(client: TestClient, title: str = "Test entry") -> UUID:
    r1 = client.post("/api/session", headers={"Origin": ORIGIN})
    assert r1.status_code == 200
    assert r1.headers["cache-control"] == "no-store"
    r2 = client.post("/api/trials", json={"title": title}, headers={"Origin": ORIGIN})
    assert r2.status_code == 201
    return UUID(r2.json()["trialId"])


def test_paired_failure_allocation_balanced_and_identical_across_arms():
    secret = "spidey-sense-balance-key"
    arm_a = [paired_failure_mode(secret, "submission_recovery", i) for i in range(20)]
    arm_b = [paired_failure_mode(secret, "submission_recovery", i) for i in range(20)]

    assert arm_a == arm_b
    for k in range(0, 20, 2):
        assert set(arm_a[k : k + 2]) == {"reply_lost", "request_lost"}


def test_combo_1_variant_a_reply_lost_creates_duplicate_and_blocks_primary():
    flag = make_flag("control", "retry")
    app, store, sdk, clock = build_client(flag, first_mode="reply_lost")

    with TestClient(app) as client:
        trial_id = start_session_and_trial(client, "Combo 1")
        # Initial submit -> 202 recovering, failureMode hidden
        init_resp = client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": str(uuid4()), "intent": "initial"},
            headers={"Origin": ORIGIN},
        )
        assert init_resp.status_code == 202
        body = init_resp.json()
        assert body["state"] == "recovering"
        assert body["variant"] == "retry"
        assert "failureMode" not in body

        # GET /api/trials/{id} must NOT reveal receiptCode or failureMode while recovering
        detail = client.get(f"/api/trials/{trial_id}").json()
        assert detail["state"] == "recovering"
        assert detail["cohort"] == "qa"
        assert detail["receiptCode"] is None
        assert detail["failureMode"] is None
        assert detail["allowedActions"] == ["retry"]

        # Variant A cannot call check-status
        cs_err = client.post(
            f"/api/trials/{trial_id}/check-status",
            json={},
            headers={"Origin": ORIGIN},
        )
        assert cs_err.status_code == 409

        # Retry creates Entry #2 -> duplicate_entry_created fires
        clock.advance(5)
        retry_resp = client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": str(uuid4()), "intent": "retry"},
            headers={"Origin": ORIGIN},
        )
        assert retry_resp.status_code == 200
        code = retry_resp.json()["receiptCode"]
        assert len(code) == 8 and all(c in CROCKFORD_ALPHABET for c in code)
        assert ("duplicate_entry_created", str(store.trials[trial_id]["visitor_id"])) in sdk.events

        # Mark displayed within 60s -> finished, primaryConverted=False (entry_count == 2)
        clock.advance(2)
        disp = client.post(
            f"/api/receipts/{code}/displayed",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        )
        assert disp.status_code == 200
        d = disp.json()
        assert d["state"] == "finished"
        assert d["failureMode"] == "reply_lost"
        assert d["finalEntryCount"] == 2
        assert d["primaryConverted"] is False
        assert not any(ev == "single_entry_receipt_60s" for ev, _ in sdk.events)


def test_combo_2_variant_a_request_lost_recovers_single_entry():
    flag = make_flag("control", "retry")
    app, store, sdk, clock = build_client(flag, first_mode="request_lost")

    with TestClient(app) as client:
        trial_id = start_session_and_trial(client, "Combo 2")
        init_resp = client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": str(uuid4()), "intent": "initial"},
            headers={"Origin": ORIGIN},
        )
        assert init_resp.status_code == 202

        clock.advance(6)
        retry_resp = client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": str(uuid4()), "intent": "retry"},
            headers={"Origin": ORIGIN},
        )
        assert retry_resp.status_code == 200
        code = retry_resp.json()["receiptCode"]

        receipt = client.get(f"/api/receipts/{code}")
        assert receipt.status_code == 200
        assert receipt.json()["title"] == "Combo 2"

        clock.advance(3)
        disp = client.post(
            f"/api/receipts/{code}/displayed",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        )
        assert disp.status_code == 200
        d = disp.json()
        assert d["state"] == "finished"
        assert d["failureMode"] == "request_lost"
        assert d["finalEntryCount"] == 1
        assert d["primaryConverted"] is True
        assert sdk.events == [("single_entry_receipt_60s", str(store.trials[trial_id]["visitor_id"]))]


def test_combo_3_variant_b_reply_lost_check_status_finds_single_entry():
    flag = make_flag("check-status-first", "check_status")
    app, store, sdk, clock = build_client(flag, first_mode="reply_lost")

    with TestClient(app) as client:
        trial_id = start_session_and_trial(client, "Combo 3")
        init_resp = client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": str(uuid4()), "intent": "initial"},
            headers={"Origin": ORIGIN},
        )
        assert init_resp.status_code == 202
        assert init_resp.json()["variant"] == "check_status"

        clock.advance(4)
        cs = client.post(
            f"/api/trials/{trial_id}/check-status",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        )
        assert cs.status_code == 200
        assert cs.json()["found"] is True
        code = cs.json()["receiptCode"]

        clock.advance(2)
        disp = client.post(
            f"/api/receipts/{code}/displayed",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        )
        assert disp.status_code == 200
        d = disp.json()
        assert d["state"] == "finished"
        assert d["failureMode"] == "reply_lost"
        assert d["finalEntryCount"] == 1
        assert d["primaryConverted"] is True
        assert sdk.events == [("single_entry_receipt_60s", str(store.trials[trial_id]["visitor_id"]))]


def test_combo_4_variant_b_request_lost_check_status_empty_then_retry():
    flag = make_flag("check-status-first", "check_status")
    app, store, sdk, clock = build_client(flag, first_mode="request_lost")

    with TestClient(app) as client:
        trial_id = start_session_and_trial(client, "Combo 4")
        init_resp = client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": str(uuid4()), "intent": "initial"},
            headers={"Origin": ORIGIN},
        )
        assert init_resp.status_code == 202

        clock.advance(3)
        cs = client.post(
            f"/api/trials/{trial_id}/check-status",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        )
        assert cs.status_code == 200
        assert cs.json() == {"found": False, "nextAction": "retry"}

        detail = client.get(f"/api/trials/{trial_id}").json()
        assert detail["allowedActions"] == ["retry"]
        assert detail["checkedEmptyAt"] is not None

        clock.advance(4)
        retry_resp = client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": str(uuid4()), "intent": "retry"},
            headers={"Origin": ORIGIN},
        )
        assert retry_resp.status_code == 200
        code = retry_resp.json()["receiptCode"]

        clock.advance(2)
        disp = client.post(
            f"/api/receipts/{code}/displayed",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        )
        assert disp.status_code == 200
        d = disp.json()
        assert d["state"] == "finished"
        assert d["failureMode"] == "request_lost"
        assert d["finalEntryCount"] == 1
        assert d["primaryConverted"] is True
        assert sdk.events == [("single_entry_receipt_60s", str(store.trials[trial_id]["visitor_id"]))]


def test_double_click_idempotency_and_receipt_refresh_non_double_counting():
    flag = make_flag("control", "retry")
    app, store, sdk, clock = build_client(flag, first_mode="reply_lost")

    with TestClient(app) as client:
        trial_id = start_session_and_trial(client, "Idempotent")
        init_action = str(uuid4())
        r1 = client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": init_action, "intent": "initial"},
            headers={"Origin": ORIGIN},
        )
        r2 = client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": init_action, "intent": "initial"},
            headers={"Origin": ORIGIN},
        )
        assert r1.status_code == 202 and r2.status_code == 202
        assert r1.json() == r2.json()
        assert len(sdk.exposures) == 1
        assert len([e for e in store.entries if e["trial_id"] == trial_id]) == 1

        retry_action = str(uuid4())
        rr1 = client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": retry_action, "intent": "retry"},
            headers={"Origin": ORIGIN},
        )
        rr2 = client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": retry_action, "intent": "retry"},
            headers={"Origin": ORIGIN},
        )
        assert rr1.status_code == 200 and rr2.status_code == 200
        assert rr1.json() == rr2.json()
        assert len([e for e in store.entries if e["trial_id"] == trial_id]) == 2
        assert sdk.events.count(("duplicate_entry_created", str(store.trials[trial_id]["visitor_id"]))) == 1

    # Now test receipt refresh non-double-counting on a clean single-entry trial
    flag_b = make_flag("check-status-first", "check_status")
    app2, store2, sdk2, clock2 = build_client(flag_b, first_mode="reply_lost")
    with TestClient(app2) as client2:
        trial_id2 = start_session_and_trial(client2, "Refresh test")
        client2.post(
            f"/api/trials/{trial_id2}/submit",
            json={"actionId": str(uuid4()), "intent": "initial"},
            headers={"Origin": ORIGIN},
        )
        cs = client2.post(
            f"/api/trials/{trial_id2}/check-status",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        )
        code2 = cs.json()["receiptCode"]

        # First /displayed
        d1 = client2.post(
            f"/api/receipts/{code2}/displayed",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        )
        # Simulated page refresh with a BRAND NEW actionId
        clock2.advance(3)
        d2 = client2.post(
            f"/api/receipts/{code2}/displayed",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        )
        assert d1.status_code == 200 and d2.status_code == 200
        assert d1.json()["primaryConverted"] is True
        assert d2.json()["primaryConverted"] is True
        assert len(sdk2.events) == 1


def test_deadline_expiry_prevents_primary_conversion_and_reveals_terminal_state():
    flag = make_flag("check-status-first", "check_status")
    app, store, sdk, clock = build_client(flag, first_mode="reply_lost")

    with TestClient(app) as client:
        trial_id = start_session_and_trial(client, "Expiry test")
        client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": str(uuid4()), "intent": "initial"},
            headers={"Origin": ORIGIN},
        )
        code = client.post(
            f"/api/trials/{trial_id}/check-status",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        ).json()["receiptCode"]

        # Advance beyond 60s window
        clock.advance(61)
        disp = client.post(
            f"/api/receipts/{code}/displayed",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        )
        assert disp.status_code == 200
        d = disp.json()
        assert d["state"] == "expired"
        assert d["primaryConverted"] is False
        assert d["failureMode"] == "reply_lost"
        assert d["finalEntryCount"] == 1
        assert sdk.events == []

        detail = client.get(f"/api/trials/{trial_id}").json()
        assert detail["state"] == "expired"
        assert detail["failureMode"] == "reply_lost"
        assert detail["finalEntryCount"] == 1
        assert detail["allowedActions"] == []


def test_fallback_and_unenrolled_run_in_practice_cohort_with_no_live_conversion():
    for flag in (
        DefaultFlag(enabled=False, value="retry"),
        make_flag("control", "retry", in_experiment=False),
    ):
        app, store, sdk, clock = build_client(flag, first_mode="request_lost")
        with TestClient(app) as client:
            trial_id = start_session_and_trial(client, "Practice run")
            init_resp = client.post(
                f"/api/trials/{trial_id}/submit",
                json={"actionId": str(uuid4()), "intent": "initial"},
                headers={"Origin": ORIGIN},
            )
            assert init_resp.status_code == 202
            assert init_resp.json()["variant"] == "retry"
            assert store.trials[trial_id]["cohort"] == "practice"

            code = client.post(
                f"/api/trials/{trial_id}/submit",
                json={"actionId": str(uuid4()), "intent": "retry"},
                headers={"Origin": ORIGIN},
            ).json()["receiptCode"]
            disp = client.post(
                f"/api/receipts/{code}/displayed",
                json={"actionId": str(uuid4())},
                headers={"Origin": ORIGIN},
            )
            assert disp.status_code == 200
            assert sdk.exposures == []
            assert sdk.events == []
            assert store.event_ledger[(trial_id, "single_entry_receipt_60s")]["dispatch_state"] == "skipped_practice"


def test_security_origin_redirect_slashes_ownership_and_summary():
    flag = make_flag("check-status-first", "check_status")
    app, store, sdk, clock = build_client(flag, first_mode="reply_lost", environment="production")

    with TestClient(app, base_url="https://testserver") as client:
        # redirect_slashes=False: trailing slash must NOT 307
        r_slash = client.get("/api/health/")
        assert r_slash.status_code == 404

        # Production rejects missing Origin and wrong Origin on POST
        assert client.post("/api/session").status_code == 403
        assert client.post("/api/session", headers={"Origin": "https://evil.example"}).status_code == 403

        # Valid Origin sets HttpOnly oc_visitor cookie
        s = client.post("/api/session", headers={"Origin": ORIGIN})
        assert s.status_code == 200
        set_cookie = s.headers.get("set-cookie", "")
        assert "oc_visitor=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "Secure" in set_cookie

        trial_resp = client.post(
            "/api/trials", json={"title": "Secret user title"}, headers={"Origin": ORIGIN}
        )
        assert trial_resp.status_code == 201
        trial_id = UUID(trial_resp.json()["trialId"])
        client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": str(uuid4()), "intent": "initial"},
            headers={"Origin": ORIGIN},
        )
        clock.advance(7)
        code = client.post(
            f"/api/trials/{trial_id}/check-status",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        ).json()["receiptCode"]
        client.post(
            f"/api/receipts/{code}/displayed",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        )

        # Different visitor cannot read receipt
        with TestClient(app, base_url="https://testserver") as other_client:
            other_client.post("/api/session", headers={"Origin": ORIGIN})
            assert other_client.get(f"/api/receipts/{code}").status_code == 403
            assert other_client.get(f"/api/trials/{trial_id}").status_code == 403

        # Summary exposes aggregates without PII or titles
        summary = client.get("/api/stats/summary")
        assert summary.status_code == 200
        sj = summary.json()
        assert sj["cohorts"]["live"] == 1
        assert sj["liveTotalExposed"] == 1
        assert sj["primaryRate"] == 1.0
        assert sj["recoveryTimeSec"]["p50"] == 7.0
        assert "Secret user title" not in summary.text


def test_session_returns_completed_or_expired_trial_for_reload_resume():
    # Finished trial case
    flag = make_flag("check-status-first", "check_status")
    app, _store, _sdk, clock = build_client(flag, first_mode="reply_lost")
    with TestClient(app) as client:
        s_init = client.post("/api/session", headers={"Origin": ORIGIN}).json()
        assert s_init["activeTrialId"] is None

        trial_id = UUID(
            client.post("/api/trials", json={"title": "Reload me"}, headers={"Origin": ORIGIN}).json()["trialId"]
        )
        client.post(
            f"/api/trials/{trial_id}/submit",
            json={"actionId": str(uuid4()), "intent": "initial"},
            headers={"Origin": ORIGIN},
        )
        code = client.post(
            f"/api/trials/{trial_id}/check-status",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        ).json()["receiptCode"]
        client.post(
            f"/api/receipts/{code}/displayed",
            json={"actionId": str(uuid4())},
            headers={"Origin": ORIGIN},
        )

        # Reloading / opening second tab calls POST /api/session and must receive the finished trial
        s_reload = client.post("/api/session", headers={"Origin": ORIGIN})
        assert s_reload.status_code == 200
        assert s_reload.json()["activeTrialId"] == str(trial_id)

        detail = client.get(f"/api/trials/{trial_id}").json()
        assert detail["state"] == "finished"
        assert detail["receiptCode"] == code
        assert detail["failureMode"] == "reply_lost"
        assert detail["finalEntryCount"] == 1

    # Expired trial case
    app2, _store2, _sdk2, clock2 = build_client(flag, first_mode="request_lost")
    with TestClient(app2) as client2:
        trial_id2 = start_session_and_trial(client2, "Expired reload")
        client2.post(
            f"/api/trials/{trial_id2}/submit",
            json={"actionId": str(uuid4()), "intent": "initial"},
            headers={"Origin": ORIGIN},
        )
        clock2.advance(65)
        detail2 = client2.get(f"/api/trials/{trial_id2}").json()
        assert detail2["state"] == "expired"

        s_reload2 = client2.post("/api/session", headers={"Origin": ORIGIN})
        assert s_reload2.status_code == 200
        assert s_reload2.json()["activeTrialId"] == str(trial_id2)
