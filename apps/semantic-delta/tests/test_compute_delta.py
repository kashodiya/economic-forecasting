"""Tests for compute_delta() and compute_all_deltas() (Task 3.3)."""
import os
import sys
import pytest
import aiosqlite
from unittest.mock import AsyncMock, patch

# Ensure the app module is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import init_db, compute_delta, compute_all_deltas

TEST_DB = os.path.join(os.path.dirname(__file__), "test_compute_delta.db")


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    """Use a temporary DB for tests and stub out LLM calls."""
    import app as app_module
    monkeypatch.setattr(app_module, "DB_PATH", TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


async def _seed_observations(rows: list[tuple]):
    """Insert observation rows: (series_id, source, observation_date, value, vintage_date)."""
    async with aiosqlite.connect(TEST_DB) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO observations "
            "(series_id, source, observation_date, value, vintage_date) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        await db.commit()


# ---- compute_delta: no observations → None ----

@pytest.mark.asyncio
async def test_compute_delta_no_observations():
    await init_db()
    result = await compute_delta("NONEXISTENT")
    assert result is None


# ---- compute_delta: single vintage → initial baseline ----

@pytest.mark.asyncio
async def test_compute_delta_single_vintage_initial():
    await init_db()
    await _seed_observations([
        ("GDP", "FRED", "2024-01-01", 100.0, "2024-03-01"),
        ("GDP", "FRED", "2024-04-01", 110.0, "2024-03-01"),
        ("GDP", "FRED", "2024-07-01", 120.0, "2024-03-01"),
    ])

    result = await compute_delta("GDP")

    assert result is not None
    assert result["direction"] == "initial"
    assert result["vintage_date_old"] is None
    assert result["vintage_date_new"] == "2024-03-01"
    # magnitude = mean([100, 110, 120]) = 110.0
    assert abs(result["magnitude"] - 110.0) < 0.01
    assert result["is_llm_validated"] is False
    assert "Initial baseline" in result["driver_explanation"]


# ---- compute_delta: two vintages → numeric delta + explanation ----

@pytest.mark.asyncio
async def test_compute_delta_two_vintages():
    await init_db()
    # Prior vintage
    await _seed_observations([
        ("GDP", "FRED", "2024-01-01", 100.0, "2024-03-01"),
        ("GDP", "FRED", "2024-04-01", 110.0, "2024-03-01"),
    ])
    # Newer vintage (values went up)
    await _seed_observations([
        ("GDP", "FRED", "2024-01-01", 105.0, "2024-06-01"),
        ("GDP", "FRED", "2024-04-01", 115.0, "2024-06-01"),
    ])

    # Stub LLM to avoid real Bedrock calls
    with patch("app._generate_driver_explanation", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = ("GDP moved up by 5.0", False)
        result = await compute_delta("GDP")

    assert result is not None
    assert result["direction"] == "up"
    assert result["vintage_date_new"] == "2024-06-01"
    assert result["vintage_date_old"] == "2024-03-01"
    # mean([105,115])=110, mean([100,110])=105 → magnitude=5.0
    assert abs(result["magnitude"] - 5.0) < 0.01

    # Verify persisted to DB
    async with aiosqlite.connect(TEST_DB) as db:
        cursor = await db.execute(
            "SELECT direction, magnitude FROM semantic_deltas WHERE series_id = ?",
            ("GDP",),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "up"
        assert abs(row[1] - 5.0) < 0.01


# ---- compute_delta: INSERT OR IGNORE prevents duplicates ----

@pytest.mark.asyncio
async def test_compute_delta_idempotent():
    await init_db()
    await _seed_observations([
        ("CPI", "FRED", "2024-01-01", 300.0, "2024-03-01"),
    ])

    result1 = await compute_delta("CPI")
    result2 = await compute_delta("CPI")

    # Both should succeed (INSERT OR IGNORE), same result
    assert result1 is not None
    assert result2 is not None
    assert result1["direction"] == result2["direction"] == "initial"

    # Only one row in DB
    async with aiosqlite.connect(TEST_DB) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM semantic_deltas WHERE series_id = 'CPI'"
        )
        count = (await cursor.fetchone())[0]
        assert count == 1


# ---- compute_all_deltas: multiple series ----

@pytest.mark.asyncio
async def test_compute_all_deltas_multiple_series():
    await init_db()
    await _seed_observations([
        ("GDP", "FRED", "2024-01-01", 100.0, "2024-03-01"),
        ("CPI", "FRED", "2024-01-01", 300.0, "2024-03-01"),
        ("PCE", "FRED", "2024-01-01", 50.0, "2024-03-01"),
    ])

    results = await compute_all_deltas()

    # Filter out error entries
    deltas = [r for r in results if "_errors" not in r]
    assert len(deltas) == 3
    series_ids = {d["series_id"] for d in deltas}
    assert series_ids == {"GDP", "CPI", "PCE"}
    # All should be "initial" since single vintage each
    assert all(d["direction"] == "initial" for d in deltas)


# ---- compute_all_deltas: catches individual failures ----

@pytest.mark.asyncio
async def test_compute_all_deltas_catches_failures():
    await init_db()
    await _seed_observations([
        ("GDP", "FRED", "2024-01-01", 100.0, "2024-03-01"),
        ("BAD", "FRED", "2024-01-01", 50.0, "2024-03-01"),
    ])

    # Make compute_delta fail for "BAD" series only
    original_compute = compute_delta

    async def _patched(series_id):
        if series_id == "BAD":
            raise RuntimeError("Simulated failure")
        return await original_compute(series_id)

    with patch("app.compute_delta", side_effect=_patched):
        results = await compute_all_deltas()

    # Should have GDP result + error summary
    deltas = [r for r in results if "_errors" not in r]
    errors_entry = [r for r in results if "_errors" in r]

    assert len(deltas) >= 1
    assert any(d["series_id"] == "GDP" for d in deltas)
    assert len(errors_entry) == 1
    assert errors_entry[0]["_errors"][0]["series_id"] == "BAD"
