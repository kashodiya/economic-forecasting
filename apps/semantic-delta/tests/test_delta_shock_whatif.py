"""Tests for apply_delta_shock() and compute_whatif()."""

import os
import sys
import pytest
import aiosqlite

# Ensure the app module is importable
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), ".."),
)

import app as semantic_app

TEST_DB = os.path.join(os.path.dirname(__file__), "test_delta_shock_whatif.db")


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
            value = base_value + i * 0.5
            await db.execute(
                """INSERT OR IGNORE INTO observations
                   (series_id, source, observation_date, value, vintage_date, frequency)
                   VALUES (?, 'FRED', ?, ?, '2025-01-01', 'monthly')""",
                (series_id, obs_date, value),
            )
        await db.commit()


async def _count_forecast_rows(indicator_id: str) -> int:
    """Return the number of forecast rows for *indicator_id*."""
    async with aiosqlite.connect(TEST_DB) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM forecasts WHERE indicator_id = ?",
            (indicator_id,),
        )
        row = await cursor.fetchone()
        return row[0]


# ---------------------------------------------------------------------------
# Tests — apply_delta_shock
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_apply_delta_shock_up_increases_values():
    """An 'up' delta should increase every point_value."""
    await _seed_observations("GDP", 24)
    base = await semantic_app.generate_forecast("GDP")
    delta = {"direction": "up", "magnitude": 5.0, "id": None}
    updated = await semantic_app.apply_delta_shock("GDP", delta)

    for orig, upd in zip(base["periods"], updated["periods"]):
        assert upd["point_value"] >= orig["point_value"], (
            f"Expected {upd['point_value']} >= {orig['point_value']}"
        )


@pytest.mark.asyncio
async def test_apply_delta_shock_down_decreases_values():
    """A 'down' delta should decrease every point_value."""
    await _seed_observations("CPIAUCSL", 24)
    base = await semantic_app.generate_forecast("CPIAUCSL")
    delta = {"direction": "down", "magnitude": 3.0, "id": None}
    updated = await semantic_app.apply_delta_shock("CPIAUCSL", delta)

    for orig, upd in zip(base["periods"], updated["periods"]):
        assert upd["point_value"] <= orig["point_value"], (
            f"Expected {upd['point_value']} <= {orig['point_value']}"
        )


@pytest.mark.asyncio
async def test_apply_delta_shock_exact_shift():
    """Verify the additive shift is exactly magnitude."""
    await _seed_observations("PCE", 24)
    base = await semantic_app.generate_forecast("PCE")
    magnitude = 2.5
    delta = {"direction": "up", "magnitude": magnitude, "id": None}
    updated = await semantic_app.apply_delta_shock("PCE", delta)

    for orig, upd in zip(base["periods"], updated["periods"]):
        assert abs(upd["point_value"] - orig["point_value"] - magnitude) < 0.01


@pytest.mark.asyncio
async def test_apply_delta_shock_bounds_shift():
    """Upper and lower bounds should shift by the same amount as point_value."""
    await _seed_observations("GDP", 24)
    base = await semantic_app.generate_forecast("GDP")
    magnitude = 4.0
    delta = {"direction": "up", "magnitude": magnitude, "id": None}
    updated = await semantic_app.apply_delta_shock("GDP", delta)

    for orig, upd in zip(base["periods"], updated["periods"]):
        assert abs(upd["upper_bound"] - orig["upper_bound"] - magnitude) < 0.01
        assert abs(upd["lower_bound"] - orig["lower_bound"] - magnitude) < 0.01


@pytest.mark.asyncio
async def test_apply_delta_shock_persists_to_db():
    """Updated forecast should be persisted as new rows."""
    await _seed_observations("GDP", 24)
    await semantic_app.generate_forecast("GDP", horizon=4)
    before_count = await _count_forecast_rows("GDP")

    delta = {"direction": "up", "magnitude": 1.0, "id": None}
    await semantic_app.apply_delta_shock("GDP", delta)
    after_count = await _count_forecast_rows("GDP")

    # New rows should have been added (not replaced)
    assert after_count > before_count


