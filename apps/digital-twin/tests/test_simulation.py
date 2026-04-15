"""Tests for the Simulation Engine (Task 5.2)."""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

# Ensure the app module is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(autouse=True)
async def _fresh_db(tmp_path, monkeypatch):
    """Use a temporary SQLite DB for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    await app.init_db()
    yield


SAMPLE_INDICATORS = {
    "CPIAUCSL": 310.0,
    "UNRATE": 4.0,
    "FEDFUNDS": 5.0,
    "DGS10": 4.2,
    "GDP_GROWTH": 2.5,
}

SAMPLE_SHOCK = {
    "variable": "interest_rate",
    "magnitude": 10.0,
    "duration": 3,
}


# ---------------------------------------------------------------------------
# _apply_shock_to_indicators
# ---------------------------------------------------------------------------

def test_apply_shock_updates_mapped_indicator():
    updated = app._apply_shock_to_indicators(SAMPLE_INDICATORS, SAMPLE_SHOCK, 1)
    # interest_rate maps to FEDFUNDS; per-period delta = 10/3 ≈ 3.333
    expected = SAMPLE_INDICATORS["FEDFUNDS"] + 10.0 / 3
    assert abs(updated["FEDFUNDS"] - expected) < 1e-6


def test_apply_shock_does_not_mutate_original():
    original = dict(SAMPLE_INDICATORS)
    app._apply_shock_to_indicators(original, SAMPLE_SHOCK, 1)
    assert original == SAMPLE_INDICATORS


def test_apply_shock_unknown_variable():
    shock = {"variable": "nonexistent", "magnitude": 5.0, "duration": 1}
    updated = app._apply_shock_to_indicators(SAMPLE_INDICATORS, shock, 1)
    assert updated == SAMPLE_INDICATORS


# ---------------------------------------------------------------------------
# _fallback_agent_response
# ---------------------------------------------------------------------------

def test_fallback_household():
    resp = app._fallback_agent_response("Household", SAMPLE_SHOCK)
    assert "beliefs" in resp and "action" in resp and "rationale" in resp
    assert resp["beliefs"]["consumption_adjustment_pct"] == -(10.0 * 0.3)


def test_fallback_firm():
    resp = app._fallback_agent_response("Firm", SAMPLE_SHOCK)
    assert resp["beliefs"]["price_adjustment_pct"] == 10.0 * 0.5
    assert resp["beliefs"]["hiring_status"] == "frozen"


def test_fallback_bank():
    resp = app._fallback_agent_response("Bank", SAMPLE_SHOCK)
    assert resp["beliefs"]["lending_standard_tightening_pct"] == 10.0 * 0.2


def test_fallback_policymaker():
    resp = app._fallback_agent_response("Policymaker", SAMPLE_SHOCK)
    assert resp["beliefs"]["rate_adjustment_pct"] == 10.0 * 0.1


# ---------------------------------------------------------------------------
# _aggregate_macro_outcomes
# ---------------------------------------------------------------------------

def test_aggregate_returns_required_keys():
    agents = [
        app._fallback_agent_response(at, SAMPLE_SHOCK)
        for at in app._AGENT_ORDER
    ]
    for a, at in zip(agents, app._AGENT_ORDER):
        a["agent_type"] = at
    macro = app._aggregate_macro_outcomes(agents, SAMPLE_INDICATORS, SAMPLE_SHOCK)
    assert "inflation" in macro
    assert "gdp_growth" in macro
    assert "unemployment" in macro


# ---------------------------------------------------------------------------
# run_simulation (with mocked invoke_agent)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_simulation_returns_correct_periods():
    """Simulation should return exactly duration periods."""
    with patch.object(app, "invoke_agent", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.side_effect = Exception("LLM unavailable")
        result = await app.run_simulation(SAMPLE_SHOCK, SAMPLE_INDICATORS)

    assert "periods" in result
    assert len(result["periods"]) == SAMPLE_SHOCK["duration"]


@pytest.mark.asyncio
async def test_run_simulation_period_structure():
    """Each period should have the required keys."""
    with patch.object(app, "invoke_agent", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.side_effect = Exception("LLM unavailable")
        result = await app.run_simulation(SAMPLE_SHOCK, SAMPLE_INDICATORS)

    for p in result["periods"]:
        assert "period" in p
        assert "inflation" in p
        assert "gdp_growth" in p
        assert "unemployment" in p
        assert "agents" in p
        assert len(p["agents"]) == 4


@pytest.mark.asyncio
async def test_run_simulation_agent_order():
    """Agents should appear in the correct order: Policymaker, Bank, Firm, Household."""
    with patch.object(app, "invoke_agent", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.side_effect = Exception("LLM unavailable")
        result = await app.run_simulation(SAMPLE_SHOCK, SAMPLE_INDICATORS)

    for p in result["periods"]:
        agent_types = [a["agent_type"] for a in p["agents"]]
        assert agent_types == ["Policymaker", "Bank", "Firm", "Household"]


@pytest.mark.asyncio
async def test_run_simulation_stores_agent_states():
    """Agent states should be persisted in the DB."""
    with patch.object(app, "invoke_agent", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.side_effect = Exception("LLM unavailable")
        await app.run_simulation(SAMPLE_SHOCK, SAMPLE_INDICATORS)

    async with app.get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM agent_states")
        count = (await cursor.fetchone())[0]

    # 3 periods * 4 agents = 12 rows
    assert count == 12


@pytest.mark.asyncio
async def test_run_simulation_uses_llm_when_available():
    """When invoke_agent succeeds, its response should be used."""
    llm_response = {
        "beliefs": {"outlook": "optimistic"},
        "action": "increase spending",
        "rationale": "LLM-generated rationale",
    }
    with patch.object(app, "invoke_agent", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = llm_response
        result = await app.run_simulation(
            {"variable": "interest_rate", "magnitude": 5.0, "duration": 1},
            SAMPLE_INDICATORS,
        )

    agents = result["periods"][0]["agents"]
    for a in agents:
        assert a["rationale"] == "LLM-generated rationale"


@pytest.mark.asyncio
async def test_run_simulation_fallback_on_llm_failure():
    """When invoke_agent fails, fallback heuristics should be used."""
    with patch.object(app, "invoke_agent", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.side_effect = Exception("LLM unavailable")
        result = await app.run_simulation(
            {"variable": "interest_rate", "magnitude": 5.0, "duration": 1},
            SAMPLE_INDICATORS,
        )

    agents = result["periods"][0]["agents"]
    for a in agents:
        assert "Heuristic fallback" in a["rationale"]


# ---------------------------------------------------------------------------
# get_current_indicators
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_current_indicators_empty_db():
    result = await app.get_current_indicators()
    assert result == {}


@pytest.mark.asyncio
async def test_get_current_indicators_returns_latest():
    """Should return the most recent value for each indicator."""
    async with app.get_db() as db:
        await db.execute(
            "INSERT INTO indicators (indicator_id, observation_date, value, source) VALUES (?, ?, ?, ?)",
            ("CPIAUCSL", "2024-01-01", 300.0, "FRED"),
        )
        await db.execute(
            "INSERT INTO indicators (indicator_id, observation_date, value, source) VALUES (?, ?, ?, ?)",
            ("CPIAUCSL", "2024-06-01", 310.0, "FRED"),
        )
        await db.execute(
            "INSERT INTO indicators (indicator_id, observation_date, value, source) VALUES (?, ?, ?, ?)",
            ("UNRATE", "2024-03-01", 3.8, "FRED"),
        )
        await db.commit()

    result = await app.get_current_indicators()
    assert result["CPIAUCSL"] == 310.0
    assert result["UNRATE"] == 3.8
