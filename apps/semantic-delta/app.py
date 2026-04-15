# Semantic Delta — FastAPI backend
# Translates BEA/FRED economic data releases into structured forecast updates
# with evidence-linked narratives.

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional, List

import aiosqlite
import httpx
from pydantic import BaseModel
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
BEA_API_KEY = os.environ.get("BEA_API_KEY", "").strip()
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"
)
AWS_PROFILE = os.environ.get("AWS_PROFILE", "").strip() or None

# ---------------------------------------------------------------------------
# FRED Configuration
# ---------------------------------------------------------------------------
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

SUPPORTED_FRED_SERIES = {
    "GDP": {"label": "Gross Domestic Product", "frequency": "quarterly"},
    "CPIAUCSL": {"label": "CPI All Urban Consumers", "frequency": "monthly"},
    "PCE": {"label": "Personal Consumption Expenditures", "frequency": "monthly"},
    "UNRATE": {"label": "Unemployment Rate", "frequency": "monthly"},
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(__file__), "semantic_delta.db")

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id TEXT NOT NULL,
    source TEXT NOT NULL,
    dataset_name TEXT,
    table_name TEXT,
    observation_date TEXT NOT NULL,
    value REAL NOT NULL,
    vintage_date TEXT NOT NULL,
    frequency TEXT DEFAULT 'quarterly',
    ingested_at TEXT DEFAULT (datetime('now')),
    UNIQUE(series_id, observation_date, vintage_date)
);

CREATE TABLE IF NOT EXISTS semantic_deltas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id TEXT NOT NULL,
    vintage_date_new TEXT NOT NULL,
    vintage_date_old TEXT,
    direction TEXT NOT NULL,
    magnitude REAL NOT NULL,
    affected_component TEXT,
    confidence_score REAL DEFAULT 0.0,
    driver_explanation TEXT NOT NULL,
    is_llm_validated INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(series_id, vintage_date_new)
);

CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    indicator_id TEXT NOT NULL,
    forecast_date TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    period_date TEXT NOT NULL,
    point_value REAL NOT NULL,
    upper_bound REAL NOT NULL,
    lower_bound REAL NOT NULL,
    model_type TEXT DEFAULT 'ETS',
    delta_id INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (delta_id) REFERENCES semantic_deltas(id)
);

CREATE TABLE IF NOT EXISTS narratives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delta_id INTEGER,
    forecast_id INTEGER,
    indicator_id TEXT NOT NULL,
    narrative_text TEXT NOT NULL,
    evidence_links_json TEXT NOT NULL,
    is_scenario INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (delta_id) REFERENCES semantic_deltas(id),
    FOREIGN KEY (forecast_id) REFERENCES forecasts(id)
);
"""


async def init_db():
    """Create all tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_CREATE_TABLES_SQL)
        await db.commit()


# ---------------------------------------------------------------------------
# Release Reader — FRED Ingestion
# ---------------------------------------------------------------------------
async def ingest_fred(series_id: str) -> dict:
    """Fetch FRED observations for *series_id*, store with vintage date.

    Returns a summary dict on success.  Raises ``fastapi.HTTPException``
    with status 502 for upstream FRED failures.
    """
    from fastapi import HTTPException

    if series_id not in SUPPORTED_FRED_SERIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported FRED series '{series_id}'. "
                   f"Supported: {', '.join(sorted(SUPPORTED_FRED_SERIES))}",
        )

    if not FRED_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="FRED_API_KEY is not configured.",
        )

    vintage = date.today().isoformat()  # e.g. "2025-07-14"
    meta = SUPPORTED_FRED_SERIES[series_id]

    # --- Fetch from FRED API ---
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                FRED_BASE_URL,
                params={
                    "series_id": series_id,
                    "api_key": FRED_API_KEY,
                    "file_type": "json",
                },
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.warning("FRED HTTP %s for %s: %s", status, series_id, exc)
        raise HTTPException(
            status_code=502,
            detail=f"FRED upstream error (HTTP {status}) for series '{series_id}': "
                   f"{exc.response.text[:200]}",
        )
    except httpx.RequestError as exc:
        logger.warning("FRED request error for %s: %s", series_id, exc)
        raise HTTPException(
            status_code=502,
            detail=f"FRED request failed for series '{series_id}': {exc}",
        )

    # --- Parse response ---
    try:
        data = resp.json()
        observations = data.get("observations", [])
    except Exception as exc:
        logger.warning("FRED JSON parse error for %s: %s", series_id, exc)
        raise HTTPException(
            status_code=502,
            detail=f"FRED returned invalid JSON for series '{series_id}': {exc}",
        )

    # --- Build rows ---
    rows: list[tuple] = []
    for obs in observations:
        val_str = obs.get("value", ".")
        if val_str in (".", ""):
            continue  # FRED uses "." for missing values
        try:
            value = float(val_str)
        except (ValueError, TypeError):
            continue
        rows.append((
            series_id,
            "FRED",
            obs["date"],
            value,
            vintage,
            meta["frequency"],
        ))

    if not rows:
        raise HTTPException(
            status_code=502,
            detail=f"FRED returned no valid observations for series '{series_id}'.",
        )

    # --- Persist with INSERT OR IGNORE to preserve prior vintages ---
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT OR IGNORE INTO observations
               (series_id, source, observation_date, value, vintage_date, frequency)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
        await db.commit()
        inserted = db.total_changes

    logger.info(
        "FRED ingest %s: %d observations fetched, %d new rows inserted (vintage %s)",
        series_id, len(rows), inserted, vintage,
    )

    return {
        "source": "FRED",
        "series_id": series_id,
        "label": meta["label"],
        "vintage_date": vintage,
        "observations_fetched": len(rows),
        "rows_inserted": inserted,
    }


# ---------------------------------------------------------------------------
# Release Reader — BEA Ingestion
# ---------------------------------------------------------------------------
BEA_BASE_URL = "https://apps.bea.gov/api/data"

BEA_CONFIG = {
    "NIPA": {
        "table": "T10101",
        "label": "GDP and Components (NIPA Table 1.1.1)",
        "frequency": "Q",
    },
}

_QUARTER_TO_MONTH = {"Q1": "01", "Q2": "04", "Q3": "07", "Q4": "10"}


def _bea_period_to_iso(period: str) -> Optional[str]:
    """Convert a BEA quarterly period like '2024Q1' to ISO date '2024-01-01'.

    Returns None if the period string cannot be parsed.
    """
    period = period.strip()
    if len(period) != 6 or period[4] != "Q":
        return None
    year = period[:4]
    quarter = period[4:]
    month = _QUARTER_TO_MONTH.get(quarter)
    if month is None or not year.isdigit():
        return None
    return f"{year}-{month}-01"


