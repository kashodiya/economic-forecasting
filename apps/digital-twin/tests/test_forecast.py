"""Unit tests for Task 4.1 — Forecast Service (generate_forecast)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as twin_app
import pytest
import pytest_asyncio
import aiosqlite


@pytest_asyncio.fixture
async def tmp_db(tmp_path):
    """Override DB_PATH to a temp file, run init_db, yield path."""
    db_path = str(tmp_path / "test_forecast.db")
    original = twin_app.DB_PATH
    twin_app.DB_PATH = db_path
    await twin_app.init_db()
    yield db_path
    twin_app.DB_PATH = original


async def _insert_observations(db_path: str, indicator_id: str, n: int, start_value: float = 100.0):
    """Helper: insert n observations with a simple linear trend."""
    async with aiosqlite.connect(db_path) as db:
        for i in range(n):
            date = f"202{i // 12 + 0:01d}-{(i % 12) + 1:02d}-01"
            value = start_value + i * 0.5
            await db.execute(
                "INSERT OR IGNORE INTO indicators (indicator_id, observation_date, value, source) VALUES (?, ?, ?, ?)",
                (indicator_id, date, value, "TEST"),
            )
        await db.commit()


@pytest.mark.asyncio
async def test_insufficient_data_returns_error(tmp_db):
    """Fewer than 10 observations should return an error dict."""
    await _insert_observations(tmp_db, "TEST_IND", n=5)
    result = await twin_app.generate_forecast("TEST_IND", periods=4)
    assert "error" in result
    assert result["indicator_id"] == "TEST_IND"
    assert result["available"] == 5
    assert result["min_required"] == twin_app.MIN_OBSERVATIONS


@pytest.mark.asyncio
async def test_exactly_minimum_observations_succeeds(tmp_db):
    """Exactly 10 observations should succeed."""
    await _insert_observations(tmp_db, "MIN_IND", n=10)
    result = await twin_app.generate_forecast("MIN_IND", periods=2)
    assert "error" not in result
    assert result["indicator_id"] == "MIN_IND"
    assert len(result["periods"]) == 2


@pytest.mark.asyncio
async def test_forecast_returns_correct_period_count(tmp_db):
    """generate_forecast should return exactly `periods` forecast entries."""
    await _insert_observations(tmp_db, "CPI_TEST", n=24)
    for n_periods in [1, 4, 8]:
        result = await twin_app.generate_forecast("CPI_TEST", periods=n_periods)
        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert len(result["periods"]) == n_periods


@pytest.mark.asyncio
async def test_quantile_ordering(tmp_db):
    """p10 <= p50 <= p90 must hold for every forecast period."""
    await _insert_observations(tmp_db, "QUANT_IND", n=20, start_value=50.0)
    result = await twin_app.generate_forecast("QUANT_IND", periods=4)
    assert "error" not in result
    for period in result["periods"]:
        assert period["p10"] <= period["p50"], f"p10 > p50 in period {period}"
        assert period["p50"] <= period["p90"], f"p50 > p90 in period {period}"


@pytest.mark.asyncio
async def test_forecast_stored_in_db(tmp_db):
    """Forecast rows should be persisted in the forecasts table."""
    await _insert_observations(tmp_db, "STORE_IND", n=15)
    result = await twin_app.generate_forecast("STORE_IND", periods=4)
    assert "error" not in result

    async with aiosqlite.connect(tmp_db) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM forecasts WHERE indicator_id = ?",
            ("STORE_IND",),
        )
        count = (await cursor.fetchone())[0]
    assert count == 4


@pytest.mark.asyncio
async def test_forecast_id_is_first_row(tmp_db):
    """forecast_id should match the id of the first inserted forecast row."""
    await _insert_observations(tmp_db, "ID_IND", n=12)
    result = await twin_app.generate_forecast("ID_IND", periods=3)
    assert "error" not in result

    forecast_id = result["forecast_id"]
    async with aiosqlite.connect(tmp_db) as db:
        cursor = await db.execute(
            "SELECT horizon_period FROM forecasts WHERE id = ?",
            (forecast_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1  # first inserted row is horizon_period=1


@pytest.mark.asyncio
async def test_no_data_returns_error(tmp_db):
    """Indicator with zero observations should return an error dict."""
    result = await twin_app.generate_forecast("NONEXISTENT", periods=4)
    assert "error" in result
    assert result["available"] == 0


@pytest.mark.asyncio
async def test_result_structure(tmp_db):
    """Result dict should have forecast_id, indicator_id, and periods list."""
    await _insert_observations(tmp_db, "STRUCT_IND", n=12)
    result = await twin_app.generate_forecast("STRUCT_IND", periods=4)
    assert "error" not in result
    assert "forecast_id" in result
    assert "indicator_id" in result
    assert "periods" in result
    assert isinstance(result["periods"], list)
    for p in result["periods"]:
        assert "date" in p
        assert "p10" in p
        assert "p50" in p
        assert "p90" in p
