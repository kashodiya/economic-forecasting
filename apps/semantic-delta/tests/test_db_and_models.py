"""Tests for database initialization and Pydantic models (Task 1.2)."""
import os
import sys
import pytest
import aiosqlite

# Ensure the app module is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import (
    init_db,
    DB_PATH,
    IngestRequest,
    SemanticDelta,
    ForecastRequest,
    ForecastPeriod,
    ForecastResult,
    WhatIfRequest,
    EvidenceLink,
    Narrative,
)


TEST_DB = os.path.join(os.path.dirname(__file__), "test_task12.db")


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    """Use a temporary DB for tests."""
    import app as app_module
    monkeypatch.setattr(app_module, "DB_PATH", TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


@pytest.mark.asyncio
async def test_init_db_creates_tables():
    """init_db should create all four tables."""
    await init_db()
    async with aiosqlite.connect(TEST_DB) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]
    assert "observations" in tables
    assert "semantic_deltas" in tables
    assert "forecasts" in tables
    assert "narratives" in tables


@pytest.mark.asyncio
async def test_init_db_idempotent():
    """Calling init_db twice should not error."""
    await init_db()
    await init_db()


@pytest.mark.asyncio
async def test_observations_unique_constraint():
    """UNIQUE(series_id, observation_date, vintage_date) should prevent duplicates."""
    await init_db()
    async with aiosqlite.connect(TEST_DB) as db:
        await db.execute(
            "INSERT INTO observations (series_id, source, observation_date, value, vintage_date) "
            "VALUES (?, ?, ?, ?, ?)",
            ("GDP", "FRED", "2024-01-01", 100.0, "2024-03-01"),
        )
        await db.commit()
        # Same row again should raise
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO observations (series_id, source, observation_date, value, vintage_date) "
                "VALUES (?, ?, ?, ?, ?)",
                ("GDP", "FRED", "2024-01-01", 100.0, "2024-03-01"),
            )


def test_ingest_request_model():
    req = IngestRequest(source="FRED", series_id="GDP")
    assert req.source == "FRED"
    assert req.dataset_name is None

    req_bea = IngestRequest(source="BEA", series_id="A191RL", dataset_name="NIPA", table_name="T10101")
    assert req_bea.dataset_name == "NIPA"


def test_semantic_delta_model():
    sd = SemanticDelta(
        id=1, series_id="GDP", vintage_date_new="2024-06-01",
        vintage_date_old="2024-03-01", direction="up", magnitude=1.5,
        affected_component=None, confidence_score=0.85,
        driver_explanation="GDP revised upward", is_llm_validated=True,
        created_at="2024-06-01T00:00:00",
    )
    assert sd.direction == "up"
    assert sd.is_llm_validated is True


def test_forecast_models():
    period = ForecastPeriod(period_date="2024-Q3", point_value=105.0, upper_bound=110.0, lower_bound=100.0)
    assert period.lower_bound <= period.point_value <= period.upper_bound

    result = ForecastResult(
        indicator_id="GDP", forecast_date="2024-06-15",
        periods=[period], model_type="ETS",
    )
    assert len(result.periods) == 1
    assert result.delta_id is None


def test_whatif_request_model():
    req = WhatIfRequest(indicator_id="GDP", shock_magnitude=2.0, shock_direction="up")
    assert req.shock_direction == "up"


def test_evidence_link_model():
    link = EvidenceLink(source="FRED", series_id="CPIAUCSL", date="2024-12-01", value=315.6, label="CPI Dec 2024")
    assert link.source == "FRED"


def test_narrative_model():
    link = EvidenceLink(source="FRED", series_id="GDP", date="2024-06-01", value=100.0, label="GDP Q2")
    narr = Narrative(
        id=1, delta_id=1, forecast_id=1, indicator_id="GDP",
        narrative_text="GDP rose.", evidence_links=[link],
        is_scenario=False, created_at="2024-06-01T00:00:00",
    )
    assert len(narr.evidence_links) == 1
    assert narr.is_scenario is False