async def ingest_bea(
    dataset_name: str = "NIPA",
    table_name: str = "T10101",
    series_id: Optional[str] = None,
) -> dict:
    """Fetch BEA data for *dataset_name*/*table_name*, store with vintage date.

    If *series_id* is provided, only rows matching that SeriesCode are kept.
    Returns a summary dict on success.  Raises ``fastapi.HTTPException``
    with status 502 for upstream BEA failures.
    """
    from fastapi import HTTPException

    if not BEA_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="BEA_API_KEY is not configured.",
        )

    vintage = date.today().isoformat()
    cfg = BEA_CONFIG.get(dataset_name)
    label = cfg["label"] if cfg else f"{dataset_name}/{table_name}"

    # --- Fetch from BEA API ---
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                BEA_BASE_URL,
                params={
                    "UserID": BEA_API_KEY,
                    "method": "GetData",
                    "DataSetName": dataset_name,
                    "TableName": table_name,
                    "Frequency": "Q",
                    "Year": "ALL",
                    "ResultFormat": "JSON",
                },
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.warning("BEA HTTP %s for %s/%s: %s", status, dataset_name, table_name, exc)
        raise HTTPException(
            status_code=502,
            detail=f"BEA upstream error (HTTP {status}) for "
                   f"{dataset_name}/{table_name}: {exc.response.text[:200]}",
        )
    except httpx.RequestError as exc:
        logger.warning("BEA request error for %s/%s: %s", dataset_name, table_name, exc)
        raise HTTPException(
            status_code=502,
            detail=f"BEA request failed for {dataset_name}/{table_name}: {exc}",
        )

    # --- Parse response ---
    try:
        data = resp.json()
        results = (
            data.get("BEAAPI", {})
            .get("Results", {})
            .get("Data", [])
        )
    except Exception as exc:
        logger.warning("BEA JSON parse error for %s/%s: %s", dataset_name, table_name, exc)
        raise HTTPException(
            status_code=502,
            detail=f"BEA returned invalid JSON for {dataset_name}/{table_name}: {exc}",
        )

    if not results:
        raise HTTPException(
            status_code=502,
            detail=f"BEA returned no data for {dataset_name}/{table_name}.",
        )

    # --- Build rows ---
    rows: list[tuple] = []
    for item in results:
        period = item.get("TimePeriod", "")
        iso_date = _bea_period_to_iso(period)
        if iso_date is None:
            continue

        val_str = str(item.get("DataValue", "")).replace(",", "").strip()
        if not val_str or val_str in (".", "—", ""):
            continue
        try:
            value = float(val_str)
        except (ValueError, TypeError):
            continue

        series_code = item.get("SeriesCode", "")
        if series_id and series_code != series_id:
            continue  # filter to requested series only

        row_id = f"BEA_{table_name}_{series_code}" if series_code else f"BEA_{table_name}"

        rows.append((
            row_id,
            "BEA",
            dataset_name,
            table_name,
            iso_date,
            value,
            vintage,
            "quarterly",
        ))

    if not rows:
        raise HTTPException(
            status_code=502,
            detail=f"BEA returned no valid observations for {dataset_name}/{table_name}.",
        )

    # --- Persist with INSERT OR IGNORE to preserve prior vintages ---
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT OR IGNORE INTO observations
               (series_id, source, dataset_name, table_name,
                observation_date, value, vintage_date, frequency)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        await db.commit()
        inserted = db.total_changes

    logger.info(
        "BEA ingest %s/%s: %d observations fetched, %d new rows inserted (vintage %s)",
        dataset_name, table_name, len(rows), inserted, vintage,
    )

    return {
        "source": "BEA",
        "dataset_name": dataset_name,
        "table_name": table_name,
        "label": label,
        "vintage_date": vintage,
        "observations_fetched": len(rows),
        "rows_inserted": inserted,
    }


# ---------------------------------------------------------------------------
# Release Reader — Bulk Ingestion
# ---------------------------------------------------------------------------
async def ingest_all() -> dict:
    """Ingest all configured FRED and BEA indicators.

    Catches individual failures so one bad series doesn't stop the pipeline.
    Returns a summary dict with success/failure counts and error details.
    """
    fred_success = 0
    fred_failed = 0
    bea_success = 0
    bea_failed = 0
    errors: list[dict] = []

    # --- FRED series ---
    for series_id in SUPPORTED_FRED_SERIES:
        try:
            await ingest_fred(series_id)
            fred_success += 1
        except Exception as exc:
            fred_failed += 1
            logger.warning("ingest_all: FRED %s failed: %s", series_id, exc)
            errors.append({
                "source": "FRED",
                "series_id": series_id,
                "error": str(exc),
            })

    # --- BEA datasets ---
    for dataset_name, cfg in BEA_CONFIG.items():
        try:
            await ingest_bea(dataset_name=dataset_name, table_name=cfg["table"])
            bea_success += 1
        except Exception as exc:
            bea_failed += 1
            logger.warning(
                "ingest_all: BEA %s/%s failed: %s",
                dataset_name, cfg["table"], exc,
            )
            errors.append({
                "source": "BEA",
                "dataset_name": dataset_name,
                "table_name": cfg["table"],
                "error": str(exc),
            })

    summary = {
        "fred_success": fred_success,
        "fred_failed": fred_failed,
        "bea_success": bea_success,
        "bea_failed": bea_failed,
        "total_success": fred_success + bea_success,
        "total_failed": fred_failed + bea_failed,
        "errors": errors,
    }

    logger.info(
        "ingest_all complete: FRED %d/%d ok, BEA %d/%d ok",
        fred_success, fred_success + fred_failed,
        bea_success, bea_success + bea_failed,
    )

    return summary


# ---------------------------------------------------------------------------
# Delta Engine — Numeric Computation
# ---------------------------------------------------------------------------
try:
    import numpy as _np

    def _mean(values: list[float]) -> float:
        return float(_np.mean(values))
except ImportError:
    _np = None  # type: ignore[assignment]

    def _mean(values: list[float]) -> float:
        return sum(values) / len(values)


def _numeric_delta(
    current_values: list[float],
    prior_values: list[float],
) -> dict:
    """Pure function: compute magnitude, direction, and affected periods.

    Parameters
    ----------
    current_values : list[float]
        Observation values from the current (newer) vintage.
    prior_values : list[float]
        Observation values from the prior (older) vintage.

    Returns
    -------
    dict with keys:
        direction       – "up", "down", or "unchanged"
        magnitude       – absolute difference of means
        current_mean    – mean of *current_values*
        prior_mean      – mean of *prior_values*
        periods_affected – count of periods where values differ
    """
    current_mean = _mean(current_values)
    prior_mean = _mean(prior_values)

    if current_mean > prior_mean:
        direction = "up"
    elif current_mean < prior_mean:
        direction = "down"
    else:
        direction = "unchanged"

    magnitude = abs(current_mean - prior_mean)

    # Count periods where the value changed between vintages.
    min_len = min(len(current_values), len(prior_values))
    periods_affected = sum(
        1 for i in range(min_len) if current_values[i] != prior_values[i]
    )
    # Extra periods in the longer list are always "affected" (new or removed).
    periods_affected += abs(len(current_values) - len(prior_values))

    return {
        "direction": direction,
        "magnitude": magnitude,
        "current_mean": current_mean,
        "prior_mean": prior_mean,
        "periods_affected": periods_affected,
    }


# ---------------------------------------------------------------------------
# Delta Engine — LLM Driver Explanation
# ---------------------------------------------------------------------------

