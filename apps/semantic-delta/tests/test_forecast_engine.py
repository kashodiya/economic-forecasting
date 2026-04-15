"""Tests for the Forecast Engine — generate_forecast()."""

import os
import sys
import pytest
import aiosqlite

# Ensure the app module is importable
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), ".."),
)

import app as semantic_app

TEST_DB = os.path.join(os.path.dirname(__file__), "test_forecast_engine.db")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    """Use a temporary DB for tests."""
    monkeypatch.setattr(semantic_app, "DB_PATH", TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


async def _seed_observations(series_id: str, count: int, base_value: float = 100.0):
    """Insert *count* monthly observations for *series_id*."""
    await semantic_app.init_db()
    async with aiosqlite.connect(TEST_DB) as db:
        for i in range(count):
            month = (i % 12) + 1
            year = 2020 + i // 12
            obs_date = f"{year}-{month:02d}-01"
            value = base_value + i * 0.5  # gentle upward trend
            await db.execute(
                """INSERT OR IGNORE INTO observations
                   (series_id, source, observation_date, value, vintage_date, frequency)
                   VALUES (?, 'FRED', ?, ?, '2025-01-01', 'monthly')""",
                (series_id, obs_date, value),
            )
        await db.commit()


# ---------------------------------------------------------------------------
# Tests — Insufficient data
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_zero_observations_returns_400():
    from fastapi import HTTPException
    await semantic_app.init_db()
    with pytest.raises(HTTPException) as exc_info:
        await semantic_app.generate_forecast("NONEXISTENT")
    assert exc_info.value.status_code == 400
    assert "0 observations" in exc_info.value.detail


@pytest.mark.asyncio
async def test_eleven_observations_returns_400():
    from fastapi import HTTPException
    await _seed_observations("GDP", 11)
    with pytest.raises(HTTPException) as exc_info:
        await semantic_app.generate_forecast("GDP")
    assert exc_info.value.status_code == 400
    assert "11 observations" in exc_info.value.detail
    assert "at least 12 required" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Tests — Successful forecast generation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_default_horizon_returns_6_periods():
    await _seed_observations("CPIAUCSL", 24)
    result = await semantic_app.generate_forecast("CPIAUCSL")
    assert result["indicator_id"] == "CPIAUCSL"
    assert len(result["periods"]) == 6
    assert result["model_type"] in ("ETS", "LinearTrend")
    assert result["delta_id"] is None


@pytest.mark.asyncio
async def test_custom_horizon():
    await _seed_observations("GDP", 30)
    result = await semantic_app.generate_forecast("GDP", horizon=3)
    assert len(result["periods"]) == 3


@pytest.mark.asyncio
async def test_bounds_ordering():
    """lower_bound <= point_value <= upper_bound for every period."""
    await _seed_observations("PCE", 36)
    result = await semantic_app.generate_forecast("PCE")
    for p in result["periods"]:
        assert p["lower_bound"] <= p["point_value"] <= p["upper_bound"], (
            f"Bounds violated: {p['lower_bound']} <= {p['point_value']} <= {p['upper_bound']}"
        )


@pytest.mark.asyncio
async def test_periods_have_required_keys():
    await _seed_observations("UNRATE", 20)
    result = await semantic_app.generate_forecast("UNRATE")
    for p in result["periods"]:
        assert "period_date" in p
        assert "point_value" in p
        assert "upper_bound" in p
        assert "lower_bound" in p


@pytest.mark.asyncio
async def test_forecast_date_present():
    await _seed_observations("GDP", 24)
    result = await semantic_app.generate_forecast("GDP")
    assert "forecast_date" in result
    assert len(result["forecast_date"]) == 10  # ISO date format


# ---------------------------------------------------------------------------
# Tests — Persistence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_forecast_rows_persisted():
    await _seed_observations("GDP", 24)
    await semantic_app.generate_forecast("GDP", horizon=4)
    async with aiosqlite.connect(TEST_DB) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM forecasts WHERE indicator_id = ?", ("GDP",),
        )
        row = await cursor.fetchone()
        assert row[0] == 4


@pytest.mark.asyncio
async def test_delta_id_persisted():
    await _seed_observations("GDP", 24)
    await semantic_app.generate_forecast("GDP", horizon=2, delta_id=42)
    async with aiosqlite.connect(TEST_DB) as db:
        cursor = await db.execute(
            "SELECT delta_id FROM forecasts WHERE indicator_id = ? LIMIT 1", ("GDP",),
        )
        row = await cursor.fetchone()
        assert row[0] == 42


# ---------------------------------------------------------------------------
# Tests — Helper functions
# ---------------------------------------------------------------------------
def test_detect_frequency_quarterly():
    assert semantic_app._detect_frequency("GDP") == "quarterly"


def test_detect_frequency_monthly():
    assert semantic_app._detect_frequency("CPIAUCSL") == "monthly"


def test_detect_frequency_bea():
    assert semantic_app._detect_frequency("BEA_T10101_A191RL") == "quarterly"


def test_detect_frequency_unknown():
    assert semantic_app._detect_frequency("UNKNOWN_SERIES") == "monthly"


def test_next_period_monthly():
    from datetime import datetime
    d = datetime(2024, 6, 1)
    assert semantic_app._next_period(d, "monthly", 1) == "2024-07-01"
    assert semantic_app._next_period(d, "monthly", 3) == "2024-09-01"


def test_next_period_quarterly():
    from datetime import datetime
    d = datetime(2024, 3, 1)
    assert semantic_app._next_period(d, "quarterly", 1) == "2024-06-01"
    assert semantic_app._next_period(d, "quarterly", 2) == "2024-09-01"