@pytest.mark.asyncio
async def test_apply_delta_shock_no_existing_forecast_generates_one():
    """If no forecast exists, apply_delta_shock should generate one first."""
    await _seed_observations("UNRATE", 24)
    # Don't generate a forecast first — let apply_delta_shock handle it
    delta = {"direction": "up", "magnitude": 1.0, "id": None}
    result = await semantic_app.apply_delta_shock("UNRATE", delta)

    assert result["indicator_id"] == "UNRATE"
    assert len(result["periods"]) == 6  # default horizon


@pytest.mark.asyncio
async def test_apply_delta_shock_preserves_period_count():
    """Updated forecast should have the same number of periods as the original."""
    await _seed_observations("GDP", 24)
    base = await semantic_app.generate_forecast("GDP", horizon=4)
    delta = {"direction": "down", "magnitude": 2.0, "id": None}
    updated = await semantic_app.apply_delta_shock("GDP", delta)

    assert len(updated["periods"]) == len(base["periods"])


@pytest.mark.asyncio
async def test_apply_delta_shock_delta_id_in_result():
    """The delta_id from the delta dict should appear in the result."""
    await _seed_observations("GDP", 24)
    await semantic_app.generate_forecast("GDP")
    delta = {"direction": "up", "magnitude": 1.0, "id": 42}
    result = await semantic_app.apply_delta_shock("GDP", delta)
    assert result["delta_id"] == 42


# ---------------------------------------------------------------------------
# Tests — compute_whatif
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_whatif_up_increases_values():
    """An 'up' what-if should increase every point_value."""
    await _seed_observations("GDP", 24)
    base = await semantic_app.generate_forecast("GDP")
    whatif = await semantic_app.compute_whatif("GDP", 5.0, "up")

    for orig, scen in zip(base["periods"], whatif["periods"]):
        assert scen["point_value"] >= orig["point_value"]


@pytest.mark.asyncio
async def test_whatif_down_decreases_values():
    """A 'down' what-if should decrease every point_value."""
    await _seed_observations("GDP", 24)
    base = await semantic_app.generate_forecast("GDP")
    whatif = await semantic_app.compute_whatif("GDP", 3.0, "down")

    for orig, scen in zip(base["periods"], whatif["periods"]):
        assert scen["point_value"] <= orig["point_value"]


@pytest.mark.asyncio
async def test_whatif_is_scenario_flag():
    """What-if result must have is_scenario = True."""
    await _seed_observations("GDP", 24)
    await semantic_app.generate_forecast("GDP")
    result = await semantic_app.compute_whatif("GDP", 1.0, "up")
    assert result["is_scenario"] is True


@pytest.mark.asyncio
async def test_whatif_does_not_persist():
    """What-if must NOT add rows to the forecasts table."""
    await _seed_observations("GDP", 24)
    await semantic_app.generate_forecast("GDP", horizon=4)
    before_count = await _count_forecast_rows("GDP")

    await semantic_app.compute_whatif("GDP", 10.0, "up")
    after_count = await _count_forecast_rows("GDP")

    assert after_count == before_count, (
        f"Expected {before_count} rows, got {after_count} — what-if should not persist"
    )


@pytest.mark.asyncio
async def test_whatif_no_existing_forecast_generates_one():
    """If no forecast exists, compute_whatif should generate one first."""
    await _seed_observations("PCE", 24)
    result = await semantic_app.compute_whatif("PCE", 2.0, "down")

    assert result["indicator_id"] == "PCE"
    assert result["is_scenario"] is True
    assert len(result["periods"]) == 6


@pytest.mark.asyncio
async def test_whatif_preserves_period_count():
    """What-if result should have the same number of periods as the base forecast."""
    await _seed_observations("GDP", 24)
    base = await semantic_app.generate_forecast("GDP", horizon=3)
    whatif = await semantic_app.compute_whatif("GDP", 1.0, "up")

    assert len(whatif["periods"]) == len(base["periods"])


@pytest.mark.asyncio
async def test_whatif_delta_id_is_none():
    """What-if results should have delta_id = None."""
    await _seed_observations("GDP", 24)
    await semantic_app.generate_forecast("GDP")
    result = await semantic_app.compute_whatif("GDP", 1.0, "up")
    assert result["delta_id"] is None