def _template_explanation(delta: dict) -> str:
    """Fallback template-based explanation from numeric delta values.

    Parameters
    ----------
    delta : dict
        Output of ``_numeric_delta`` (must contain *direction*, *magnitude*,
        *prior_mean*, *current_mean*, *periods_affected*).

    Returns
    -------
    str  – A human-readable sentence describing the change.
    """
    direction = delta.get("direction", "unchanged")
    magnitude = delta.get("magnitude", 0.0)
    prior_mean = delta.get("prior_mean", 0.0)
    current_mean = delta.get("current_mean", 0.0)
    periods = delta.get("periods_affected", 0)

    if direction == "unchanged":
        return (
            f"The indicator remained unchanged at a mean of "
            f"{current_mean:.1f}, affecting {periods} periods."
        )

    verb = "moved up" if direction == "up" else "moved down"
    return (
        f"The indicator {verb} by {magnitude:.1f} "
        f"(from mean {prior_mean:.1f} to {current_mean:.1f}), "
        f"affecting {periods} periods."
    )


# Words that contradict an "up" direction
_DOWN_WORDS = {"decreased", "declined", "fell", "dropped"}
# Words that contradict a "down" direction
_UP_WORDS = {"increased", "rose", "grew", "surged"}


def _validate_llm_explanation(
    explanation: str,
    numeric_delta: dict,
) -> tuple[bool, str]:
    """Check that *explanation* does not contradict *numeric_delta*.

    Validation rules
    ----------------
    * If direction is ``"up"``, the explanation must not contain any of the
      *_DOWN_WORDS* (decreased, declined, fell, dropped).
    * If direction is ``"down"``, the explanation must not contain any of the
      *_UP_WORDS* (increased, rose, grew, surged).

    Returns
    -------
    tuple[bool, str]
        ``(is_valid, final_explanation)`` — when invalid the template
        explanation is returned instead.
    """
    direction = numeric_delta.get("direction", "unchanged")
    lower = explanation.lower()

    if direction == "up":
        for word in _DOWN_WORDS:
            if word in lower:
                return False, _template_explanation(numeric_delta)
    elif direction == "down":
        for word in _UP_WORDS:
            if word in lower:
                return False, _template_explanation(numeric_delta)

    return True, explanation


async def _generate_driver_explanation(
    delta: dict,
    series_id: str,
) -> tuple[str, bool]:
    """Call AWS Bedrock Claude to explain the economic reason for a change.

    Parameters
    ----------
    delta : dict
        Output of ``_numeric_delta``.
    series_id : str
        The economic indicator identifier (e.g. ``"GDP"``).

    Returns
    -------
    tuple[str, bool]
        ``(explanation, is_llm_validated)`` — on any failure the template
        explanation is returned with ``is_llm_validated=False``.
    """
    import json as _json

    try:
        import boto3
    except ImportError:
        logger.warning("boto3 not installed — falling back to template explanation")
        return _template_explanation(delta), False

    direction = delta.get("direction", "unchanged")
    magnitude = delta.get("magnitude", 0.0)
    prior_mean = delta.get("prior_mean", 0.0)
    current_mean = delta.get("current_mean", 0.0)
    periods = delta.get("periods_affected", 0)

    prompt = (
        f"You are an economic analyst. The indicator '{series_id}' has "
        f"{direction} by {magnitude:.2f} (from mean {prior_mean:.2f} to "
        f"{current_mean:.2f}), affecting {periods} periods. "
        f"In 2-3 sentences, explain the likely economic reason for this "
        f"change in '{series_id}'. Be specific and grounded in economic "
        f"fundamentals."
    )

    try:
        session_kwargs: dict = {"region_name": AWS_REGION}
        if AWS_PROFILE:
            session_kwargs["profile_name"] = AWS_PROFILE
        session = boto3.Session(**session_kwargs)
        client = session.client("bedrock-runtime")

        body = _json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": prompt}],
        })

        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        result = _json.loads(response["body"].read())
        explanation = result["content"][0]["text"].strip()

        # Validate the LLM output against the numeric delta
        is_valid, final_explanation = _validate_llm_explanation(
            explanation, delta,
        )
        return final_explanation, is_valid

    except Exception as exc:
        logger.warning(
            "Bedrock call failed for %s — falling back to template: %s",
            series_id, exc,
        )
        return _template_explanation(delta), False


# ---------------------------------------------------------------------------
# Delta Engine — Compute & Persist
# ---------------------------------------------------------------------------

async def compute_delta(series_id: str) -> Optional[dict]:
    """Compare the latest two vintages for *series_id* and persist a semantic delta.

    * If only one vintage exists, creates a baseline "initial" delta whose
      magnitude equals the mean of that vintage's values.
    * If two or more vintages exist, computes the numeric delta between the
      two most recent vintages, generates a driver explanation (LLM with
      template fallback), and persists the result.

    Returns the delta dict on success, or ``None`` if no observations exist.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Distinct vintage dates for this series, newest first
        cursor = await db.execute(
            "SELECT DISTINCT vintage_date FROM observations "
            "WHERE series_id = ? ORDER BY vintage_date DESC",
            (series_id,),
        )
        rows = await cursor.fetchall()
        vintage_dates = [r["vintage_date"] for r in rows]

        if not vintage_dates:
            return None

        newest_vintage = vintage_dates[0]

        if len(vintage_dates) == 1:
            # --- Baseline / initial delta ---
            cursor = await db.execute(
                "SELECT value FROM observations "
                "WHERE series_id = ? AND vintage_date = ? "
                "ORDER BY observation_date",
                (series_id, newest_vintage),
            )
            vals = [r["value"] for r in await cursor.fetchall()]
            if not vals:
                return None

            magnitude = _mean(vals)
            explanation = (
                f"Initial baseline for '{series_id}' with mean value "
                f"{magnitude:.2f} across {len(vals)} observations."
            )

            await db.execute(
                """INSERT OR IGNORE INTO semantic_deltas
                   (series_id, vintage_date_new, vintage_date_old,
                    direction, magnitude, affected_component,
                    confidence_score, driver_explanation, is_llm_validated)
                   VALUES (?, ?, NULL, 'initial', ?, NULL, 0.0, ?, 0)""",
                (series_id, newest_vintage, magnitude, explanation),
            )
            await db.commit()

            return {
                "series_id": series_id,
                "vintage_date_new": newest_vintage,
                "vintage_date_old": None,
                "direction": "initial",
                "magnitude": magnitude,
                "affected_component": None,
                "confidence_score": 0.0,
                "driver_explanation": explanation,
                "is_llm_validated": False,
            }

        # --- Two or more vintages: compute delta between latest two ---
        prior_vintage = vintage_dates[1]

        cursor = await db.execute(
            "SELECT value FROM observations "
            "WHERE series_id = ? AND vintage_date = ? "
            "ORDER BY observation_date",
            (series_id, newest_vintage),
        )
        current_values = [r["value"] for r in await cursor.fetchall()]

        cursor = await db.execute(
            "SELECT value FROM observations "
            "WHERE series_id = ? AND vintage_date = ? "
            "ORDER BY observation_date",
            (series_id, prior_vintage),
        )
        prior_values = [r["value"] for r in await cursor.fetchall()]

    if not current_values or not prior_values:
        return None

    # Numeric delta (pure function)
    delta = _numeric_delta(current_values, prior_values)

    # LLM (or template) explanation
    explanation, is_llm_validated = await _generate_driver_explanation(
        delta, series_id,
    )

    # Persist
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR IGNORE INTO semantic_deltas
               (series_id, vintage_date_new, vintage_date_old,
                direction, magnitude, affected_component,
                confidence_score, driver_explanation, is_llm_validated)
               VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
            (
                series_id,
                newest_vintage,
                prior_vintage,
                delta["direction"],
                delta["magnitude"],
                delta.get("confidence_score", 0.0),
                explanation,
                1 if is_llm_validated else 0,
            ),
        )
        await db.commit()

    return {
        "series_id": series_id,
        "vintage_date_new": newest_vintage,
        "vintage_date_old": prior_vintage,
        "direction": delta["direction"],
        "magnitude": delta["magnitude"],
        "affected_component": None,
        "confidence_score": delta.get("confidence_score", 0.0),
        "driver_explanation": explanation,
        "is_llm_validated": is_llm_validated,
    }


async def compute_all_deltas() -> list[dict]:
    """Compute semantic deltas for every distinct series_id in the observations table.

    Individual failures are caught and logged so one bad series doesn't stop
    the rest.  Returns a list of computed delta dicts (``None`` results are
    excluded) plus an ``"_errors"`` key in the last element if any failures
    occurred.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT DISTINCT series_id FROM observations"
        )
        rows = await cursor.fetchall()
        series_ids = [r[0] for r in rows]

    results: list[dict] = []
    errors: list[dict] = []

    for sid in series_ids:
        try:
            delta = await compute_delta(sid)
            if delta is not None:
                results.append(delta)
        except Exception as exc:
            logger.warning("compute_all_deltas: %s failed: %s", sid, exc)
            errors.append({"series_id": sid, "error": str(exc)})

    if errors:
        results.append({"_errors": errors})

    return results


