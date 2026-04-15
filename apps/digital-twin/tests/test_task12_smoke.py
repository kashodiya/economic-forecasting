"""Smoke test for task 1.2 — schema init and Pydantic models."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as twin_app
import pytest
import pytest_asyncio
import aiosqlite
from pydantic import ValidationError


EXPECTED_TABLES = sorted([
    "indicators",
    "forecasts",
    "forecast_explanations",
    "scenarios",
    "scenario_trajectories",
    "agent_states",
    "alerts",
])


@pytest_asyncio.fixture
async def tmp_db(tmp_path):
    """Override DB_PATH to a temp file, run init_db, yield path."""
    db_path = str(tmp_path / "test.db")
    original = twin_app.DB_PATH
    twin_app.DB_PATH = db_path
    await twin_app.init_db()
    yield db_path
    twin_app.DB_PATH = original


@pytest.mark.asyncio
async def test_all_tables_created(tmp_db):
    async with aiosqlite.connect(tmp_db) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = await cursor.fetchall()
        table_names = sorted([r[0] for r in rows if not r[0].startswith("sqlite_")])
    assert EXPECTED_TABLES == table_names


@pytest.mark.asyncio
async def test_indicator_unique_constraint(tmp_db):
    async with aiosqlite.connect(tmp_db) as db:
        await db.execute(
            "INSERT INTO indicators (indicator_id, observation_date, value, source) VALUES (?, ?, ?, ?)",
            ("CPI", "2024-01-01", 100.0, "FRED"),
        )
        await db.commit()
        with pytest.raises(Exception):
            await db.execute(
                "INSERT INTO indicators (indicator_id, observation_date, value, source) VALUES (?, ?, ?, ?)",
                ("CPI", "2024-01-01", 200.0, "FRED"),
            )
            await db.commit()


def test_shock_spec_valid():
    s = twin_app.ShockSpecification(variable="oil_price", magnitude=10.0, duration=3)
    assert s.variable == "oil_price"
    assert s.duration == 3


def test_shock_spec_bad_duration():
    with pytest.raises(ValidationError):
        twin_app.ShockSpecification(variable="oil_price", magnitude=10.0, duration=0)


def test_shock_spec_bad_variable():
    with pytest.raises(ValidationError):
        twin_app.ShockSpecification(variable="unknown_var", magnitude=10.0, duration=2)


def test_recognized_variables():
    assert len(twin_app.RECOGNIZED_VARIABLES) == 9
    assert "energy_price" in twin_app.RECOGNIZED_VARIABLES
    assert "gdp_growth" in twin_app.RECOGNIZED_VARIABLES


def test_alert_model():
    a = twin_app.AlertModel(
        id=1,
        indicator_id="CPI",
        observed_value=105.0,
        p10_value=98.0,
        p90_value=103.0,
        severity="warning",
        created_at="2024-01-01T00:00:00",
    )
    assert a.severity == "warning"
    assert a.driver_attribution is None


def test_forecast_result():
    fr = twin_app.ForecastResult(
        forecast_id=1,
        indicator_id="CPI",
        periods=[
            twin_app.ForecastPeriod(date="2024-Q1", p10=95.0, p50=100.0, p90=105.0)
        ],
    )
    assert len(fr.periods) == 1
    assert fr.explanation is None


def test_scenario_result():
    sr = twin_app.ScenarioResult(
        scenario_id=1,
        shock=twin_app.ShockSpecification(variable="oil_price", magnitude=20.0, duration=2),
        trajectory=[
            twin_app.TrajectoryPeriod(period=1, inflation=2.5, gdp_growth=1.0, unemployment=4.0)
        ],
        counterfactual=[
            twin_app.TrajectoryPeriod(period=1, inflation=2.0, gdp_growth=1.5, unemployment=3.8)
        ],
        agents=[
            twin_app.AgentResponse(
                agent_type="Household",
                beliefs={"inflation": "rising"},
                action="reduce spending",
                rationale="Prices are up",
            )
        ],
    )
    assert sr.scenario_id == 1
    assert sr.shock.variable == "oil_price"
