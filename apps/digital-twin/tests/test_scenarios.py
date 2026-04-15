"""Tests for scenario API endpoints (Task 6.1)."""
from __future__ import annotations

import os
import sys

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

# Ensure app module is importable
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


async def _seed_indicators():
    """Insert enough indicator data for forecasts to work."""
    async with app.get_db() as db:
        for sid, val in [("CPIAUCSL", 300.0), ("UNRATE", 4.0), ("FEDFUNDS", 5.0),
                         ("DGS10", 4.2), ("GDP_GROWTH", 2.5)]:
            for i in range(12):
                await db.execute(
                    "INSERT INTO indicators (indicator_id, observation_date, value, source) "
                    "VALUES (?, ?, ?, 'TEST')",
                    (sid, f"2024-{i+1:02d}-01", val + i * 0.1),
                )
        await db.commit()


def _patch_invoke_agent(monkeypatch):
    """Patch invoke_agent to avoid real Bedrock calls."""
    async def fake_invoke_agent(agent_type, context):
        return {
            "beliefs": {"outlook": "stable"},
            "action": "hold steady",
            "rationale": "Test rationale for " + agent_type,
        }
    monkeypatch.setattr(app, "invoke_agent", fake_invoke_agent)


# ---------------------------------------------------------------------------
# POST /api/scenarios
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_scenario_valid(client, monkeypatch):
    """Valid shock spec should create a scenario and return trajectory."""
    await _seed_indicators()
    _patch_invoke_agent(monkeypatch)

    resp = await client.post("/api/scenarios", json={
        "variable": "energy_price",
        "magnitude": 10.0,
        "duration": 2,
    })
    assert resp.status_code == 200
    data = resp.json()

    assert "scenario_id" in data
    assert data["shock"]["variable"] == "energy_price"
    assert data["shock"]["magnitude"] == 10.0
    assert data["shock"]["duration"] == 2
    assert len(data["trajectory"]) == 2
    assert len(data["counterfactual"]) == 2
    assert len(data["agents"]) == 4  # 4 agent types

    for tp in data["trajectory"]:
        assert "period" in tp
        assert "inflation" in tp
        assert "gdp_growth" in tp
        assert "unemployment" in tp


@pytest.mark.asyncio
async def test_create_scenario_invalid_variable(client):
    """Unrecognized variable should return 422."""
    resp = await client.post("/api/scenarios", json={
        "variable": "not_a_real_variable",
        "magnitude": 5.0,
        "duration": 1,
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_scenario_negative_duration(client):
    """Negative duration should return 422."""
    resp = await client.post("/api/scenarios", json={
        "variable": "cpi",
        "magnitude": 5.0,
        "duration": -1,
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_scenario_zero_duration(client):
    """Zero duration should return 422."""
    resp = await client.post("/api/scenarios", json={
        "variable": "cpi",
        "magnitude": 5.0,
        "duration": 0,
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_scenario_missing_fields(client):
    """Missing required fields should return 422."""
    resp = await client.post("/api/scenarios", json={
        "variable": "cpi",
    })
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/scenarios/{scenario_id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_scenario_not_found(client):
    """Non-existent scenario_id should return 404."""
    resp = await client.get("/api/scenarios/99999")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_get_scenario_after_create(client, monkeypatch):
    """GET should return the same scenario data that was created via POST."""
    await _seed_indicators()
    _patch_invoke_agent(monkeypatch)

    # Create scenario
    post_resp = await client.post("/api/scenarios", json={
        "variable": "interest_rate",
        "magnitude": 5.0,
        "duration": 2,
    })
    assert post_resp.status_code == 200
    created = post_resp.json()
    scenario_id = created["scenario_id"]

    # Retrieve it
    get_resp = await client.get(f"/api/scenarios/{scenario_id}")
    assert get_resp.status_code == 200
    fetched = get_resp.json()

    assert fetched["scenario_id"] == scenario_id
    assert fetched["shock"]["variable"] == "interest_rate"
    assert fetched["shock"]["magnitude"] == 5.0
    assert fetched["shock"]["duration"] == 2
    assert len(fetched["trajectory"]) == 2
    assert len(fetched["counterfactual"]) == 2
    assert len(fetched["agents"]) == 4