# ---------------------------------------------------------------------------
# Forecast Engine
# ---------------------------------------------------------------------------
import math
from datetime import datetime
from dateutil.relativedelta import relativedelta


def _detect_frequency(series_id: str) -> str:
    """Return 'monthly' or 'quarterly' for a known series, default 'monthly'."""
    meta = SUPPORTED_FRED_SERIES.get(series_id)
    if meta:
        return meta.get("frequency", "monthly")
    # BEA series are typically quarterly
    if series_id.startswith("BEA_"):
        return "quarterly"
    return "monthly"


def _next_period(last_date: datetime, frequency: str, steps: int) -> str:
    """Compute the ISO date string *steps* periods ahead of *last_date*."""
    if frequency == "quarterly":
        new_date = last_date + relativedelta(months=3 * steps)
    else:  # monthly
        new_date = last_date + relativedelta(months=steps)
    return new_date.strftime("%Y-%m-%d")


async def generate_forecast(
    indicator_id: str,
    horizon: int = 6,
    delta_id: Optional[int] = None,
) -> dict:
    """Generate a forecast for *indicator_id* using Holt-Winters ETS.

    Steps:
      1. Query observations ordered by date.
      2. If fewer than 12 observations, raise HTTPException(400).
      3. Fit Holt-Winters ExponentialSmoothing (additive trend, no seasonal).
      4. Generate point forecasts for *horizon* periods ahead.
      5. Compute upper/lower bounds using residual std (point ± 1.96 * std).
      6. Persist each forecast period to the forecasts table.
      7. Return a ForecastResult dict.

    Falls back to simple linear trend if ETS fitting fails.
    """
    from fastapi import HTTPException
    import numpy as np

    # ------------------------------------------------------------------
    # 1. Read historical observations
    # ------------------------------------------------------------------
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT observation_date, value FROM observations "
            "WHERE series_id = ? ORDER BY observation_date",
            (indicator_id,),
        )
        rows = await cursor.fetchall()

    # ------------------------------------------------------------------
    # 2. Check minimum observation count
    # ------------------------------------------------------------------
    if len(rows) < 12:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Insufficient data for indicator '{indicator_id}': "
                f"{len(rows)} observations available, at least 12 required "
                f"for forecast generation."
            ),
        )

    dates = [r["observation_date"] for r in rows]
    values = np.array([r["value"] for r in rows], dtype=float)
    frequency = _detect_frequency(indicator_id)
    last_date = datetime.strptime(dates[-1], "%Y-%m-%d")
    forecast_date = date.today().isoformat()

    # ------------------------------------------------------------------
    # 3. Fit model (Holt-Winters ETS with linear-trend fallback)
    # ------------------------------------------------------------------
    model_type = "ETS"
    residual_std: float = 0.0
    forecast_values = None

    try:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing

        ets_model = ExponentialSmoothing(
            values,
            trend="add",
            seasonal=None,
        )
        fit = ets_model.fit(optimized=True)

        # Residual standard deviation from in-sample residuals
        residual_std = float(np.nanstd(fit.resid))

        # Point forecasts
        forecast_values = fit.forecast(horizon)

    except Exception as exc:
        logger.warning(
            "ETS failed for %s, falling back to linear trend: %s",
            indicator_id, exc,
        )
        model_type = "LinearTrend"

        # Simple linear trend via numpy polyfit
        x = np.arange(len(values), dtype=float)
        coeffs = np.polyfit(x, values, 1)
        x_forecast = np.arange(len(values), len(values) + horizon, dtype=float)
        forecast_values = np.polyval(coeffs, x_forecast)

        # Residual std from fitted values
        fitted = np.polyval(coeffs, x)
        residual_std = float(np.std(values - fitted))

    # ------------------------------------------------------------------
    # 4-5. Build forecast periods with confidence bounds
    # ------------------------------------------------------------------
    if residual_std == 0:
        residual_std = 1e-6  # avoid zero-width intervals

    periods: list[dict] = []
    for step_idx in range(horizon):
        point = float(forecast_values[step_idx])
        # Widen uncertainty with sqrt of step for fan effect
        sigma = residual_std * math.sqrt(step_idx + 1)
        upper = point + 1.96 * sigma
        lower = point - 1.96 * sigma
        period_date = _next_period(last_date, frequency, step_idx + 1)
        periods.append({
            "period_date": period_date,
            "point_value": round(point, 4),
            "upper_bound": round(upper, 4),
            "lower_bound": round(lower, 4),
        })

    # ------------------------------------------------------------------
    # 6. Persist forecast periods to DB
    # ------------------------------------------------------------------
    async with aiosqlite.connect(DB_PATH) as db:
        for idx, p in enumerate(periods):
            await db.execute(
                """INSERT INTO forecasts
                   (indicator_id, forecast_date, horizon, period_date,
                    point_value, upper_bound, lower_bound,
                    model_type, delta_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    indicator_id,
                    forecast_date,
                    idx + 1,
                    p["period_date"],
                    p["point_value"],
                    p["upper_bound"],
                    p["lower_bound"],
                    model_type,
                    delta_id,
                ),
            )
        await db.commit()

    # ------------------------------------------------------------------
    # 7. Return ForecastResult
    # ------------------------------------------------------------------
    return {
        "indicator_id": indicator_id,
        "forecast_date": forecast_date,
        "periods": periods,
        "model_type": model_type,
        "delta_id": delta_id,
    }


async def apply_delta_shock(indicator_id: str, delta: dict) -> dict:
    """Update existing forecast by incorporating *delta* as a shock.

    Parameters
    ----------
    indicator_id : str
        The economic indicator to update.
    delta : dict
        Must contain at least ``direction`` ("up" or "down") and
        ``magnitude`` (positive float).  May also contain ``id`` to
        record which semantic delta triggered the update.

    Behaviour
    ---------
    * Fetches the latest forecast for *indicator_id* from the DB.
    * If no forecast exists, generates one first via ``generate_forecast``.
    * For "up" direction: adds *magnitude* to each point_value.
    * For "down" direction: subtracts *magnitude* from each point_value.
    * Adjusts upper/lower bounds proportionally (same additive shift).
    * Persists the updated forecast as new rows (with the delta_id).
    * Returns the updated ForecastResult dict.
    """
    direction = delta.get("direction", "unchanged")
    magnitude = float(delta.get("magnitude", 0.0))
    delta_id = delta.get("id")

    # ------------------------------------------------------------------
    # 1. Get the latest forecast for this indicator
    # ------------------------------------------------------------------
    existing_periods: list[dict] = []
    existing_model_type = "ETS"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Find the most recent forecast_date for this indicator
        cursor = await db.execute(
            "SELECT forecast_date, model_type FROM forecasts "
            "WHERE indicator_id = ? ORDER BY created_at DESC LIMIT 1",
            (indicator_id,),
        )
        row = await cursor.fetchone()

        if row:
            latest_forecast_date = row["forecast_date"]
            existing_model_type = row["model_type"]
            cursor = await db.execute(
                "SELECT period_date, point_value, upper_bound, lower_bound "
                "FROM forecasts "
                "WHERE indicator_id = ? AND forecast_date = ? "
                "ORDER BY horizon",
                (indicator_id, latest_forecast_date),
            )
            rows = await cursor.fetchall()
            existing_periods = [
                {
                    "period_date": r["period_date"],
                    "point_value": r["point_value"],
                    "upper_bound": r["upper_bound"],
                    "lower_bound": r["lower_bound"],
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # 2. If no existing forecast, generate one first
    # ------------------------------------------------------------------
    if not existing_periods:
        base = await generate_forecast(indicator_id)
        existing_periods = base["periods"]
        existing_model_type = base["model_type"]

    # ------------------------------------------------------------------
    # 3. Apply the shock
    # ------------------------------------------------------------------
    shift = magnitude if direction == "up" else -magnitude

    updated_periods: list[dict] = []
    for p in existing_periods:
        updated_periods.append({
            "period_date": p["period_date"],
            "point_value": round(p["point_value"] + shift, 4),
            "upper_bound": round(p["upper_bound"] + shift, 4),
            "lower_bound": round(p["lower_bound"] + shift, 4),
        })

    # ------------------------------------------------------------------
    # 4. Persist updated forecast as new rows
    # ------------------------------------------------------------------
    forecast_date = date.today().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        for idx, p in enumerate(updated_periods):
            await db.execute(
                """INSERT INTO forecasts
                   (indicator_id, forecast_date, horizon, period_date,
                    point_value, upper_bound, lower_bound,
                    model_type, delta_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    indicator_id,
                    forecast_date,
                    idx + 1,
                    p["period_date"],
                    p["point_value"],
                    p["upper_bound"],
                    p["lower_bound"],
                    existing_model_type,
                    delta_id,
                ),
            )
        await db.commit()

    # ------------------------------------------------------------------
    # 5. Return updated ForecastResult
    # ------------------------------------------------------------------
    return {
        "indicator_id": indicator_id,
        "forecast_date": forecast_date,
        "periods": updated_periods,
        "model_type": existing_model_type,
        "delta_id": delta_id,
    }


