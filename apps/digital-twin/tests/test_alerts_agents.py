"""Tests for GET /api/alerts and GET /api/agents/{scenario_id} endpoints (Task 6.3)."""
from __future__ import annotations

import json
import os
import sys

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db(tmp_path, monkeypatch):
    """Use a temporary SQLite DB for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    await app.init_db()
    yield


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# GET /api/alerts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_alerts_empty(client):
    """No alerts should return an empty list."""
    resp = await client.get("/api/alerts")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"alerts": []}


@pytest.mark.asyncio
async def test_get_alerts_returns_all_fields(client):
    """Alerts should include all expected fields with parsed driver_attribution."""
    driver = [{"feature_name": "CPI", "importance_score": 0.9, "direction": "positive"}]
    async with app.get_db() as db:
        await db.execute(
            """INSERT INTO alerts
               (indicator_id, observed_value, p10_value, p90_value,
                severity, driver_attribution, narrative)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("CPIAUCSL", 320.0, 295.0, 310.0, "warning",
             json.dumps(driver), "CPI exceeded forecast band."),
        )
        await db.commit()

    resp = await client.get("/api/alerts")
    assert resp.status_code == 200
    alerts = resp.json()["alerts"]
    assert len(alerts) == 1

    a = alerts[0]
    assert a["indicator_id"] == "CPIAUCSL"
    assert a["observed_value"] == 320.0
    assert a["p10_value"] == 295.0
    assert a["p90_value"] == 310.0
    assert a["severity"] == "warning"
    assert a["driver_attribution"] == driver
    assert a["narrative"] == "CPI exceeded forecast band."
    assert "id" in a
    assert "created_at" in a


@pytest.mark.asyncio
async def test_get_alerts_reverse_chronological(client):
    """Alerts should be returned newest-first."""
    async with app.get_db() as db:
        await db.execute(
            """INSERT INTO alerts
               (indicator_id, observed_value, p10_value, p90_value,
                severity, driver_attribution, narrative, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("A", 1.0, 2.0, 3.0, "warning", None, None, "2024-01-01 00:00:00"),
        )
        await db.execute(
            """INSERT INTO alerts
               (indicator_id, observed_value, p10_value, p90_value,
                severity, driver_attribution, narrative, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("B", 5.0, 2.0, 3.0, "critical", None, None, "2024-06-01 00:00:00"),
        )
        await db.commit()

    resp = await client.get("/api/alerts")
    alerts = resp.json()["alerts"]
    assert len(alerts) == 2
    assert alerts[0]["indicator_id"] == "B"  # newer first
    assert alerts[1]["indicator_id"] == "A"


@pytest.mark.asyncio
async def test_get_alerts_null_driver_attribution(client):
    """Null driver_attribution should come back as None/null."""
    async with app.get_db() as db:
        await db.execute(
            """INSERT INTO alerts
               (indicator_id, observed_value, p10_value, p90_value,
                severity, driver_attribution, narrative)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("X", 10.0, 5.0, 8.0, "critical", None, None),
        )
        await db.commit()

    resp = await client.get("/api/alerts")
    a = resp.json()["alerts"][0]
    assert a["driver_attribution"] is None
    assert a["narrative"] is None


# ---------------------------------------------------------------------------
# GET /api/agents/{scenario_id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_agents_not_found(client):
    """Non-existent scenario_id should return 404."""
    resp = await client.get("/api/agents/99999")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_get_agents_returns_latest_period(client):
    """Should return only the latest period's agent states."""
    # Create a scenario row
    async with app.get_db() as db:
        cursor = await db.execute(
            "INSERT INTO scenarios (shock_variable, shock_magnitude, shock_duration) VALUES (?, ?, ?)",
            ("cpi", 5.0, 2),
        )
        scenario_id = cursor.lastrowid

        # Insert agent states for period 1 and period 2
        for period in (1, 2):
            for agent_type in ("Policymaker", "Bank", "Firm", "Household"):
                beliefs = json.dumps({"outlook": f"period_{period}"})
                await db.execute(
                    """INSERT INTO agent_states
                       (scenario_id, period, agent_type, beliefs, action, rationale)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (scenario_id, period, agent_type, beliefs,
                     f"action_{period}", f"rationale_{period}"),
                )
        await db.commit()

    resp = await client.get(f"/api/agents/{scenario_id}")
    assert resp.status_code == 200
    agents = resp.json()["agents"]
    assert len(agents) == 4

    # All should be from period 2 (latest)
    for agent in agents:
        assert agent["beliefs"] == {"outlook": "period_2"}
        assert agent["action"] == "action_2"
        assert agent["rationale"] == "rationale_2"
        assert agent["agent_type"] in ("Policymaker", "Bank", "Firm", "Household")


@pytest.mark.asyncio
async def test_get_agents_beliefs_parsed_from_json(client):
    """Beliefs stored as JSON string should be parsed into a dict."""
    async with app.get_db() as db:
        cursor = await db.execute(
            "INSERT INTO scenarios (shock_variable, shock_magnitude, shock_duration) VALUES (?, ?, ?)",
            ("unemployment", 2.0, 1),
        )
        scenario_id = cursor.lastrowid
        beliefs = json.dumps({"inflation_risk": "high", "rate": 5.5})
        await db.execute(
            """INSERT INTO agent_states
               (scenario_id, period, agent_type, beliefs, action, rationale)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (scenario_id, 1, "Policymaker", beliefs, "raise rate", "inflation is high"),
        )
        await db.commit()

    resp = await client.get(f"/api/agents/{scenario_id}")
    agents = resp.json()["agents"]
    assert len(agents) == 1
    assert agents[0]["beliefs"] == {"inflation_risk": "high", "rate": 5.5}


@pytest.mark.asyncio
async def test_get_agents_empty_states(client):
    """Scenario with no agent states should return empty list."""
    async with app.get_db() as db:
        cursor = await db.execute(
            "INSERT INTO scenarios (shock_variable, shock_magnitude, shock_duration) VALUES (?, ?, ?)",
            ("cpi", 1.0, 1),
        )
        await db.commit()
        scenario_id = cursor.lastrowid

    resp = await client.get(f"/api/agents/{scenario_id}")
    assert resp.status_code == 200
    assert resp.json() == {"agents": []}
