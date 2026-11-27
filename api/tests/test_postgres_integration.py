from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.flagsmith_service import FlagsmithService
from app.main import create_app

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "").strip()


@pytest.mark.skipif(not TEST_DB_URL, reason="Set TEST_DATABASE_URL to run live Postgres integration tests.")
def test_postgres_schema_and_end_to_end_smoke():
    settings = Settings(
        database_url=TEST_DB_URL,
        flagsmith_environment_key="",
        flagsmith_api_url=None,
        flagsmith_events_api_url=None,
        experiment_name=f"test_exp_{uuid4().hex[:8]}",
        recovery_window_seconds=60,
        cookie_secret="pg-test-cookie-secret",
        balance_secret="pg-test-balance-secret",
        app_origin="http://localhost:3000",
        environment="development",
    )
    app = create_app(settings=settings, flagsmith=FlagsmithService(settings))
    with TestClient(app) as client:
        h = client.get("/api/health")
        assert h.status_code == 200
        assert h.json()["database"] == "ok"

        s = client.post("/api/session", headers={"Origin": "http://localhost:3000"})
        assert s.status_code == 200
        tid = client.post("/api/trials", json={"title": "PG smoke"}, headers={"Origin": "http://localhost:3000"}).json()["trialId"]
        sub = client.post(
            f"/api/trials/{tid}/submit",
            json={"actionId": str(uuid4()), "intent": "initial"},
            headers={"Origin": "http://localhost:3000"},
        )
        assert sub.status_code == 202
        ret = client.post(
            f"/api/trials/{tid}/submit",
            json={"actionId": str(uuid4()), "intent": "retry"},
            headers={"Origin": "http://localhost:3000"},
        )
        assert ret.status_code == 200
        code = ret.json()["receiptCode"]
        disp = client.post(
            f"/api/receipts/{code}/displayed",
            json={"actionId": str(uuid4())},
            headers={"Origin": "http://localhost:3000"},
        )
        assert disp.status_code == 200
        assert disp.json()["state"] == "finished"

        s_reload = client.post("/api/session", headers={"Origin": "http://localhost:3000"})
        assert s_reload.status_code == 200
        assert s_reload.json()["activeTrialId"] == tid