async def compute_whatif(
    indicator_id: str,
    shock_magnitude: float,
    shock_direction: str,
) -> dict:
    """Compute a hypothetical forecast WITHOUT persisting to DB.

    Parameters
    ----------
    indicator_id : str
        The economic indicator to run the scenario on.
    shock_magnitude : float
        Size of the hypothetical shock (positive value).
    shock_direction : str
        ``"up"`` or ``"down"``.

    Returns
    -------
    dict
        A ForecastResult-like dict with an additional ``"is_scenario": True``
        flag.  The result is NOT persisted to the database.
    """
    # ------------------------------------------------------------------
    # 1. Get the latest forecast (or generate one)
    # ------------------------------------------------------------------
    existing_periods: list[dict] = []
    existing_model_type = "ETS"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT forecast_date, model_type FROM forecasts "
            "WHERE indicator_id = ? ORDER BY created_at DESC LIMIT 1",
            (indicator_id,),
        )
        row = await cursor.fetchone()

        if row:
            latest_forecast_date = row["forecast_date"]
            existing_model_type = row["model_type"]
            cursor = await db.execute(
                "SELECT period_date, point_value, upper_bound, lower_bound "
                "FROM forecasts "
                "WHERE indicator_id = ? AND forecast_date = ? "
                "ORDER BY horizon",
                (indicator_id, latest_forecast_date),
            )
            rows = await cursor.fetchall()
            existing_periods = [
                {
                    "period_date": r["period_date"],
                    "point_value": r["point_value"],
                    "upper_bound": r["upper_bound"],
                    "lower_bound": r["lower_bound"],
                }
                for r in rows
            ]

    if not existing_periods:
        base = await generate_forecast(indicator_id)
        existing_periods = base["periods"]
        existing_model_type = base["model_type"]

    # ------------------------------------------------------------------
    # 2. Apply the shock (same logic as apply_delta_shock)
    # ------------------------------------------------------------------
    shift = shock_magnitude if shock_direction == "up" else -shock_magnitude

    scenario_periods: list[dict] = []
    for p in existing_periods:
        scenario_periods.append({
            "period_date": p["period_date"],
            "point_value": round(p["point_value"] + shift, 4),
            "upper_bound": round(p["upper_bound"] + shift, 4),
            "lower_bound": round(p["lower_bound"] + shift, 4),
        })

    # ------------------------------------------------------------------
    # 3. Return result — NOT persisted to DB
    # ------------------------------------------------------------------
    return {
        "indicator_id": indicator_id,
        "forecast_date": date.today().isoformat(),
        "periods": scenario_periods,
        "model_type": existing_model_type,
        "delta_id": None,
        "is_scenario": True,
    }


# ---------------------------------------------------------------------------
# Narrative Generator
# ---------------------------------------------------------------------------
import json as _json_mod


def _format_evidence_link(source: str, series_id: str, date: str, value: float) -> str:
    """Format a single evidence link as an inline citation string.

    Returns a string like ``[FRED:GDP, 2024-06-01, 105.0]``.
    """
    return f"[{source}:{series_id}, {date}, {value}]"


def _extract_evidence_links(narrative_text: str, delta: dict) -> list[dict]:
    """Construct evidence links from the delta data.

    Each link is a dict with: source, series_id, date, value, label.
    At minimum, creates one evidence link from the delta's series_id
    and vintage data.
    """
    series_id = delta.get("series_id", "UNKNOWN")
    vintage_new = delta.get("vintage_date_new", "")
    magnitude = delta.get("magnitude", 0.0)
    direction = delta.get("direction", "unchanged")

    # Determine source from series_id prefix
    source = "BEA" if series_id.startswith("BEA_") else "FRED"

    # Look up a human-readable label
    meta = SUPPORTED_FRED_SERIES.get(series_id)
    label = meta["label"] if meta else series_id

    links = [
        {
            "source": source,
            "series_id": series_id,
            "date": vintage_new,
            "value": magnitude,
            "label": f"{label}, {vintage_new}",
        }
    ]

    # If there's an old vintage, add a link for the prior value too
    vintage_old = delta.get("vintage_date_old")
    if vintage_old:
        prior_mean = delta.get("prior_mean", delta.get("magnitude", 0.0))
        links.append({
            "source": source,
            "series_id": series_id,
            "date": vintage_old,
            "value": prior_mean,
            "label": f"{label}, {vintage_old} (prior)",
        })

    return links


