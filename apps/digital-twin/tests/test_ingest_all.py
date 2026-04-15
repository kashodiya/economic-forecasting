"""Tests for ingest_all, POST /api/ingest, and GET /api/indicators (Task 2.3)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as twin_app
import pytest
import pytest_asyncio
import aiosqlite
import httpx
from unittest.mock import AsyncMock, patch
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def tmp_db(tmp_path):
    """Override DB_PATH to a temp file, run init_db, yield path."""
    db_path = str(tmp_path / "test.db")
    original = twin_app.DB_PATH
    twin_app.DB_PATH = db_path
    await twin_app.init_db()
    yield db_path
    twin_app.DB_PATH = original


def _mock_fred_response(observations):
    return httpx.Response(
        status_code=200,
        json={"observations": observations},
        request=httpx.Request("GET", "https://api.stlouisfed.org/fred/series/observations"),
    )


def _mock_bea_response(data_items):
    return httpx.Response(
        status_code=200,
        json={"BEAAPI": {"Results": {"Data": data_items}}},
        request=httpx.Request("GET", "https://apps.bea.gov/api/data/"),
    )


# ---------------------------------------------------------------------------
# ingest_all tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_all_returns_both_sources(tmp_db):
    """ingest_all returns a dict with 'fred' and 'bea' keys."""
    fred_resp = _mock_fred_response([{"date": "2024-01-01", "value": "100.0"}])
    bea_resp = _mock_bea_response([{"TimePeriod": "2024Q1", "DataValue": "5.0"}])

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if "stlouisfed" in str(url):
            return fred_resp
        return bea_resp

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=mock_get):
        result = await twin_app.ingest_all()

    assert "fred" in result
    assert "bea" in result
    assert "ingested" in result["fred"]
    assert "errors" in result["fred"]
    assert "ingested" in result["bea"]
    assert "errors" in result["bea"]


@pytest.mark.asyncio
async def test_ingest_all_aggregates_ingested_counts(tmp_db):
    """ingest_all correctly reports ingested counts from both sources."""
    fred_obs = [{"date": "2024-01-01", "value": "100.0"}]
    bea_items = [{"TimePeriod": "2024Q1", "DataValue": "5.0"}]

    async def mock_get(url, **kwargs):
        if "stlouisfed" in str(url):
            return _mock_fred_response(fred_obs)
        return _mock_bea_response(bea_items)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=mock_get):
        result = await twin_app.ingest_all()

    # FRED has 4 default series, each with 1 obs = 4 total
    assert result["fred"]["ingested"] == len(twin_app.DEFAULT_FRED_SERIES)
    assert result["bea"]["ingested"] == 1
    assert result["fred"]["errors"] == []
    assert result["bea"]["errors"] == []


@pytest.mark.asyncio
async def test_ingest_all_propagates_errors(tmp_db):
    """ingest_all propagates errors from individual sources."""
    error_resp = httpx.Response(
        status_code=400,
        json={"error": "bad key"},
        request=httpx.Request("GET", "https://api.stlouisfed.org/fred/series/observations"),
    )

    async def mock_get(url, **kwargs):
        if "stlouisfed" in str(url):
            return error_resp
        return _mock_bea_response([{"TimePeriod": "2024Q1", "DataValue": "5.0"}])

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=mock_get):
        result = await twin_app.ingest_all()

    assert len(result["fred"]["errors"]) > 0
    assert result["bea"]["errors"] == []


# ---------------------------------------------------------------------------
# POST /api/ingest endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_api_ingest_returns_200(tmp_db):
    """POST /api/ingest returns 200 with fred and bea keys."""
    fred_obs = [{"date": "2024-01-01", "value": "100.0"}]
    bea_items = [{"TimePeriod": "2024Q1", "DataValue": "5.0"}]

    async def mock_get(url, **kwargs):
        if "stlouisfed" in str(url):
            return _mock_fred_response(fred_obs)
        return _mock_bea_response(bea_items)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=mock_get):
        async with AsyncClient(
            transport=ASGITransport(app=twin_app.app), base_url="http://test"
        ) as client:
            response = await client.post("/api/ingest")

    assert response.status_code == 200
    body = response.json()
    assert "fred" in body
    assert "bea" in body


@pytest.mark.asyncio
async def test_post_api_ingest_response_structure(tmp_db):
    """POST /api/ingest response has correct nested structure."""
    async def mock_get(url, **kwargs):
        if "stlouisfed" in str(url):
            return _mock_fred_response([{"date": "2024-01-01", "value": "100.0"}])
        return _mock_bea_response([{"TimePeriod": "2024Q1", "DataValue": "5.0"}])

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=mock_get):
        async with AsyncClient(
            transport=ASGITransport(app=twin_app.app), base_url="http://test"
        ) as client:
            response = await client.post("/api/ingest")

    body = response.json()
    assert "ingested" in body["fred"]
    assert "errors" in body["fred"]
    assert "ingested" in body["bea"]
    assert "errors" in body["bea"]


# ---------------------------------------------------------------------------
# GET /api/indicators endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_indicators_empty(tmp_db):
    """GET /api/indicators returns empty list when no data exists."""
    async with AsyncClient(
        transport=ASGITransport(app=twin_app.app), base_url="http://test"
    ) as client:
        response = await client.get("/api/indicators")

    assert response.status_code == 200
    body = response.json()
    assert "observations" in body
    assert body["observations"] == []


@pytest.mark.asyncio
async def test_get_indicators_returns_all(tmp_db):
    """GET /api/indicators returns all stored observations."""
    async with aiosqlite.connect(tmp_db) as db:
        await db.execute(
            "INSERT INTO indicators (indicator_id, observation_date, value, source) VALUES (?, ?, ?, ?)",
            ("CPIAUCSL", "2024-01-01", 308.0, "FRED"),
        )
        await db.execute(
            "INSERT INTO indicators (indicator_id, observation_date, value, source) VALUES (?, ?, ?, ?)",
            ("GDP_GROWTH", "2024-01-01", 5.2, "BEA"),
        )
        await db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=twin_app.app), base_url="http://test"
    ) as client:
        response = await client.get("/api/indicators")

    assert response.status_code == 200
    body = response.json()
    assert len(body["observations"]) == 2


@pytest.mark.asyncio
async def test_get_indicators_filter_by_id(tmp_db):
    """GET /api/indicators?indicator_id=X returns only matching observations."""
    async with aiosqlite.connect(tmp_db) as db:
        await db.execute(
            "INSERT INTO indicators (indicator_id, observation_date, value, source) VALUES (?, ?, ?, ?)",
            ("CPIAUCSL", "2024-01-01", 308.0, "FRED"),
        )
        await db.execute(
            "INSERT INTO indicators (indicator_id, observation_date, value, source) VALUES (?, ?, ?, ?)",
            ("UNRATE", "2024-01-01", 3.7, "FRED"),
        )
        await db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=twin_app.app), base_url="http://test"
    ) as client:
        response = await client.get("/api/indicators?indicator_id=CPIAUCSL")

    assert response.status_code == 200
    body = response.json()
    assert len(body["observations"]) == 1
    assert body["observations"][0]["indicator_id"] == "CPIAUCSL"


@pytest.mark.asyncio
async def test_get_indicators_response_fields(tmp_db):
    """GET /api/indicators observations have required fields."""
    async with aiosqlite.connect(tmp_db) as db:
        await db.execute(
            "INSERT INTO indicators (indicator_id, observation_date, value, source) VALUES (?, ?, ?, ?)",
            ("FEDFUNDS", "2024-03-01", 5.33, "FRED"),
        )
        await db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=twin_app.app), base_url="http://test"
    ) as client:
        response = await client.get("/api/indicators?indicator_id=FEDFUNDS")

    obs = response.json()["observations"][0]
    assert obs["indicator_id"] == "FEDFUNDS"
    assert obs["observation_date"] == "2024-03-01"
    assert obs["value"] == 5.33
    assert obs["source"] == "FRED"


@pytest.mark.asyncio
async def test_get_indicators_unknown_id_returns_empty(tmp_db):
    """GET /api/indicators?indicator_id=UNKNOWN returns empty list (not 404)."""
    async with AsyncClient(
        transport=ASGITransport(app=twin_app.app), base_url="http://test"
    ) as client:
        response = await client.get("/api/indicators?indicator_id=UNKNOWN_XYZ")

    assert response.status_code == 200
    assert response.json()["observations"] == []
