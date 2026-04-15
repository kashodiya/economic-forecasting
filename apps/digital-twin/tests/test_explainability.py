"""Tests for the Explainability Service (Task 4.2)."""
from __future__ import annotations

import pytest
import pytest_asyncio

import aiosqlite

from app import (
    DB_PATH,
    init_db,
    get_db,
    compute_feature_importance,
    generate_explanation_text,
    store_explanation_text,
)


@pytest_asyncio.fixture(autouse=True)
async def fresh_db(tmp_path, monkeypatch):
    """Use a temporary database for each test."""
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr("app.DB_PATH", db_file)
    await init_db()
    yield db_file


async def _seed_indicators(db_path: str, data: dict[str, list[tuple[str, float]]]):
    """Insert indicator rows. data = {indicator_id: [(date, value), ...]}"""
    async with aiosqlite.connect(db_path) as db:
        for iid, rows in data.items():
            for date, val in rows:
                await db.execute(
                    "INSERT OR IGNORE INTO indicators (indicator_id, observation_date, value, source) "
                    "VALUES (?, ?, ?, 'TEST')",
                    (iid, date, val),
                )
        await db.commit()


async def _seed_forecast(db_path: str, indicator_id: str) -> int:
    """Insert a single forecast row and return its id."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO forecasts (indicator_id, forecast_date, horizon_period, p10, p50, p90) "
            "VALUES (?, '2025-01-01', 1, 1.0, 2.0, 3.0)",
            (indicator_id,),
        )
        await db.commit()
        return cursor.lastrowid


# ---- compute_feature_importance tests ----

@pytest.mark.asyncio
async def test_feature_importance_returns_sorted_list(fresh_db):
    """Features should be sorted by importance_score descending."""
    dates = [f"2024-{m:02d}-01" for m in range(1, 13)]
    target_vals = [(d, float(i)) for i, d in enumerate(dates)]
    # Perfectly correlated
    corr_vals = [(d, float(i) * 2) for i, d in enumerate(dates)]
    # Weakly correlated (random-ish)
    weak_vals = [(d, float(i % 3)) for i, d in enumerate(dates)]

    await _seed_indicators(fresh_db, {
        "TARGET": target_vals,
        "STRONG": corr_vals,
        "WEAK": weak_vals,
    })
    fid = await _seed_forecast(fresh_db, "TARGET")

    features = await compute_feature_importance("TARGET", fid)

    assert len(features) == 2
    assert features[0]["importance_score"] >= features[1]["importance_score"]
    for f in features:
        assert f["importance_score"] >= 0
        assert f["direction"] in ("positive", "negative")


@pytest.mark.asyncio
async def test_feature_importance_stored_in_db(fresh_db):
    """Feature importance rows should be persisted in forecast_explanations."""
    dates = [f"2024-{m:02d}-01" for m in range(1, 13)]
    await _seed_indicators(fresh_db, {
        "A": [(d, float(i)) for i, d in enumerate(dates)],
        "B": [(d, float(i) * 3) for i, d in enumerate(dates)],
    })
    fid = await _seed_forecast(fresh_db, "A")

    features = await compute_feature_importance("A", fid)

    async with aiosqlite.connect(fresh_db) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM forecast_explanations WHERE forecast_id = ?", (fid,)
        )
        rows = await cursor.fetchall()

    assert len(rows) == len(features)
    assert rows[0]["feature_name"] == features[0]["feature_name"]


@pytest.mark.asyncio
async def test_feature_importance_no_other_indicators(fresh_db):
    """When only the target indicator exists, return empty list."""
    dates = [f"2024-{m:02d}-01" for m in range(1, 6)]
    await _seed_indicators(fresh_db, {
        "SOLO": [(d, float(i)) for i, d in enumerate(dates)],
    })
    fid = await _seed_forecast(fresh_db, "SOLO")

    features = await compute_feature_importance("SOLO", fid)
    assert features == []


@pytest.mark.asyncio
async def test_feature_importance_direction(fresh_db):
    """Positive correlation -> 'positive', negative -> 'negative'."""
    dates = [f"2024-{m:02d}-01" for m in range(1, 13)]
    target = [(d, float(i)) for i, d in enumerate(dates)]
    pos = [(d, float(i) * 5) for i, d in enumerate(dates)]
    neg = [(d, float(11 - i) * 5) for i, d in enumerate(dates)]

    await _seed_indicators(fresh_db, {"T": target, "POS": pos, "NEG": neg})
    fid = await _seed_forecast(fresh_db, "T")

    features = await compute_feature_importance("T", fid)
    by_name = {f["feature_name"]: f for f in features}

    assert by_name["POS"]["direction"] == "positive"
    assert by_name["NEG"]["direction"] == "negative"


@pytest.mark.asyncio
async def test_feature_importance_skips_low_overlap(fresh_db):
    """Indicators with fewer than 3 common dates should be skipped."""
    dates_target = [f"2024-{m:02d}-01" for m in range(1, 13)]
    dates_other = ["2025-01-01", "2025-02-01"]  # no overlap

    await _seed_indicators(fresh_db, {
        "X": [(d, float(i)) for i, d in enumerate(dates_target)],
        "Y": [(d, float(i)) for i, d in enumerate(dates_other)],
    })
    fid = await _seed_forecast(fresh_db, "X")

    features = await compute_feature_importance("X", fid)
    assert features == []


# ---- generate_explanation_text tests ----

@pytest.mark.asyncio
async def test_explanation_text_fallback_no_features(fresh_db):
    """Empty features list should produce a simple message."""
    text = await generate_explanation_text("MISSING", [])
    assert "No contributing factors" in text
    assert "MISSING" in text


@pytest.mark.asyncio
async def test_explanation_text_fallback_with_features(fresh_db, monkeypatch):
    """When boto3 is unavailable, the template fallback should be used."""
    monkeypatch.setattr("app._HAS_BOTO3", False)

    dates = [f"2024-{m:02d}-01" for m in range(1, 6)]
    await _seed_indicators(fresh_db, {
        "IND": [(d, float(i)) for i, d in enumerate(dates)],
        "F1": [(d, float(i * 2)) for i, d in enumerate(dates)],
    })

    features = [
        {"feature_name": "F1", "importance_score": 0.95, "direction": "positive"},
    ]
    text = await generate_explanation_text("IND", features)

    assert "IND" in text
    assert "F1" in text
    assert "positively" in text


# ---- store_explanation_text tests ----

@pytest.mark.asyncio
async def test_store_explanation_text(fresh_db):
    """Explanation text should be stored on the first feature row."""
    dates = [f"2024-{m:02d}-01" for m in range(1, 13)]
    await _seed_indicators(fresh_db, {
        "A": [(d, float(i)) for i, d in enumerate(dates)],
        "B": [(d, float(i) * 2) for i, d in enumerate(dates)],
    })
    fid = await _seed_forecast(fresh_db, "A")
    await compute_feature_importance("A", fid)

    await store_explanation_text(fid, "Test explanation")

    async with aiosqlite.connect(fresh_db) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM forecast_explanations WHERE forecast_id = ? ORDER BY id ASC",
            (fid,),
        )
        rows = await cursor.fetchall()

    assert rows[0]["explanation_text"] == "Test explanation"
    # Other rows should remain NULL
    for row in rows[1:]:
        assert row["explanation_text"] is None