def _build_narrative_prompt(delta: dict, forecast: dict, series_meta: dict) -> str:
    """Build an LLM prompt asking Claude to explain the forecast change
    with evidence citations.
    """
    series_id = delta.get("series_id", "UNKNOWN")
    direction = delta.get("direction", "unchanged")
    magnitude = delta.get("magnitude", 0.0)
    explanation = delta.get("driver_explanation", "")
    label = series_meta.get("label", series_id)
    vintage_new = delta.get("vintage_date_new", "")

    # Summarise forecast periods
    periods = forecast.get("periods", [])
    period_summary = ""
    if periods:
        points = [f"{p['period_date']}: {p['point_value']}" for p in periods[:3]]
        period_summary = ", ".join(points)
        if len(periods) > 3:
            period_summary += f" (and {len(periods) - 3} more periods)"

    source = "BEA" if series_id.startswith("BEA_") else "FRED"
    citation = _format_evidence_link(source, series_id, vintage_new, magnitude)

    return (
        f"You are an economic analyst writing a brief narrative for a dashboard.\n\n"
        f"Indicator: {label} ({series_id})\n"
        f"Data source: {source}\n"
        f"Semantic delta: direction={direction}, magnitude={magnitude:.4f}\n"
        f"Driver explanation: {explanation}\n"
        f"Forecast periods: {period_summary}\n\n"
        f"Write a 2-3 sentence narrative explaining how the latest data release "
        f"affected the forecast for {label}. Include at least one inline evidence "
        f"citation in the format {citation}. Be specific and grounded in the data."
    )


def _template_narrative(delta: dict, forecast: dict) -> str:
    """Fallback template narrative from numeric values with programmatic
    evidence links when Bedrock is unavailable.
    """
    series_id = delta.get("series_id", "UNKNOWN")
    direction = delta.get("direction", "unchanged")
    magnitude = delta.get("magnitude", 0.0)
    current_mean = delta.get("current_mean", 0.0)
    prior_mean = delta.get("prior_mean", 0.0)
    vintage_new = delta.get("vintage_date_new", "")

    meta = SUPPORTED_FRED_SERIES.get(series_id)
    label = meta["label"] if meta else series_id
    source = "BEA" if series_id.startswith("BEA_") else "FRED"

    # Direction verb
    if direction == "up":
        verb = "moved up"
    elif direction == "down":
        verb = "moved down"
    else:
        verb = "remained unchanged"

    # Forecast summary
    periods = forecast.get("periods", [])
    if periods:
        points = [str(p["point_value"]) for p in periods[:3]]
        forecast_summary = f"The forecast now projects {', '.join(points)}."
    else:
        forecast_summary = "No forecast periods available."

    citation = _format_evidence_link(source, series_id, vintage_new, current_mean)

    return (
        f"The forecast for {label} was updated following a semantic delta. "
        f"{label} {verb} by {magnitude:.1f} "
        f"(from {prior_mean:.1f} to {current_mean:.1f}). "
        f"{forecast_summary} {citation}"
    )


async def generate_narrative(delta_id: int, forecast_id: int) -> dict:
    """Generate a narrative for a delta+forecast pair.

    Steps:
      1. Read delta and forecast from DB.
      2. Build prompt, call Bedrock Claude.
      3. On failure, use _template_narrative.
      4. Extract evidence links.
      5. Persist to narratives table.
      6. Return narrative dict.
    """
    # ------------------------------------------------------------------
    # 1. Read delta and forecast from DB
    # ------------------------------------------------------------------
    delta_row = None
    forecast_periods: list[dict] = []
    forecast_indicator = ""

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM semantic_deltas WHERE id = ?", (delta_id,)
        )
        delta_row = await cursor.fetchone()

        cursor = await db.execute(
            "SELECT * FROM forecasts WHERE id = ?", (forecast_id,)
        )
        fc_row = await cursor.fetchone()

        if fc_row:
            forecast_indicator = fc_row["indicator_id"]
            forecast_date = fc_row["forecast_date"]
            # Get all periods for this forecast
            cursor = await db.execute(
                "SELECT period_date, point_value, upper_bound, lower_bound "
                "FROM forecasts "
                "WHERE indicator_id = ? AND forecast_date = ? "
                "ORDER BY horizon",
                (forecast_indicator, forecast_date),
            )
            rows = await cursor.fetchall()
            forecast_periods = [
                {
                    "period_date": r["period_date"],
                    "point_value": r["point_value"],
                    "upper_bound": r["upper_bound"],
                    "lower_bound": r["lower_bound"],
                }
                for r in rows
            ]

    if not delta_row:
        # No delta found — return a minimal narrative
        return {
            "delta_id": delta_id,
            "forecast_id": forecast_id,
            "indicator_id": forecast_indicator or "UNKNOWN",
            "narrative_text": "No delta data available for narrative generation.",
            "evidence_links": [],
            "is_scenario": False,
        }

    # Build delta dict from row
    delta = {
        "series_id": delta_row["series_id"],
        "vintage_date_new": delta_row["vintage_date_new"],
        "vintage_date_old": delta_row["vintage_date_old"],
        "direction": delta_row["direction"],
        "magnitude": delta_row["magnitude"],
        "driver_explanation": delta_row["driver_explanation"],
        "current_mean": delta_row["magnitude"],  # best available
        "prior_mean": 0.0,
    }

    forecast_dict = {
        "indicator_id": forecast_indicator,
        "periods": forecast_periods,
    }

    series_id = delta["series_id"]
    meta = SUPPORTED_FRED_SERIES.get(series_id, {"label": series_id})
    indicator_id = forecast_indicator or series_id

    # ------------------------------------------------------------------
    # 2-3. Build prompt, call Bedrock Claude (with template fallback)
    # ------------------------------------------------------------------
    prompt = _build_narrative_prompt(delta, forecast_dict, meta)
    narrative_text = ""

    try:
        import boto3

        session_kwargs: dict = {"region_name": AWS_REGION}
        if AWS_PROFILE:
            session_kwargs["profile_name"] = AWS_PROFILE
        session = boto3.Session(**session_kwargs)
        client = session.client("bedrock-runtime")

        body = _json_mod.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}],
        })

        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        result = _json_mod.loads(response["body"].read())
        narrative_text = result["content"][0]["text"].strip()

    except Exception as exc:
        logger.warning(
            "Bedrock narrative call failed for delta %s — using template: %s",
            delta_id, exc,
        )
        narrative_text = _template_narrative(delta, forecast_dict)

    # ------------------------------------------------------------------
    # 4. Extract evidence links
    # ------------------------------------------------------------------
    evidence_links = _extract_evidence_links(narrative_text, delta)

    # ------------------------------------------------------------------
    # 5. Persist to narratives table
    # ------------------------------------------------------------------
    evidence_json = _json_mod.dumps(evidence_links)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO narratives
               (delta_id, forecast_id, indicator_id,
                narrative_text, evidence_links_json, is_scenario)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (delta_id, forecast_id, indicator_id, narrative_text, evidence_json),
        )
        await db.commit()
        narrative_id = cursor.lastrowid

    # ------------------------------------------------------------------
    # 6. Return narrative dict
    # ------------------------------------------------------------------
    return {
        "id": narrative_id,
        "delta_id": delta_id,
        "forecast_id": forecast_id,
        "indicator_id": indicator_id,
        "narrative_text": narrative_text,
        "evidence_links": evidence_links,
        "is_scenario": False,
    }


