"""Tests for FRED data ingestion (Task 2.1)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as twin_app
import pytest
import pytest_asyncio
import aiosqlite
import httpx
from unittest.mock import AsyncMock, patch


@pytest_asyncio.fixture
async def tmp_db(tmp_path):
    """Override DB_PATH to a temp file, run init_db, yield path."""
    db_path = str(tmp_path / "test.db")
    original = twin_app.DB_PATH
    twin_app.DB_PATH = db_path
    await twin_app.init_db()
    yield db_path
    twin_app.DB_PATH = original


def _make_fred_response(observations):
    """Build a mock FRED API JSON response."""
    return {"observations": observations}


def _mock_response(json_data, status_code=200):
    """Create a mock httpx.Response."""
    resp = httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("GET", "https://api.stlouisfed.org/fred/series/observations"),
    )
    return resp


@pytest.mark.asyncio
async def test_ingest_fred_stores_observations(tmp_db):
    """Valid FRED observations are stored in the indicators table."""
    obs = [
        {"date": "2024-01-01", "value": "308.417"},
        {"date": "2024-02-01", "value": "310.326"},
    ]
    mock_resp = _mock_response(_make_fred_response(obs))

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        result = await twin_app.ingest_fred(["CPIAUCSL"])

    assert result["ingested"] == 2
    assert result["errors"] == []

    async with aiosqlite.connect(tmp_db) as db:
        cursor = await db.execute("SELECT indicator_id, observation_date, value, source FROM indicators")
        rows = await cursor.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "CPIAUCSL"
    assert rows[0][3] == "FRED"


@pytest.mark.asyncio
async def test_ingest_fred_deduplication(tmp_db):
    """Running ingestion twice with same data should not create duplicates."""
    obs = [{"date": "2024-01-01", "value": "308.417"}]
    mock_resp = _mock_response(_make_fred_response(obs))

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        r1 = await twin_app.ingest_fred(["CPIAUCSL"])
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        r2 = await twin_app.ingest_fred(["CPIAUCSL"])

    assert r1["ingested"] == 1
    assert r2["ingested"] == 0  # no new rows on second run

    async with aiosqlite.connect(tmp_db) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM indicators")
        count = (await cursor.fetchone())[0]
    assert count == 1


@pytest.mark.asyncio
async def test_ingest_fred_skips_missing_values(tmp_db):
    """FRED '.' values (missing data) are skipped."""
    obs = [
        {"date": "2024-01-01", "value": "."},
        {"date": "2024-02-01", "value": "310.0"},
    ]
    mock_resp = _mock_response(_make_fred_response(obs))

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        result = await twin_app.ingest_fred(["UNRATE"])

    assert result["ingested"] == 1
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_ingest_fred_http_error(tmp_db):
    """HTTP errors are caught and returned in the errors list."""
    error_resp = _mock_response({"error": "bad key"}, status_code=400)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=error_resp):
        result = await twin_app.ingest_fred(["BADID"])

    assert result["ingested"] == 0
    assert len(result["errors"]) == 1
    assert "BADID" in result["errors"][0]


@pytest.mark.asyncio
async def test_ingest_fred_network_error(tmp_db):
    """Network/request errors are caught and returned in the errors list."""
    with patch(
        "httpx.AsyncClient.get",
        new_callable=AsyncMock,
        side_effect=httpx.ConnectError("connection refused"),
    ):
        result = await twin_app.ingest_fred(["CPIAUCSL"])

    assert result["ingested"] == 0
    assert len(result["errors"]) == 1
    assert "CPIAUCSL" in result["errors"][0]


@pytest.mark.asyncio
async def test_ingest_fred_defaults_to_configured_series(tmp_db):
    """When called with no args, uses DEFAULT_FRED_SERIES."""
    obs = [{"date": "2024-01-01", "value": "100.0"}]
    mock_resp = _mock_response(_make_fred_response(obs))

    call_count = 0

    async def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return mock_resp

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=mock_get):
        result = await twin_app.ingest_fred()

    # Should have called the API once per default series
    assert call_count == len(twin_app.DEFAULT_FRED_SERIES)
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# BEA ingestion tests (Task 2.2)
# ---------------------------------------------------------------------------

def _make_bea_response(data_items):
    """Build a mock BEA API JSON response."""
    return {"BEAAPI": {"Results": {"Data": data_items}}}


def _mock_bea_response(json_data, status_code=200):
    """Create a mock httpx.Response for BEA calls."""
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("GET", "https://apps.bea.gov/api/data/"),
    )


@pytest.mark.asyncio
async def test_ingest_bea_stores_observations(tmp_db):
    """Valid BEA observations are stored in the indicators table."""
    items = [
        {"TimePeriod": "2024Q1", "DataValue": "5.2"},
        {"TimePeriod": "2024Q2", "DataValue": "3.1"},
    ]
    mock_resp = _mock_bea_response(_make_bea_response(items))

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        result = await twin_app.ingest_bea()

    assert result["ingested"] == 2
    assert result["errors"] == []

    async with aiosqlite.connect(tmp_db) as db:
        cursor = await db.execute(
            "SELECT indicator_id, observation_date, value, source FROM indicators ORDER BY observation_date"
        )
        rows = await cursor.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "GDP_GROWTH"
    assert rows[0][1] == "2024-01-01"
    assert rows[0][3] == "BEA"
    assert rows[1][1] == "2024-04-01"


@pytest.mark.asyncio
async def test_ingest_bea_deduplication(tmp_db):
    """Running BEA ingestion twice with same data should not create duplicates."""
    items = [{"TimePeriod": "2024Q1", "DataValue": "5.2"}]
    mock_resp = _mock_bea_response(_make_bea_response(items))

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        r1 = await twin_app.ingest_bea()
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        r2 = await twin_app.ingest_bea()

    assert r1["ingested"] == 1
    assert r2["ingested"] == 0

    async with aiosqlite.connect(tmp_db) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM indicators")
        count = (await cursor.fetchone())[0]
    assert count == 1


@pytest.mark.asyncio
async def test_ingest_bea_skips_non_numeric(tmp_db):
    """Non-numeric DataValue entries are skipped."""
    items = [
        {"TimePeriod": "2024Q1", "DataValue": "N/A"},
        {"TimePeriod": "2024Q2", "DataValue": "3.1"},
    ]
    mock_resp = _mock_bea_response(_make_bea_response(items))

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        result = await twin_app.ingest_bea()

    assert result["ingested"] == 1
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_ingest_bea_handles_commas_in_values(tmp_db):
    """BEA values with commas (e.g., '1,234.5') are parsed correctly."""
    items = [{"TimePeriod": "2024Q1", "DataValue": "1,234.5"}]
    mock_resp = _mock_bea_response(_make_bea_response(items))

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        result = await twin_app.ingest_bea()

    assert result["ingested"] == 1

    async with aiosqlite.connect(tmp_db) as db:
        cursor = await db.execute("SELECT value FROM indicators")
        row = await cursor.fetchone()
    assert row[0] == 1234.5


@pytest.mark.asyncio
async def test_ingest_bea_http_error(tmp_db):
    """HTTP errors are caught and returned in the errors list."""
    error_resp = _mock_bea_response({"error": "bad key"}, status_code=400)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=error_resp):
        result = await twin_app.ingest_bea()

    assert result["ingested"] == 0
    assert len(result["errors"]) == 1
    assert "GDP_GROWTH" in result["errors"][0]


@pytest.mark.asyncio
async def test_ingest_bea_network_error(tmp_db):
    """Network/request errors are caught and returned in the errors list."""
    with patch(
        "httpx.AsyncClient.get",
        new_callable=AsyncMock,
        side_effect=httpx.ConnectError("connection refused"),
    ):
        result = await twin_app.ingest_bea()

    assert result["ingested"] == 0
    assert len(result["errors"]) == 1
    assert "GDP_GROWTH" in result["errors"][0]


@pytest.mark.asyncio
async def test_ingest_bea_malformed_response(tmp_db):
    """Malformed BEA response (missing expected keys) is handled gracefully."""
    bad_payload = {"BEAAPI": {"Results": {}}}  # missing "Data" key
    mock_resp = _mock_bea_response(bad_payload)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        result = await twin_app.ingest_bea()

    assert result["ingested"] == 0
    assert len(result["errors"]) == 1
    assert "parsing" in result["errors"][0].lower() or "GDP_GROWTH" in result["errors"][0]


@pytest.mark.asyncio
async def test_ingest_bea_quarter_mapping(tmp_db):
    """All four quarters map to correct ISO dates."""
    items = [
        {"TimePeriod": "2023Q1", "DataValue": "1.0"},
        {"TimePeriod": "2023Q2", "DataValue": "2.0"},
        {"TimePeriod": "2023Q3", "DataValue": "3.0"},
        {"TimePeriod": "2023Q4", "DataValue": "4.0"},
    ]
    mock_resp = _mock_bea_response(_make_bea_response(items))

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        await twin_app.ingest_bea()

    async with aiosqlite.connect(tmp_db) as db:
        cursor = await db.execute("SELECT observation_date FROM indicators ORDER BY observation_date")
        dates = [r[0] for r in await cursor.fetchall()]

    assert dates == ["2023-01-01", "2023-04-01", "2023-07-01", "2023-10-01"]
