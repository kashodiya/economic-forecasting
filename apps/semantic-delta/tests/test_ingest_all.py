"""Tests for ingest_all() bulk ingestion (Task 2.3)."""
import os
import sys
import pytest
from unittest.mock import AsyncMock, patch

# Ensure the app module is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from app import ingest_all, init_db, SUPPORTED_FRED_SERIES, BEA_CONFIG

TEST_DB = os.path.join(os.path.dirname(__file__), "test_ingest_all.db")


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    """Use a temporary DB for tests."""
    monkeypatch.setattr(app_module, "DB_PATH", TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


@pytest.mark.asyncio
async def test_ingest_all_all_succeed():
    """When all ingest calls succeed, counts reflect full success."""
    await init_db()

    fake_fred = AsyncMock(return_value={"source": "FRED", "rows_inserted": 10})
    fake_bea = AsyncMock(return_value={"source": "BEA", "rows_inserted": 20})

    with patch.object(app_module, "ingest_fred", fake_fred), \
         patch.object(app_module, "ingest_bea", fake_bea):
        result = await ingest_all()

    assert result["fred_success"] == len(SUPPORTED_FRED_SERIES)
    assert result["fred_failed"] == 0
    assert result["bea_success"] == len(BEA_CONFIG)
    assert result["bea_failed"] == 0
    assert result["total_success"] == len(SUPPORTED_FRED_SERIES) + len(BEA_CONFIG)
    assert result["total_failed"] == 0
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_ingest_all_fred_failure_continues():
    """A FRED failure should not stop BEA ingestion."""
    await init_db()

    call_count = 0

    async def flaky_fred(series_id):
        nonlocal call_count
        call_count += 1
        if series_id == "GDP":
            raise RuntimeError("FRED upstream error for GDP")
        return {"source": "FRED", "rows_inserted": 5}

    fake_bea = AsyncMock(return_value={"source": "BEA", "rows_inserted": 20})

    with patch.object(app_module, "ingest_fred", side_effect=flaky_fred), \
         patch.object(app_module, "ingest_bea", fake_bea):
        result = await ingest_all()

    # All FRED series were attempted
    assert call_count == len(SUPPORTED_FRED_SERIES)
    assert result["fred_failed"] == 1
    assert result["fred_success"] == len(SUPPORTED_FRED_SERIES) - 1
    # BEA still ran successfully
    assert result["bea_success"] == len(BEA_CONFIG)
    assert result["bea_failed"] == 0
    # Error recorded
    assert len(result["errors"]) == 1
    assert result["errors"][0]["source"] == "FRED"
    assert result["errors"][0]["series_id"] == "GDP"


@pytest.mark.asyncio
async def test_ingest_all_bea_failure_continues():
    """A BEA failure should be recorded but not stop FRED ingestion."""
    await init_db()

    fake_fred = AsyncMock(return_value={"source": "FRED", "rows_inserted": 10})

    async def failing_bea(**kwargs):
        raise RuntimeError("BEA API down")

    with patch.object(app_module, "ingest_fred", fake_fred), \
         patch.object(app_module, "ingest_bea", side_effect=failing_bea):
        result = await ingest_all()

    assert result["fred_success"] == len(SUPPORTED_FRED_SERIES)
    assert result["fred_failed"] == 0
    assert result["bea_success"] == 0
    assert result["bea_failed"] == len(BEA_CONFIG)
    assert len(result["errors"]) == len(BEA_CONFIG)
    assert all(e["source"] == "BEA" for e in result["errors"])


@pytest.mark.asyncio
async def test_ingest_all_all_fail():
    """When everything fails, counts and errors reflect total failure."""
    await init_db()

    async def fail_fred(series_id):
        raise RuntimeError(f"FRED fail {series_id}")

    async def fail_bea(**kwargs):
        raise RuntimeError("BEA fail")

    with patch.object(app_module, "ingest_fred", side_effect=fail_fred), \
         patch.object(app_module, "ingest_bea", side_effect=fail_bea):
        result = await ingest_all()

    assert result["fred_success"] == 0
    assert result["fred_failed"] == len(SUPPORTED_FRED_SERIES)
    assert result["bea_success"] == 0
    assert result["bea_failed"] == len(BEA_CONFIG)
    assert result["total_success"] == 0
    assert result["total_failed"] == len(SUPPORTED_FRED_SERIES) + len(BEA_CONFIG)
    assert len(result["errors"]) == len(SUPPORTED_FRED_SERIES) + len(BEA_CONFIG)


@pytest.mark.asyncio
async def test_ingest_all_returns_expected_keys():
    """Summary dict should contain all expected keys."""
    await init_db()

    fake_fred = AsyncMock(return_value={})
    fake_bea = AsyncMock(return_value={})

    with patch.object(app_module, "ingest_fred", fake_fred), \
         patch.object(app_module, "ingest_bea", fake_bea):
        result = await ingest_all()

    expected_keys = {
        "fred_success", "fred_failed",
        "bea_success", "bea_failed",
        "total_success", "total_failed",
        "errors",
    }
    assert set(result.keys()) == expected_keys