async def generate_whatif_narrative(whatif_result: dict, indicator_id: str) -> dict:
    """Generate a narrative for a what-if scenario.

    Uses LLM if available, template fallback otherwise.
    Sets is_scenario = True. Does NOT persist to DB (ephemeral).
    """
    # Build a pseudo-delta from the what-if result
    periods = whatif_result.get("periods", [])
    magnitude = whatif_result.get("shock_magnitude", 0.0)
    direction = whatif_result.get("shock_direction", "up")

    # Try to infer magnitude from periods if not directly available
    if not magnitude and periods:
        magnitude = abs(periods[0].get("point_value", 0.0))

    meta = SUPPORTED_FRED_SERIES.get(indicator_id, {"label": indicator_id})
    label = meta.get("label", indicator_id) if isinstance(meta, dict) else indicator_id
    source = "BEA" if indicator_id.startswith("BEA_") else "FRED"

    delta = {
        "series_id": indicator_id,
        "vintage_date_new": date.today().isoformat(),
        "vintage_date_old": None,
        "direction": direction,
        "magnitude": magnitude,
        "current_mean": magnitude,
        "prior_mean": 0.0,
        "driver_explanation": f"What-if scenario: {direction} shock of {magnitude:.2f} on {label}.",
    }

    forecast_dict = {
        "indicator_id": indicator_id,
        "periods": periods,
    }

    # ------------------------------------------------------------------
    # Try LLM, fall back to template
    # ------------------------------------------------------------------
    narrative_text = ""

    try:
        import boto3

        prompt = (
            f"You are an economic analyst. Write a 2-3 sentence narrative for a "
            f"HYPOTHETICAL what-if scenario. The user applied a {direction} shock "
            f"of {magnitude:.2f} to {label} ({indicator_id}). "
        )
        if periods:
            points = [f"{p['period_date']}: {p['point_value']}" for p in periods[:3]]
            prompt += f"The scenario forecast projects: {', '.join(points)}. "
        prompt += (
            f"Clearly state this is a hypothetical scenario, not an actual update. "
            f"Include an inline citation like "
            f"{_format_evidence_link(source, indicator_id, date.today().isoformat(), magnitude)}."
        )

        session_kwargs: dict = {"region_name": AWS_REGION}
        if AWS_PROFILE:
            session_kwargs["profile_name"] = AWS_PROFILE
        session = boto3.Session(**session_kwargs)
        client = session.client("bedrock-runtime")

        body = _json_mod.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}],
        })

        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        result = _json_mod.loads(response["body"].read())
        narrative_text = result["content"][0]["text"].strip()

    except Exception as exc:
        logger.warning(
            "Bedrock what-if narrative failed for %s — using template: %s",
            indicator_id, exc,
        )
        # Template fallback for what-if
        if periods:
            points = [str(p["point_value"]) for p in periods[:3]]
            forecast_summary = f"The scenario forecast projects {', '.join(points)}."
        else:
            forecast_summary = "No forecast periods available."

        verb = "moved up" if direction == "up" else "moved down"
        citation = _format_evidence_link(
            source, indicator_id, date.today().isoformat(), magnitude
        )
        narrative_text = (
            f"In this hypothetical scenario, {label} {verb} by {magnitude:.1f}. "
            f"{forecast_summary} "
            f"This is a what-if analysis and does not reflect actual data. {citation}"
        )

    # ------------------------------------------------------------------
    # Extract evidence links (do NOT persist — ephemeral)
    # ------------------------------------------------------------------
    evidence_links = _extract_evidence_links(narrative_text, delta)

    return {
        "delta_id": None,
        "forecast_id": None,
        "indicator_id": indicator_id,
        "narrative_text": narrative_text,
        "evidence_links": evidence_links,
        "is_scenario": True,
    }


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------
class IngestRequest(BaseModel):
    source: str  # 'FRED' or 'BEA'
    series_id: str
    dataset_name: Optional[str] = None  # BEA only
    table_name: Optional[str] = None  # BEA only


class SemanticDelta(BaseModel):
    id: int
    series_id: str
    vintage_date_new: str
    vintage_date_old: Optional[str]
    direction: str  # 'up', 'down', 'unchanged', 'initial'
    magnitude: float
    affected_component: Optional[str]
    confidence_score: float
    driver_explanation: str
    is_llm_validated: bool
    created_at: str


class ForecastRequest(BaseModel):
    indicator_id: str
    horizon: int = 6


class ForecastPeriod(BaseModel):
    period_date: str
    point_value: float
    upper_bound: float
    lower_bound: float


class ForecastResult(BaseModel):
    indicator_id: str
    forecast_date: str
    periods: List[ForecastPeriod]
    model_type: str
    delta_id: Optional[int] = None


class WhatIfRequest(BaseModel):
    indicator_id: str
    shock_magnitude: float
    shock_direction: str  # 'up' or 'down'


class EvidenceLink(BaseModel):
    source: str  # 'FRED' or 'BEA'
    series_id: str
    date: str
    value: float
    label: str  # human-readable label


class Narrative(BaseModel):
    id: int
    delta_id: Optional[int]
    forecast_id: Optional[int]
    indicator_id: str
    narrative_text: str
    evidence_links: List[EvidenceLink]
    is_scenario: bool
    created_at: str


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: initialise database. Shutdown: nothing special."""
    await init_db()
    yield


app = FastAPI(title="Semantic Delta", lifespan=lifespan)

# CORS — allow all origins for hackathon convenience
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


# ---------------------------------------------------------------------------
# SPA Route
# ---------------------------------------------------------------------------
@app.get("/")
async def spa_root():
    """Serve the single-page application."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "templates", "index.html"))


# ---------------------------------------------------------------------------
# REST API Endpoints
# ---------------------------------------------------------------------------

# --- 8.1: Ingestion and Indicator Endpoints ---

@app.post("/api/ingest")
async def api_ingest(req: IngestRequest):
    """Ingest data for a source + series. Routes to ingest_fred or ingest_bea."""
    if req.source.upper() == "FRED":
        return await ingest_fred(req.series_id)
    elif req.source.upper() == "BEA":
        return await ingest_bea(
            dataset_name=req.dataset_name or "NIPA",
            table_name=req.table_name or "T10101",
            series_id=req.series_id,
        )
    else:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported source '{req.source}'. Use 'FRED' or 'BEA'.",
        )


@app.get("/api/indicators")
async def api_indicators():
    """List all indicators with latest values, dates, and direction from most recent delta."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Get latest observation per series_id
        cursor = await db.execute(
            """
            SELECT o.series_id, o.source, o.value AS latest_value,
                   o.observation_date AS latest_date
            FROM observations o
            INNER JOIN (
                SELECT series_id, MAX(observation_date) AS max_date
                FROM observations
                GROUP BY series_id
            ) latest ON o.series_id = latest.series_id
                    AND o.observation_date = latest.max_date
            GROUP BY o.series_id
            """
        )
        rows = await cursor.fetchall()

        indicators = []
        for r in rows:
            # Get direction from most recent semantic_delta for this series
            dc = await db.execute(
                "SELECT direction FROM semantic_deltas "
                "WHERE series_id = ? ORDER BY created_at DESC LIMIT 1",
                (r["series_id"],),
            )
            delta_row = await dc.fetchone()
            direction = delta_row["direction"] if delta_row else None

            indicators.append({
                "series_id": r["series_id"],
                "source": r["source"],
                "latest_value": r["latest_value"],
                "latest_date": r["latest_date"],
                "direction": direction,
            })

    return indicators


# --- 8.2: Delta Endpoints ---

@app.get("/api/deltas")
async def api_deltas(
    indicator_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    """List semantic deltas with optional filtering."""
    query = "SELECT * FROM semantic_deltas WHERE 1=1"
    params: list = []

    if indicator_id:
        query += " AND series_id = ?"
        params.append(indicator_id)
    if start_date:
        query += " AND vintage_date_new >= ?"
        params.append(start_date)
    if end_date:
        query += " AND vintage_date_new <= ?"
        params.append(end_date)

    query += " ORDER BY created_at DESC"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

    return [
        {
            "id": r["id"],
            "series_id": r["series_id"],
            "vintage_date_new": r["vintage_date_new"],
            "vintage_date_old": r["vintage_date_old"],
            "direction": r["direction"],
            "magnitude": r["magnitude"],
            "affected_component": r["affected_component"],
            "confidence_score": r["confidence_score"],
            "driver_explanation": r["driver_explanation"],
            "is_llm_validated": bool(r["is_llm_validated"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# --- 8.3: Forecast and What-If Endpoints ---

@app.post("/api/forecast")
async def api_forecast(req: ForecastRequest):
    """Generate/update forecast for an indicator."""
    return await generate_forecast(req.indicator_id, req.horizon)


@app.get("/api/forecast/{indicator_id}")
async def api_get_forecast(indicator_id: str):
    """Return the latest forecast for an indicator."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Find the most recent forecast_date for this indicator
        cursor = await db.execute(
            "SELECT forecast_date, model_type, delta_id FROM forecasts "
            "WHERE indicator_id = ? ORDER BY created_at DESC LIMIT 1",
            (indicator_id,),
        )
        row = await cursor.fetchone()

        if not row:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=404,
                detail=f"No forecast found for indicator '{indicator_id}'.",
            )

        forecast_date = row["forecast_date"]
        model_type = row["model_type"]
        delta_id = row["delta_id"]

        # Get all periods for this forecast
        cursor = await db.execute(
            "SELECT period_date, point_value, upper_bound, lower_bound "
            "FROM forecasts "
            "WHERE indicator_id = ? AND forecast_date = ? "
            "ORDER BY horizon",
            (indicator_id, forecast_date),
        )
        periods = await cursor.fetchall()

    return {
        "indicator_id": indicator_id,
        "forecast_date": forecast_date,
        "periods": [
            {
                "period_date": p["period_date"],
                "point_value": p["point_value"],
                "upper_bound": p["upper_bound"],
                "lower_bound": p["lower_bound"],
            }
            for p in periods
        ],
        "model_type": model_type,
        "delta_id": delta_id,
    }


@app.post("/api/whatif")
async def api_whatif(req: WhatIfRequest):
    """Compute a what-if scenario forecast + narrative."""
    whatif_result = await compute_whatif(
        req.indicator_id, req.shock_magnitude, req.shock_direction,
    )
    narrative = await generate_whatif_narrative(whatif_result, req.indicator_id)
    return {
        "forecast": whatif_result,
        "narrative": narrative,
    }


# --- 8.4: Narrative and Pipeline Endpoints ---

@app.get("/api/narratives")
async def api_narratives(
    indicator_id: Optional[str] = None,
    delta_id: Optional[int] = None,
):
    """List narratives with optional filtering by indicator_id or delta_id."""
    query = "SELECT * FROM narratives WHERE 1=1"
    params: list = []

    if indicator_id:
        query += " AND indicator_id = ?"
        params.append(indicator_id)
    if delta_id is not None:
        query += " AND delta_id = ?"
        params.append(delta_id)

    query += " ORDER BY created_at DESC"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

    results = []
    for r in rows:
        # Parse evidence_links_json back to a list
        try:
            evidence_links = _json_mod.loads(r["evidence_links_json"])
        except (ValueError, TypeError):
            evidence_links = []

        results.append({
            "id": r["id"],
            "delta_id": r["delta_id"],
            "forecast_id": r["forecast_id"],
            "indicator_id": r["indicator_id"],
            "narrative_text": r["narrative_text"],
            "evidence_links": evidence_links,
            "is_scenario": bool(r["is_scenario"]),
            "created_at": r["created_at"],
        })

    return results


@app.post("/api/pipeline")
async def api_pipeline():
    """Run full bulk pipeline: ingest → deltas → forecasts → narratives.

    Returns a summary with counts for each step.
    """
    # Step 1: Ingest all indicators
    ingest_summary = await ingest_all()

    # Step 2: Compute all deltas
    deltas = await compute_all_deltas()

    # Separate actual deltas from error entries
    errors_entry = None
    computed_deltas = []
    for d in deltas:
        if "_errors" in d:
            errors_entry = d
        else:
            computed_deltas.append(d)

    # Step 3 & 4: For each delta, generate/update forecast and narrative
    forecasts_generated = 0
    forecasts_failed = 0
    narratives_generated = 0
    narratives_failed = 0

    for delta in computed_deltas:
        series_id = delta.get("series_id")
        if not series_id:
            continue

        # Get the delta_id from DB for linking
        delta_id = None
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id FROM semantic_deltas "
                "WHERE series_id = ? ORDER BY created_at DESC LIMIT 1",
                (series_id,),
            )
            row = await cursor.fetchone()
            if row:
                delta_id = row["id"]

        # Generate forecast
        forecast_result = None
        try:
            forecast_result = await generate_forecast(
                series_id, horizon=6, delta_id=delta_id,
            )
            forecasts_generated += 1
        except Exception as exc:
            logger.warning("Pipeline forecast failed for %s: %s", series_id, exc)
            forecasts_failed += 1

        # Generate narrative (needs both delta_id and a forecast_id)
        if delta_id and forecast_result:
            try:
                # Get the forecast_id from DB
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    cursor = await db.execute(
                        "SELECT id FROM forecasts "
                        "WHERE indicator_id = ? ORDER BY created_at DESC LIMIT 1",
                        (series_id,),
                    )
                    fc_row = await cursor.fetchone()
                    forecast_id = fc_row["id"] if fc_row else None

                if forecast_id:
                    await generate_narrative(delta_id, forecast_id)
                    narratives_generated += 1
                else:
                    narratives_failed += 1
            except Exception as exc:
                logger.warning("Pipeline narrative failed for %s: %s", series_id, exc)
                narratives_failed += 1

    return {
        "ingest": ingest_summary,
        "deltas_computed": len(computed_deltas),
        "delta_errors": errors_entry.get("_errors", []) if errors_entry else [],
        "forecasts_generated": forecasts_generated,
        "forecasts_failed": forecasts_failed,
        "narratives_generated": narratives_generated,
        "narratives_failed": narratives_failed,
    }
