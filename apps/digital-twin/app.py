"""Economic Digital Twin Dashboard — FastAPI backend."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import httpx

import aiosqlite
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
BEA_API_KEY = os.environ.get("BEA_API_KEY", "").strip()
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECOGNIZED_VARIABLES = [
    "energy_price", "interest_rate", "oil_price",
    "tax_rate", "government_spending", "trade_tariff",
    "cpi", "unemployment", "gdp_growth",
]

DEFAULT_FRED_SERIES = ["CPIAUCSL", "UNRATE", "FEDFUNDS", "DGS10"]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DB_PATH = os.path.join(os.path.dirname(__file__), "digital_twin.db")

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS indicators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    indicator_id TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    value REAL NOT NULL,
    source TEXT NOT NULL,
    ingested_at TEXT DEFAULT (datetime('now')),
    UNIQUE(indicator_id, observation_date)
);

CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    indicator_id TEXT NOT NULL,
    forecast_date TEXT NOT NULL,
    horizon_period INTEGER NOT NULL,
    p10 REAL NOT NULL,
    p50 REAL NOT NULL,
    p90 REAL NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS forecast_explanations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_id INTEGER NOT NULL REFERENCES forecasts(id),
    feature_name TEXT NOT NULL,
    importance_score REAL NOT NULL,
    direction TEXT NOT NULL,
    explanation_text TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shock_variable TEXT NOT NULL,
    shock_magnitude REAL NOT NULL,
    shock_duration INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scenario_trajectories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id INTEGER NOT NULL REFERENCES scenarios(id),
    period INTEGER NOT NULL,
    indicator_name TEXT NOT NULL,
    shock_value REAL NOT NULL,
    counterfactual_value REAL NOT NULL,
    delta REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id INTEGER NOT NULL REFERENCES scenarios(id),
    period INTEGER NOT NULL,
    agent_type TEXT NOT NULL,
    beliefs TEXT NOT NULL,
    action TEXT NOT NULL,
    rationale TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    indicator_id TEXT NOT NULL,
    observed_value REAL NOT NULL,
    p10_value REAL NOT NULL,
    p90_value REAL NOT NULL,
    severity TEXT NOT NULL,
    driver_attribution TEXT,
    narrative TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


async def init_db() -> None:
    """Create all SQLite tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_CREATE_TABLES_SQL)
        await db.commit()


@asynccontextmanager
async def get_db():
    """Async context manager that yields an aiosqlite connection."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        yield db


# ---------------------------------------------------------------------------
# App (with lifespan)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(application: FastAPI):
    """Initialise DB on startup."""
    await init_db()
    yield


app = FastAPI(title="Economic Digital Twin Dashboard", lifespan=_lifespan)

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_spa():
    """Serve the single-page application."""
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    return FileResponse(template_path)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ShockSpecification(BaseModel):
    variable: str
    magnitude: float
    duration: int

    @field_validator("duration")
    @classmethod
    def duration_positive(cls, v):
        if v <= 0:
            raise ValueError("duration must be positive")
        return v

    @field_validator("variable")
    @classmethod
    def variable_recognized(cls, v):
        if v not in RECOGNIZED_VARIABLES:
            raise ValueError(
                f"variable '{v}' is not recognized. "
                f"Must be one of: {', '.join(RECOGNIZED_VARIABLES)}"
            )
        return v


class AgentResponse(BaseModel):
    agent_type: str
    beliefs: dict
    action: str
    rationale: str


class ForecastPeriod(BaseModel):
    date: str
    p10: float
    p50: float
    p90: float


class ForecastResult(BaseModel):
    forecast_id: int
    indicator_id: str
    periods: list[ForecastPeriod]
    explanation: Optional[str] = None
    features: Optional[list[dict]] = None


class TrajectoryPeriod(BaseModel):
    period: int
    inflation: float
    gdp_growth: float
    unemployment: float


class ScenarioResult(BaseModel):
    scenario_id: int
    shock: ShockSpecification
    trajectory: list[TrajectoryPeriod]
    counterfactual: list[TrajectoryPeriod]
    agents: list[AgentResponse]


class AlertModel(BaseModel):
    id: int
    indicator_id: str
    observed_value: float
    p10_value: float
    p90_value: float
    severity: str
    driver_attribution: Optional[list[dict]] = None
    narrative: Optional[str] = None
    created_at: str


# ---------------------------------------------------------------------------
# Data Ingestion Service
# ---------------------------------------------------------------------------

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"


async def ingest_fred(series_ids: list[str] | None = None) -> dict:
    """Fetch observations from FRED API for given series IDs and store in DB.

    Args:
        series_ids: List of FRED series IDs. Defaults to DEFAULT_FRED_SERIES.

    Returns:
        {"ingested": int, "errors": list[str]}
    """
    if series_ids is None:
        series_ids = DEFAULT_FRED_SERIES

    ingested = 0
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        for series_id in series_ids:
            try:
                response = await client.get(
                    FRED_BASE_URL,
                    params={
                        "series_id": series_id,
                        "api_key": FRED_API_KEY,
                        "file_type": "json",
                    },
                )
                response.raise_for_status()
                data = response.json()

                observations = data.get("observations", [])
                async with get_db() as db:
                    count_before_cursor = await db.execute(
                        "SELECT COUNT(*) FROM indicators WHERE indicator_id = ? AND source = 'FRED'",
                        (series_id,),
                    )
                    count_before = (await count_before_cursor.fetchone())[0]

                    for obs in observations:
                        date = obs.get("date")
                        raw_value = obs.get("value", "")
                        # FRED uses "." for missing values — skip those
                        if not date or raw_value in ("", "."):
                            continue
                        try:
                            value = float(raw_value)
                        except (ValueError, TypeError):
                            continue

                        await db.execute(
                            """
                            INSERT OR IGNORE INTO indicators
                                (indicator_id, observation_date, value, source)
                            VALUES (?, ?, ?, 'FRED')
                            """,
                            (series_id, date, value),
                        )

                    await db.commit()

                    count_after_cursor = await db.execute(
                        "SELECT COUNT(*) FROM indicators WHERE indicator_id = ? AND source = 'FRED'",
                        (series_id,),
                    )
                    count_after = (await count_after_cursor.fetchone())[0]
                    ingested += count_after - count_before

                # Check alerts for this series after ingestion
                try:
                    await _check_alerts_after_ingestion(series_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Alert check failed for FRED %s: %s", series_id, exc)

            except httpx.HTTPStatusError as exc:
                msg = f"FRED HTTP error for {series_id}: {exc.response.status_code}"
                logger.error(msg, extra={"indicator_id": series_id, "source": "FRED"})
                errors.append(msg)
            except httpx.RequestError as exc:
                msg = f"FRED request error for {series_id}: {exc}"
                logger.error(msg, extra={"indicator_id": series_id, "source": "FRED"})
                errors.append(msg)
            except Exception as exc:  # noqa: BLE001
                msg = f"Unexpected error ingesting FRED series {series_id}: {exc}"
                logger.error(msg, extra={"indicator_id": series_id, "source": "FRED"})
                errors.append(msg)

    return {"ingested": ingested, "errors": errors}


# ---------------------------------------------------------------------------
# BEA Data Ingestion
# ---------------------------------------------------------------------------

BEA_BASE_URL = "https://apps.bea.gov/api/data/"


def _bea_quarter_to_date(time_period: str) -> str:
    """Convert BEA TimePeriod like '2024Q1' to an ISO date string (first day of quarter).

    Q1 -> 01-01, Q2 -> 04-01, Q3 -> 07-01, Q4 -> 10-01
    """
    quarter_start = {"Q1": "01-01", "Q2": "04-01", "Q3": "07-01", "Q4": "10-01"}
    year = time_period[:4]
    quarter = time_period[4:]
    return f"{year}-{quarter_start.get(quarter, '01-01')}"


async def ingest_bea(dataset: str = "NIPA", table_name: str = "T10101") -> dict:
    """Fetch GDP/related data from BEA API and store in DB.

    Args:
        dataset: BEA dataset name (default: NIPA).
        table_name: BEA table name (default: T10101 — GDP).

    Returns:
        {"ingested": int, "errors": list[str]}
    """
    ingested = 0
    errors: list[str] = []
    indicator_id = "GDP_GROWTH"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(
                BEA_BASE_URL,
                params={
                    "UserID": BEA_API_KEY,
                    "method": "GetData",
                    "DataSetName": dataset,
                    "TableName": table_name,
                    "Frequency": "Q",
                    "Year": "ALL",
                    "ResultFormat": "JSON",
                },
            )
            response.raise_for_status()
            payload = response.json()

            data_items = payload["BEAAPI"]["Results"]["Data"]

            async with get_db() as db:
                count_before_cursor = await db.execute(
                    "SELECT COUNT(*) FROM indicators WHERE indicator_id = ? AND source = 'BEA'",
                    (indicator_id,),
                )
                count_before = (await count_before_cursor.fetchone())[0]

                for item in data_items:
                    time_period = item.get("TimePeriod", "")
                    raw_value = item.get("DataValue", "")

                    if not time_period or not raw_value:
                        continue

                    # BEA sometimes returns commas in numbers and non-numeric markers
                    cleaned = raw_value.replace(",", "").strip()
                    try:
                        value = float(cleaned)
                    except (ValueError, TypeError):
                        continue

                    obs_date = _bea_quarter_to_date(time_period)

                    await db.execute(
                        """
                        INSERT OR IGNORE INTO indicators
                            (indicator_id, observation_date, value, source)
                        VALUES (?, ?, ?, 'BEA')
                        """,
                        (indicator_id, obs_date, value),
                    )

                await db.commit()

                count_after_cursor = await db.execute(
                    "SELECT COUNT(*) FROM indicators WHERE indicator_id = ? AND source = 'BEA'",
                    (indicator_id,),
                )
                count_after = (await count_after_cursor.fetchone())[0]
                ingested = count_after - count_before

            # Check alerts for BEA indicator after ingestion
            try:
                await _check_alerts_after_ingestion(indicator_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Alert check failed for BEA %s: %s", indicator_id, exc)

        except httpx.HTTPStatusError as exc:
            msg = f"BEA HTTP error for {indicator_id}: {exc.response.status_code}"
            logger.error(msg, extra={"indicator_id": indicator_id, "source": "BEA"})
            errors.append(msg)
        except httpx.RequestError as exc:
            msg = f"BEA request error for {indicator_id}: {exc}"
            logger.error(msg, extra={"indicator_id": indicator_id, "source": "BEA"})
            errors.append(msg)
        except (KeyError, TypeError) as exc:
            msg = f"BEA response parsing error for {indicator_id}: {exc}"
            logger.error(msg, extra={"indicator_id": indicator_id, "source": "BEA"})
            errors.append(msg)
        except Exception as exc:  # noqa: BLE001
            msg = f"Unexpected error ingesting BEA data for {indicator_id}: {exc}"
            logger.error(msg, extra={"indicator_id": indicator_id, "source": "BEA"})
            errors.append(msg)

    return {"ingested": ingested, "errors": errors}


# ---------------------------------------------------------------------------
# ingest_all — orchestrates FRED + BEA ingestion
# ---------------------------------------------------------------------------

async def ingest_all() -> dict:
    """Call both FRED and BEA ingestion and return combined results.

    Returns:
        {"fred": {"ingested": int, "errors": [...]}, "bea": {"ingested": int, "errors": [...]}}
    """
    fred_result = await ingest_fred()
    bea_result = await ingest_bea()
    return {"fred": fred_result, "bea": bea_result}


# ---------------------------------------------------------------------------
# API endpoints — Ingest and Indicators
# ---------------------------------------------------------------------------

from fastapi import Query
from fastapi.responses import JSONResponse


@app.post("/api/ingest")
async def api_ingest():
    """Trigger data ingestion from FRED and BEA APIs.

    Returns:
        {"fred": {...}, "bea": {...}}
    """
    result = await ingest_all()
    return result


@app.get("/api/indicators")
async def api_indicators(indicator_id: Optional[str] = Query(default=None)):
    """Return stored observations from the indicators table.

    Args:
        indicator_id: Optional filter by indicator ID.

    Returns:
        {"observations": [{"indicator_id": ..., "observation_date": ..., "value": ..., "source": ...}]}
    """
    async with get_db() as db:
        if indicator_id:
            cursor = await db.execute(
                """
                SELECT indicator_id, observation_date, value, source
                FROM indicators
                WHERE indicator_id = ?
                ORDER BY observation_date ASC
                """,
                (indicator_id,),
            )
        else:
            cursor = await db.execute(
                """
                SELECT indicator_id, observation_date, value, source
                FROM indicators
                ORDER BY indicator_id ASC, observation_date ASC
                """
            )
        rows = await cursor.fetchall()

    observations = [
        {
            "indicator_id": row["indicator_id"],
            "observation_date": row["observation_date"],
            "value": row["value"],
            "source": row["source"],
        }
        for row in rows
    ]
    return {"observations": observations}


# ---------------------------------------------------------------------------
# Forecast Service
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta
import numpy as np

try:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    _HAS_STATSMODELS = True
except ImportError:
    _HAS_STATSMODELS = False

MIN_OBSERVATIONS = 10


async def generate_forecast(indicator_id: str, periods: int = 4) -> dict:
    """Generate p10/p50/p90 probabilistic forecasts for an indicator.

    Uses Holt-Winters ExponentialSmoothing (ETS) as primary model with a
    simple linear-trend fallback. Confidence bands are derived from residual
    standard deviation: p50 ± 1.28 * std_residuals.

    Args:
        indicator_id: The indicator to forecast (must exist in indicators table).
        periods: Number of future periods to forecast (default 4).

    Returns:
        On success:
            {
                "forecast_id": int,          # id of the first inserted forecast row
                "indicator_id": str,
                "periods": [
                    {"date": str, "p10": float, "p50": float, "p90": float},
                    ...
                ]
            }
        On insufficient data:
            {"error": str, "indicator_id": str, "min_required": int, "available": int}
    """
    # 1. Fetch historical data
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT observation_date, value
            FROM indicators
            WHERE indicator_id = ?
            ORDER BY observation_date ASC
            """,
            (indicator_id,),
        )
        rows = await cursor.fetchall()

    if len(rows) < MIN_OBSERVATIONS:
        return {
            "error": (
                f"Insufficient historical data for '{indicator_id}'. "
                f"Need at least {MIN_OBSERVATIONS} observations, found {len(rows)}."
            ),
            "indicator_id": indicator_id,
            "min_required": MIN_OBSERVATIONS,
            "available": len(rows),
        }

    values = np.array([row["value"] for row in rows], dtype=float)
    last_date_str = rows[-1]["observation_date"]

    # 2. Fit model and generate point forecasts + residual std
    point_forecasts: np.ndarray
    std_residuals: float

    fitted = False
    if _HAS_STATSMODELS:
        try:
            model = ExponentialSmoothing(
                values,
                trend="add",
                seasonal=None,
                initialization_method="estimated",
            )
            fit = model.fit(optimized=True, disp=False)
            point_forecasts = fit.forecast(periods)
            residuals = fit.resid
            std_residuals = float(np.std(residuals)) if len(residuals) > 1 else 0.0
            fitted = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ETS model failed for %s, falling back to linear trend: %s",
                indicator_id, exc,
            )

    if not fitted:
        # Linear trend fallback
        x = np.arange(len(values), dtype=float)
        slope, intercept = np.polyfit(x, values, 1)
        future_x = np.arange(len(values), len(values) + periods, dtype=float)
        point_forecasts = slope * future_x + intercept
        # Residuals from linear fit
        residuals = values - (slope * x + intercept)
        std_residuals = float(np.std(residuals)) if len(residuals) > 1 else 0.0

    # 3. Build forecast periods with quantile bands
    try:
        last_date = datetime.strptime(last_date_str, "%Y-%m-%d")
    except ValueError:
        last_date = datetime.now()

    forecast_date_str = datetime.now().strftime("%Y-%m-%d")
    margin = 1.28 * std_residuals

    period_results = []
    for i in range(periods):
        future_date = last_date + timedelta(days=30 * (i + 1))
        p50 = float(point_forecasts[i])
        p10 = p50 - margin
        p90 = p50 + margin
        period_results.append({
            "horizon_period": i + 1,
            "date": future_date.strftime("%Y-%m-%d"),
            "p10": round(p10, 6),
            "p50": round(p50, 6),
            "p90": round(p90, 6),
        })

    # 4. Store in forecasts table
    first_forecast_id: int | None = None
    async with get_db() as db:
        for pr in period_results:
            cursor = await db.execute(
                """
                INSERT INTO forecasts
                    (indicator_id, forecast_date, horizon_period, p10, p50, p90)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    indicator_id,
                    forecast_date_str,
                    pr["horizon_period"],
                    pr["p10"],
                    pr["p50"],
                    pr["p90"],
                ),
            )
            if first_forecast_id is None:
                first_forecast_id = cursor.lastrowid
        await db.commit()

    # 5. Return result
    return {
        "forecast_id": first_forecast_id,
        "indicator_id": indicator_id,
        "periods": [
            {"date": pr["date"], "p10": pr["p10"], "p50": pr["p50"], "p90": pr["p90"]}
            for pr in period_results
        ],
    }


# ---------------------------------------------------------------------------
# Explainability Service
# ---------------------------------------------------------------------------

import json

try:
    import boto3
    _HAS_BOTO3 = True
except ImportError:
    _HAS_BOTO3 = False


async def compute_feature_importance(
    indicator_id: str, forecast_id: int
) -> list[dict]:
    """Compute correlation-based feature importance for a forecast.

    For each *other* indicator in the DB, compute the Pearson correlation
    with the target indicator's historical values (aligned by date).
    Returns a list sorted by importance_score descending.

    Each entry: {"feature_name": str, "importance_score": float, "direction": "positive"|"negative"}
    """
    # 1. Fetch target indicator values keyed by date
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT observation_date, value FROM indicators "
            "WHERE indicator_id = ? ORDER BY observation_date ASC",
            (indicator_id,),
        )
        target_rows = await cursor.fetchall()

        # 2. Get all distinct other indicator IDs
        cursor2 = await db.execute(
            "SELECT DISTINCT indicator_id FROM indicators WHERE indicator_id != ?",
            (indicator_id,),
        )
        other_ids = [r["indicator_id"] for r in await cursor2.fetchall()]

    if not target_rows or not other_ids:
        return []

    target_by_date: dict[str, float] = {
        r["observation_date"]: r["value"] for r in target_rows
    }

    features: list[dict] = []

    for other_id in other_ids:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT observation_date, value FROM indicators "
                "WHERE indicator_id = ? ORDER BY observation_date ASC",
                (other_id,),
            )
            other_rows = await cursor.fetchall()

        # Align by common dates
        common_target: list[float] = []
        common_other: list[float] = []
        for row in other_rows:
            date = row["observation_date"]
            if date in target_by_date:
                common_target.append(target_by_date[date])
                common_other.append(row["value"])

        if len(common_target) < 3:
            # Not enough overlap for meaningful correlation
            continue

        arr_target = np.array(common_target)
        arr_other = np.array(common_other)

        # Pearson correlation
        std_t = np.std(arr_target)
        std_o = np.std(arr_other)
        if std_t == 0 or std_o == 0:
            corr = 0.0
        else:
            corr = float(np.corrcoef(arr_target, arr_other)[0, 1])
            if np.isnan(corr):
                corr = 0.0

        features.append({
            "feature_name": other_id,
            "importance_score": round(abs(corr), 6),
            "direction": "positive" if corr >= 0 else "negative",
        })

    # Sort descending by importance_score
    features.sort(key=lambda f: f["importance_score"], reverse=True)

    # 3. Store in forecast_explanations table (no explanation_text yet)
    async with get_db() as db:
        for feat in features:
            await db.execute(
                """
                INSERT INTO forecast_explanations
                    (forecast_id, feature_name, importance_score, direction)
                VALUES (?, ?, ?, ?)
                """,
                (forecast_id, feat["feature_name"], feat["importance_score"], feat["direction"]),
            )
        await db.commit()

    return features


async def generate_explanation_text(
    indicator_id: str, features: list[dict]
) -> str:
    """Generate a plain-language explanation for forecast drivers via Bedrock Claude.

    Falls back to a simple template if the Bedrock call fails or boto3 is unavailable.
    """
    if not features:
        return f"No contributing factors identified for {indicator_id}."

    # Build a summary of top features for the prompt
    top_n = features[:5]
    feature_lines = "\n".join(
        f"- {f['feature_name']}: importance={f['importance_score']:.4f}, "
        f"direction={f['direction']}"
        for f in top_n
    )

    # Fetch recent values for referenced indicators to ground the explanation
    recent_values: dict[str, float] = {}
    indicator_ids_to_fetch = [indicator_id] + [f["feature_name"] for f in top_n]
    async with get_db() as db:
        for iid in indicator_ids_to_fetch:
            cursor = await db.execute(
                "SELECT value FROM indicators WHERE indicator_id = ? "
                "ORDER BY observation_date DESC LIMIT 1",
                (iid,),
            )
            row = await cursor.fetchone()
            if row:
                recent_values[iid] = row["value"]

    recent_lines = "\n".join(
        f"- {iid}: latest value = {val}" for iid, val in recent_values.items()
    )

    prompt = (
        f"You are an economic analyst. Explain in plain language what is driving "
        f"the forecast for the indicator '{indicator_id}'.\n\n"
        f"Top contributing factors (by correlation-based importance):\n{feature_lines}\n\n"
        f"Recent indicator values:\n{recent_lines}\n\n"
        f"Write a concise 2-3 sentence explanation suitable for a dashboard. "
        f"Reference specific indicator names and their recent values. "
        f"Mention whether each top factor pushes the forecast up or down."
    )

    # Try Bedrock Claude
    if _HAS_BOTO3:
        try:
            bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
            response = bedrock.invoke_model(
                modelId=BEDROCK_MODEL_ID,
                body=json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                }),
                contentType="application/json",
            )
            result = json.loads(response["body"].read())
            text = result["content"][0]["text"]
            return text.strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Bedrock explanation call failed for %s, using template fallback: %s",
                indicator_id, exc,
            )

    # Template-based fallback
    parts = []
    for f in top_n:
        direction_word = "positively" if f["direction"] == "positive" else "negatively"
        val_str = ""
        if f["feature_name"] in recent_values:
            val_str = f" (recent value: {recent_values[f['feature_name']]})"
        parts.append(
            f"{f['feature_name']}{val_str} is {direction_word} correlated "
            f"(score: {f['importance_score']:.4f})"
        )

    target_val = recent_values.get(indicator_id, "N/A")
    explanation = (
        f"The forecast for {indicator_id} (recent value: {target_val}) "
        f"is primarily driven by: {'; '.join(parts)}."
    )
    return explanation


async def store_explanation_text(forecast_id: int, explanation_text: str) -> None:
    """Store the explanation text on the first feature row for a given forecast_id."""
    async with get_db() as db:
        # Find the first (lowest id) forecast_explanation row for this forecast
        cursor = await db.execute(
            "SELECT id FROM forecast_explanations WHERE forecast_id = ? ORDER BY id ASC LIMIT 1",
            (forecast_id,),
        )
        row = await cursor.fetchone()
        if row:
            await db.execute(
                "UPDATE forecast_explanations SET explanation_text = ? WHERE id = ?",
                (explanation_text, row["id"]),
            )
            await db.commit()


# ---------------------------------------------------------------------------
# Forecast API endpoint
# ---------------------------------------------------------------------------

from fastapi import HTTPException


@app.get("/api/forecasts/{indicator_id}", response_model=ForecastResult)
async def api_get_forecast(indicator_id: str):
    """Generate and return a probabilistic forecast for the given indicator.

    Steps:
      1. Verify the indicator exists in the indicators table (404 if not).
      2. Generate forecast via generate_forecast(); return 400 on insufficient data.
      3. Compute feature importance and generate + store explanation text.
      4. Return ForecastResult-compatible JSON.
    """
    # 1. Check indicator exists
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM indicators WHERE indicator_id = ?",
            (indicator_id,),
        )
        row = await cursor.fetchone()
        count = row[0] if row else 0

    if count == 0:
        raise HTTPException(
            status_code=404,
            detail=f"Indicator '{indicator_id}' not found. "
                   "Ingest data for this indicator before requesting a forecast.",
        )

    # 2. Generate forecast
    forecast_result = await generate_forecast(indicator_id)

    if "error" in forecast_result:
        raise HTTPException(status_code=400, detail=forecast_result["error"])

    forecast_id: int = forecast_result["forecast_id"]
    periods: list[dict] = forecast_result["periods"]

    # 3. Compute feature importance, generate explanation, persist it
    features = await compute_feature_importance(indicator_id, forecast_id)
    explanation_text = await generate_explanation_text(indicator_id, features)
    await store_explanation_text(forecast_id, explanation_text)

    # 4. Return combined result
    return {
        "forecast_id": forecast_id,
        "indicator_id": indicator_id,
        "periods": periods,
        "explanation": explanation_text,
        "features": features,
    }


# ---------------------------------------------------------------------------
# LLM Service — Task 5.1
# ---------------------------------------------------------------------------

_AGENT_ROLE_INSTRUCTIONS = {
    "Household": (
        "You represent a household making economic decisions. "
        "Focus on consumption decisions, savings, and labor supply. "
        "Reference income levels, inflation expectations, and credit availability in your reasoning."
    ),
    "Firm": (
        "You represent a firm making business decisions. "
        "Focus on pricing strategy, hiring plans, and investment decisions. "
        "Reference demand expectations, cost of capital, and input costs in your reasoning."
    ),
    "Bank": (
        "You represent a bank managing its balance sheet and lending activities. "
        "Focus on lending standards, credit volumes, and deposit rates. "
        "Reference default risk, capital ratios, and funding costs in your reasoning."
    ),
    "Policymaker": (
        "You represent a central bank or fiscal policymaker. "
        "Focus on the policy rate and fiscal stance. "
        "Reference Taylor-rule-style heuristics, the output gap, and the inflation target in your reasoning."
    ),
}


def _build_agent_prompt(agent_type: str, context: dict) -> str:
    """Build a structured prompt for the given agent type and context.

    Args:
        agent_type: One of 'Household', 'Firm', 'Bank', 'Policymaker'.
        context: Dict with keys 'indicators' (dict) and 'shock' (dict or None).

    Returns:
        Prompt string to send to the LLM.
    """
    indicators: dict = context.get("indicators", {})
    shock: dict | None = context.get("shock", None)

    role_instruction = _AGENT_ROLE_INSTRUCTIONS.get(
        agent_type,
        f"You are a {agent_type} agent in an economic simulation.",
    )

    # Format indicator lines
    indicator_lines = []
    label_map = {
        "CPIAUCSL": "CPI (FRED CPIAUCSL)",
        "UNRATE": "Unemployment Rate (FRED UNRATE)",
        "FEDFUNDS": "Fed Funds Rate (FRED FEDFUNDS)",
        "DGS10": "10-Year Treasury Yield (FRED DGS10)",
        "GDP_GROWTH": "GDP Growth (BEA NIPA)",
    }
    for key, value in indicators.items():
        label = label_map.get(key, key)
        indicator_lines.append(f"- {label}: {value}")

    indicators_section = (
        "\n".join(indicator_lines) if indicator_lines else "- No indicator data available."
    )

    # Format shock section
    if shock:
        shock_section = (
            f"Active Shock: {shock.get('variable', 'unknown')} changed by "
            f"{shock.get('magnitude', 0)}% for {shock.get('duration', 1)} quarter(s)."
        )
    else:
        shock_section = "No active shock — baseline conditions."

    prompt = (
        f"You are a {agent_type} agent in an economic simulation.\n\n"
        f"{role_instruction}\n\n"
        f"Current Economic Indicators:\n{indicators_section}\n\n"
        f"{shock_section}\n\n"
        f"Based on your role as a {agent_type}, respond with ONLY valid JSON (no markdown, "
        f"no code blocks, no extra text):\n"
        f'{{\n'
        f'  "beliefs": {{ "key economic beliefs as key-value pairs" }},\n'
        f'  "action": "your chosen action as a concise string",\n'
        f'  "rationale": "explanation referencing specific indicator values and sources"\n'
        f'}}'
    )
    return prompt


def _parse_llm_json(text: str) -> dict:
    """Parse JSON from LLM response text, handling markdown code block wrappers.

    Args:
        text: Raw text from the LLM response.

    Returns:
        Parsed dict with 'beliefs', 'action', 'rationale' keys.

    Raises:
        ValueError: If JSON cannot be parsed.
    """
    import re

    stripped = text.strip()

    # Strip markdown code fences if present (```json ... ``` or ``` ... ```)
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()

    # Find the first { ... } block in case there's surrounding prose
    brace_match = re.search(r"\{[\s\S]*\}", stripped)
    if brace_match:
        stripped = brace_match.group(0)

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse LLM JSON response: {exc}\nRaw text: {text!r}") from exc

    return parsed


async def invoke_agent(agent_type: str, context: dict) -> dict:
    """Call AWS Bedrock Claude to generate an agent response.

    Args:
        agent_type: One of 'Household', 'Firm', 'Bank', 'Policymaker'.
        context: Dict with keys:
            - 'indicators': dict of indicator_id -> float value
            - 'shock': dict with 'variable', 'magnitude', 'duration' keys, or None

    Returns:
        Dict with keys 'beliefs' (dict), 'action' (str), 'rationale' (str).

    Raises:
        Exception: If the Bedrock call fails (caller handles fallback).
    """
    import asyncio
    from botocore.config import Config

    prompt = _build_agent_prompt(agent_type, context)

    def _call_bedrock():
        bedrock = boto3.client(
            "bedrock-runtime",
            region_name=AWS_REGION,
            config=Config(read_timeout=30, connect_timeout=5, retries={"max_attempts": 1}),
        )
        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            }),
            contentType="application/json",
        )
        result = json.loads(response["body"].read())
        return result["content"][0]["text"]

    text = await asyncio.to_thread(_call_bedrock)
    parsed = _parse_llm_json(text)

    # Ensure required keys are present with sensible defaults
    return {
        "beliefs": parsed.get("beliefs", {}),
        "action": parsed.get("action", ""),
        "rationale": parsed.get("rationale", ""),
    }


# ---------------------------------------------------------------------------
# Simulation Engine — Task 5.2
# ---------------------------------------------------------------------------

# Agent interaction order per simulation step (design doc §3)
_AGENT_ORDER = ["Policymaker", "Bank", "Firm", "Household"]

# Mapping from indicator DB keys to the shock variable names used in ShockSpecification
_SHOCK_INDICATOR_MAP = {
    "interest_rate": "FEDFUNDS",
    "cpi": "CPIAUCSL",
    "unemployment": "UNRATE",
    "gdp_growth": "GDP_GROWTH",
    "energy_price": "CPIAUCSL",   # proxy via CPI
    "oil_price": "CPIAUCSL",      # proxy via CPI
    "tax_rate": "GDP_GROWTH",     # proxy via GDP
    "government_spending": "GDP_GROWTH",
    "trade_tariff": "CPIAUCSL",
}


def _apply_shock_to_indicators(
    indicators: dict,
    shock: dict,
    period: int,
) -> dict:
    """Return a copy of indicators with the shock effect applied for the given period.

    The shock magnitude is applied proportionally: the mapped indicator is
    shifted by magnitude * (1 / duration) per period so the full effect
    accumulates linearly over the shock duration.

    Args:
        indicators: Current indicator values dict.
        shock: Dict with 'variable', 'magnitude', 'duration'.
        period: 1-based period index within the shock duration.

    Returns:
        New dict with updated indicator values.
    """
    updated = dict(indicators)
    variable = shock.get("variable", "")
    magnitude = float(shock.get("magnitude", 0.0))
    duration = int(shock.get("duration", 1))

    target_key = _SHOCK_INDICATOR_MAP.get(variable)
    if target_key and target_key in updated:
        # Proportional per-period effect
        per_period_delta = magnitude / duration
        updated[target_key] = updated[target_key] + per_period_delta

    return updated


def _fallback_agent_response(agent_type: str, shock: dict) -> dict:
    """Return a heuristic fallback response when the LLM call fails.

    Heuristics (from design doc):
      - Household:   Reduce consumption by shock_magnitude * 0.3%, maintain savings
      - Firm:        Raise prices by shock_magnitude * 0.5%, freeze hiring
      - Bank:        Tighten lending standards by shock_magnitude * 0.2%
      - Policymaker: Adjust rate by shock_magnitude * 0.1% (Taylor-rule approximation)

    Args:
        agent_type: One of 'Household', 'Firm', 'Bank', 'Policymaker'.
        shock: Dict with 'variable', 'magnitude', 'duration'.

    Returns:
        Dict with 'beliefs', 'action', 'rationale'.
    """
    magnitude = float(shock.get("magnitude", 0.0))
    variable = shock.get("variable", "unknown")

    if agent_type == "Household":
        consumption_change = magnitude * 0.3
        return {
            "beliefs": {
                "economic_outlook": "cautious",
                "consumption_adjustment_pct": -consumption_change,
            },
            "action": f"Reduce consumption by {consumption_change:.2f}%, maintain savings",
            "rationale": (
                f"Heuristic fallback: {variable} shock of {magnitude}% leads household "
                f"to reduce consumption by {consumption_change:.2f}% while maintaining savings."
            ),
        }
    elif agent_type == "Firm":
        price_change = magnitude * 0.5
        return {
            "beliefs": {
                "demand_outlook": "uncertain",
                "price_adjustment_pct": price_change,
                "hiring_status": "frozen",
            },
            "action": f"Raise prices by {price_change:.2f}%, freeze hiring",
            "rationale": (
                f"Heuristic fallback: {variable} shock of {magnitude}% leads firm "
                f"to raise prices by {price_change:.2f}% and freeze hiring."
            ),
        }
    elif agent_type == "Bank":
        lending_tightening = magnitude * 0.2
        return {
            "beliefs": {
                "credit_risk": "elevated",
                "lending_standard_tightening_pct": lending_tightening,
            },
            "action": f"Tighten lending standards by {lending_tightening:.2f}%",
            "rationale": (
                f"Heuristic fallback: {variable} shock of {magnitude}% leads bank "
                f"to tighten lending standards by {lending_tightening:.2f}%."
            ),
        }
    else:  # Policymaker
        rate_adjustment = magnitude * 0.1
        return {
            "beliefs": {
                "inflation_risk": "moderate",
                "rate_adjustment_pct": rate_adjustment,
            },
            "action": f"Adjust policy rate by {rate_adjustment:.2f}% (Taylor-rule approximation)",
            "rationale": (
                f"Heuristic fallback: {variable} shock of {magnitude}% triggers "
                f"Taylor-rule rate adjustment of {rate_adjustment:.2f}%."
            ),
        }


def _aggregate_macro_outcomes(
    agent_responses: list[dict],
    base_indicators: dict,
    shock: dict,
) -> dict:
    """Aggregate agent actions into macro-level outcome estimates.

    Derives inflation, gdp_growth, and unemployment changes from agent
    responses using simple additive heuristics grounded in the shock
    magnitude and agent-specific multipliers.

    Args:
        agent_responses: List of dicts, each with 'agent_type', 'beliefs', 'action', 'rationale'.
        base_indicators: Current indicator values (after shock applied).
        shock: Dict with 'variable', 'magnitude', 'duration'.

    Returns:
        Dict with 'inflation', 'gdp_growth', 'unemployment'.
    """
    magnitude = float(shock.get("magnitude", 0.0))

    # Base values from indicators (with fallbacks)
    base_inflation = base_indicators.get("CPIAUCSL", 3.0)
    base_gdp = base_indicators.get("GDP_GROWTH", 2.0)
    base_unemployment = base_indicators.get("UNRATE", 4.0)

    # Accumulate deltas from each agent's beliefs
    inflation_delta = 0.0
    gdp_delta = 0.0
    unemployment_delta = 0.0

    for resp in agent_responses:
        agent_type = resp.get("agent_type", "")
        beliefs = resp.get("beliefs", {})

        if agent_type == "Firm":
            # Firm price increases feed into inflation
            price_adj = beliefs.get("price_adjustment_pct", 0.0)
            if isinstance(price_adj, (int, float)):
                inflation_delta += float(price_adj) * 0.01  # scale to percentage points

        if agent_type == "Household":
            # Reduced consumption drags GDP
            consumption_adj = beliefs.get("consumption_adjustment_pct", 0.0)
            if isinstance(consumption_adj, (int, float)):
                gdp_delta += float(consumption_adj) * 0.02  # consumption → GDP multiplier

        if agent_type == "Bank":
            # Tighter lending reduces investment → lower GDP, higher unemployment
            tightening = beliefs.get("lending_standard_tightening_pct", 0.0)
            if isinstance(tightening, (int, float)):
                gdp_delta -= float(tightening) * 0.01
                unemployment_delta += float(tightening) * 0.005

        if agent_type == "Policymaker":
            # Rate hike dampens inflation but also GDP
            rate_adj = beliefs.get("rate_adjustment_pct", 0.0)
            if isinstance(rate_adj, (int, float)):
                inflation_delta -= float(rate_adj) * 0.05
                gdp_delta -= float(rate_adj) * 0.02

    return {
        "inflation": round(base_inflation + inflation_delta, 4),
        "gdp_growth": round(base_gdp + gdp_delta, 4),
        "unemployment": round(base_unemployment + unemployment_delta, 4),
    }


async def run_simulation(shock: dict, current_indicators: dict) -> dict:
    """Execute a multi-step agent simulation for the given shock.

    For each period in shock['duration']:
      1. Update indicator context with shock effects (proportional per-period).
      2. Query each agent via invoke_agent() in order: Policymaker → Bank → Firm → Household.
         Falls back to heuristic response if invoke_agent() raises.
      3. Aggregate agent actions into macro outcomes (inflation, GDP growth, unemployment).
      4. Store agent_states rows in SQLite (requires a scenario_id; stored under scenario_id=0
         when called standalone — the scenario API endpoint will use its own scenario_id).

    Args:
        shock: Dict with keys 'variable' (str), 'magnitude' (float), 'duration' (int).
        current_indicators: Dict with keys like CPIAUCSL, UNRATE, FEDFUNDS, DGS10,
                            GDP_GROWTH mapping to float values.

    Returns:
        {
            "periods": [
                {
                    "period": int,
                    "inflation": float,
                    "gdp_growth": float,
                    "unemployment": float,
                    "agents": [
                        {"agent_type": str, "beliefs": dict, "action": str, "rationale": str},
                        ...
                    ]
                },
                ...
            ]
        }
    """
    duration = int(shock.get("duration", 1))
    periods_output = []

    # We store agent_states under a temporary scenario_id=0 when called standalone.
    # The /api/scenarios endpoint creates a real scenario row and passes scenario_id separately.
    # To support both cases, run_simulation accepts an optional _scenario_id kwarg via the
    # internal helper below; the public signature stays clean.
    scenario_id = shock.get("_scenario_id", 0)

    indicators = dict(current_indicators)

    for period in range(1, duration + 1):
        # 1. Apply shock effect for this period
        indicators = _apply_shock_to_indicators(indicators, shock, period)

        context = {"indicators": indicators, "shock": shock}

        # 2. Query all agents in parallel for speed
        import asyncio

        async def _call_agent(agent_type):
            try:
                response = await invoke_agent(agent_type, context)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "invoke_agent failed for %s (period %d): %s — using fallback heuristic",
                    agent_type, period, exc,
                )
                response = _fallback_agent_response(agent_type, shock)
            return {
                "agent_type": agent_type,
                "beliefs": response.get("beliefs", {}),
                "action": response.get("action", ""),
                "rationale": response.get("rationale", ""),
            }

        agent_results = await asyncio.gather(*[_call_agent(at) for at in _AGENT_ORDER])

        # 3. Aggregate macro outcomes
        macro = _aggregate_macro_outcomes(agent_results, indicators, shock)

        # 4. Store agent_states in SQLite
        async with get_db() as db:
            for ar in agent_results:
                await db.execute(
                    """
                    INSERT INTO agent_states
                        (scenario_id, period, agent_type, beliefs, action, rationale)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scenario_id,
                        period,
                        ar["agent_type"],
                        json.dumps(ar["beliefs"]),
                        ar["action"],
                        ar["rationale"],
                    ),
                )
            await db.commit()

        periods_output.append({
            "period": period,
            "inflation": macro["inflation"],
            "gdp_growth": macro["gdp_growth"],
            "unemployment": macro["unemployment"],
            "agents": agent_results,
        })

    return {"periods": periods_output}


async def get_current_indicators() -> dict:
    """Fetch the latest value for each indicator from the DB.

    Returns:
        Dict mapping indicator_id -> latest float value.
        e.g. {"CPIAUCSL": 314.5, "UNRATE": 3.9, "FEDFUNDS": 5.33, "DGS10": 4.2, "GDP_GROWTH": 2.8}
    """
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT indicator_id, value
            FROM indicators
            WHERE (indicator_id, observation_date) IN (
                SELECT indicator_id, MAX(observation_date)
                FROM indicators
                GROUP BY indicator_id
            )
            """
        )
        rows = await cursor.fetchall()

    return {row["indicator_id"]: row["value"] for row in rows}


# ---------------------------------------------------------------------------
# Scenario API endpoints — Task 6.1
# ---------------------------------------------------------------------------


@app.get("/api/scenarios")
async def api_list_scenarios():
    """Return all scenarios in reverse chronological order.

    Returns:
        {"scenarios": [{"id": int, "shock_variable": str, "shock_magnitude": float,
                         "shock_duration": int, "created_at": str}]}
    """
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT id, shock_variable, shock_magnitude, shock_duration, created_at
            FROM scenarios
            ORDER BY created_at DESC
            """
        )
        rows = await cursor.fetchall()

    scenarios = [
        {
            "id": row["id"],
            "shock_variable": row["shock_variable"],
            "shock_magnitude": row["shock_magnitude"],
            "shock_duration": row["shock_duration"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    return {"scenarios": scenarios}


@app.post("/api/scenarios")
async def api_create_scenario(shock: ShockSpecification):
    """Create a new scenario by running a simulation with the given shock.

    Steps:
      1. Pydantic validates ShockSpecification (recognized variable, positive duration) → 422 on failure.
      2. Fetch current indicators from DB.
      3. Store scenario definition in scenarios table to get scenario_id.
      4. Run simulation with the shock and current indicators.
      5. Generate counterfactual baseline from latest forecast data.
      6. Store trajectory + counterfactual in scenario_trajectories table.
      7. Return ScenarioResult-compatible JSON.

    Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 12.4, 12.5, 12.8, 12.9
    """
    # 1. Get current indicators
    indicators = await get_current_indicators()

    # 2. Store scenario definition to get scenario_id
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO scenarios (shock_variable, shock_magnitude, shock_duration)
            VALUES (?, ?, ?)
            """,
            (shock.variable, shock.magnitude, shock.duration),
        )
        scenario_id = cursor.lastrowid
        await db.commit()

    # 3. Run simulation (pass _scenario_id so agent_states are stored under this scenario)
    shock_dict = shock.model_dump()
    shock_dict["_scenario_id"] = scenario_id
    sim_result = await run_simulation(shock_dict, indicators)

    # 4. Generate counterfactual baseline from latest forecast data
    counterfactual = await _generate_counterfactual(shock.duration, indicators)

    # 5. Build trajectory from simulation periods
    trajectory: list[dict] = []
    all_agents: list[dict] = []

    for period_data in sim_result.get("periods", []):
        trajectory.append({
            "period": period_data["period"],
            "inflation": period_data["inflation"],
            "gdp_growth": period_data["gdp_growth"],
            "unemployment": period_data["unemployment"],
        })
        # Collect agents from the last period for the response
        for agent in period_data.get("agents", []):
            all_agents.append(agent)

    # 6. Store trajectory + counterfactual in scenario_trajectories table
    async with get_db() as db:
        for i, (traj_period, cf_period) in enumerate(zip(trajectory, counterfactual)):
            period_num = traj_period["period"]
            for indicator_name in ("inflation", "gdp_growth", "unemployment"):
                shock_val = traj_period[indicator_name]
                cf_val = cf_period[indicator_name]
                delta = round(shock_val - cf_val, 6)
                await db.execute(
                    """
                    INSERT INTO scenario_trajectories
                        (scenario_id, period, indicator_name, shock_value,
                         counterfactual_value, delta)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (scenario_id, period_num, indicator_name, shock_val, cf_val, delta),
                )
        await db.commit()

    # 7. Deduplicate agents — keep only the last period's agents for the response
    last_period_agents = []
    if sim_result.get("periods"):
        last_period = sim_result["periods"][-1]
        for agent in last_period.get("agents", []):
            last_period_agents.append({
                "agent_type": agent["agent_type"],
                "beliefs": agent["beliefs"],
                "action": agent["action"],
                "rationale": agent["rationale"],
            })

    # Build counterfactual as TrajectoryPeriod-compatible dicts
    cf_trajectory = [
        {
            "period": cf["period"],
            "inflation": cf["inflation"],
            "gdp_growth": cf["gdp_growth"],
            "unemployment": cf["unemployment"],
        }
        for cf in counterfactual
    ]

    return {
        "scenario_id": scenario_id,
        "shock": shock.model_dump(),
        "trajectory": trajectory,
        "counterfactual": cf_trajectory,
        "agents": last_period_agents,
    }


async def _generate_counterfactual(duration: int, current_indicators: dict) -> list[dict]:
    """Generate a counterfactual baseline trajectory (no-shock path).

    Tries to use generate_forecast for each macro indicator. Falls back to
    flat baseline from current indicator values if forecast is unavailable.

    Args:
        duration: Number of periods to generate.
        current_indicators: Dict of indicator_id -> latest value.

    Returns:
        List of dicts with keys: period, inflation, gdp_growth, unemployment.
    """
    # Map macro outcome names to indicator IDs for forecast lookup
    macro_indicator_map = {
        "inflation": "CPIAUCSL",
        "gdp_growth": "GDP_GROWTH",
        "unemployment": "UNRATE",
    }

    # Try to get forecast-based baselines
    baselines: dict[str, list[float]] = {}

    for macro_name, indicator_id in macro_indicator_map.items():
        try:
            forecast_result = await generate_forecast(indicator_id, periods=duration)
            if "error" not in forecast_result and forecast_result.get("periods"):
                baselines[macro_name] = [
                    p["p50"] for p in forecast_result["periods"]
                ]
            else:
                # Forecast failed — use flat baseline from current value
                base_val = current_indicators.get(indicator_id, 0.0)
                baselines[macro_name] = [base_val] * duration
        except Exception:  # noqa: BLE001
            base_val = current_indicators.get(indicator_id, 0.0)
            baselines[macro_name] = [base_val] * duration

    counterfactual = []
    for i in range(duration):
        counterfactual.append({
            "period": i + 1,
            "inflation": round(baselines["inflation"][i], 4),
            "gdp_growth": round(baselines["gdp_growth"][i], 4),
            "unemployment": round(baselines["unemployment"][i], 4),
        })

    return counterfactual


@app.get("/api/scenarios/{scenario_id}")
async def api_get_scenario(scenario_id: int):
    """Retrieve a scenario by ID with its trajectory, counterfactual, and agent states.

    Steps:
      1. Fetch scenario from scenarios table by id → 404 if not found.
      2. Fetch trajectory from scenario_trajectories table.
      3. Fetch agent states from agent_states table.
      4. Return the full scenario result.

    Requirements: 4.4, 4.5, 12.5, 12.8
    """
    # 1. Fetch scenario definition
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, shock_variable, shock_magnitude, shock_duration FROM scenarios WHERE id = ?",
            (scenario_id,),
        )
        scenario_row = await cursor.fetchone()

    if not scenario_row:
        raise HTTPException(
            status_code=404,
            detail=f"Scenario with id {scenario_id} not found.",
        )

    shock = {
        "variable": scenario_row["shock_variable"],
        "magnitude": scenario_row["shock_magnitude"],
        "duration": scenario_row["shock_duration"],
    }

    # 2. Fetch trajectory and counterfactual from scenario_trajectories
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT period, indicator_name, shock_value, counterfactual_value, delta
            FROM scenario_trajectories
            WHERE scenario_id = ?
            ORDER BY period ASC, indicator_name ASC
            """,
            (scenario_id,),
        )
        traj_rows = await cursor.fetchall()

    # Pivot rows into per-period trajectory and counterfactual dicts
    trajectory_map: dict[int, dict] = {}
    counterfactual_map: dict[int, dict] = {}

    for row in traj_rows:
        period = row["period"]
        name = row["indicator_name"]

        if period not in trajectory_map:
            trajectory_map[period] = {"period": period, "inflation": 0.0, "gdp_growth": 0.0, "unemployment": 0.0}
        if period not in counterfactual_map:
            counterfactual_map[period] = {"period": period, "inflation": 0.0, "gdp_growth": 0.0, "unemployment": 0.0}

        trajectory_map[period][name] = row["shock_value"]
        counterfactual_map[period][name] = row["counterfactual_value"]

    trajectory = [trajectory_map[p] for p in sorted(trajectory_map.keys())]
    counterfactual = [counterfactual_map[p] for p in sorted(counterfactual_map.keys())]

    # 3. Fetch agent states
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT agent_type, beliefs, action, rationale, period
            FROM agent_states
            WHERE scenario_id = ?
            ORDER BY period ASC
            """,
            (scenario_id,),
        )
        agent_rows = await cursor.fetchall()

    # Return agents from the last period only (consistent with POST response)
    last_period_num = shock["duration"]
    agents = []
    for row in agent_rows:
        if row["period"] == last_period_num:
            beliefs = row["beliefs"]
            if isinstance(beliefs, str):
                try:
                    beliefs = json.loads(beliefs)
                except (json.JSONDecodeError, TypeError):
                    beliefs = {}
            agents.append({
                "agent_type": row["agent_type"],
                "beliefs": beliefs,
                "action": row["action"],
                "rationale": row["rationale"],
            })

    return {
        "scenario_id": scenario_id,
        "shock": shock,
        "trajectory": trajectory,
        "counterfactual": counterfactual,
        "agents": agents,
    }


# ---------------------------------------------------------------------------
# Alert Detection — Task 6.2
# ---------------------------------------------------------------------------


async def check_alerts(indicator_id: str, observed_value: float) -> dict | None:
    """Compare an observed value against the most recent forecast p10/p90 bands.

    Fetches the latest horizon_period=1 forecast row for the indicator.
    If the observed value falls outside [p10, p90] (strictly), generates an
    Alert with severity and optional driver attribution / narrative.

    Args:
        indicator_id: The indicator to check.
        observed_value: The newly observed value.

    Returns:
        Alert dict if outside bands, None if within bands or no forecast exists.
    """
    # 1. Fetch the most recent forecast for this indicator (horizon_period=1)
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT id, p10, p50, p90
            FROM forecasts
            WHERE indicator_id = ? AND horizon_period = 1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (indicator_id,),
        )
        row = await cursor.fetchone()

    if not row:
        return None

    p10 = row["p10"]
    p90 = row["p90"]

    # 2. Check if observed value is within [p10, p90] (inclusive) → no alert
    if p10 <= observed_value <= p90:
        return None

    # 3. Determine severity
    band_width = p90 - p10
    if band_width <= 0:
        band_width = 1e-9  # avoid division by zero

    if observed_value < p10:
        distance = p10 - observed_value
    else:
        distance = observed_value - p90

    severity = "critical" if distance > 2 * band_width else "warning"

    # 4. Try to generate driver attribution and narrative
    driver_attribution: list[dict] | None = None
    narrative: str | None = None
    try:
        # Use a simple feature list for explanation
        features = [
            {"feature_name": indicator_id, "importance_score": 1.0, "direction": "positive"}
        ]
        narrative = await generate_explanation_text(indicator_id, features)
        driver_attribution = features
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to generate alert explanation for %s: %s", indicator_id, exc
        )

    # 5. Store alert in DB
    driver_json = json.dumps(driver_attribution) if driver_attribution else None
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO alerts
                (indicator_id, observed_value, p10_value, p90_value,
                 severity, driver_attribution, narrative)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (indicator_id, observed_value, p10, p90, severity, driver_json, narrative),
        )
        alert_id = cursor.lastrowid
        await db.commit()

        # Fetch the created_at timestamp
        cursor = await db.execute(
            "SELECT created_at FROM alerts WHERE id = ?", (alert_id,)
        )
        alert_row = await cursor.fetchone()
        created_at = alert_row["created_at"] if alert_row else ""

    return {
        "id": alert_id,
        "indicator_id": indicator_id,
        "observed_value": observed_value,
        "p10_value": p10,
        "p90_value": p90,
        "severity": severity,
        "driver_attribution": driver_attribution,
        "narrative": narrative,
        "created_at": created_at,
    }


async def _check_alerts_after_ingestion(indicator_id: str) -> None:
    """Check alerts for the most recently ingested observation of an indicator.

    Fetches the latest observation value from the indicators table and runs
    check_alerts against it. Any generated alert is logged.
    """
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT value FROM indicators
            WHERE indicator_id = ?
            ORDER BY observation_date DESC
            LIMIT 1
            """,
            (indicator_id,),
        )
        row = await cursor.fetchone()

    if not row:
        return

    observed_value = row["value"]
    alert = await check_alerts(indicator_id, observed_value)
    if alert:
        logger.info(
            "Alert generated for %s: severity=%s, observed=%s, p10=%s, p90=%s",
            indicator_id,
            alert["severity"],
            alert["observed_value"],
            alert["p10_value"],
            alert["p90_value"],
        )


# ---------------------------------------------------------------------------
# Alert & Agent State Retrieval Endpoints — Task 6.3
# ---------------------------------------------------------------------------


@app.get("/api/alerts")
async def api_get_alerts():
    """Return all alerts in reverse chronological order.

    Requirements: 10.1, 12.6
    """
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT id, indicator_id, observed_value, p10_value, p90_value,
                   severity, driver_attribution, narrative, created_at
            FROM alerts
            ORDER BY created_at DESC
            """
        )
        rows = await cursor.fetchall()

    alerts = []
    for row in rows:
        # Parse driver_attribution from JSON string
        driver_attribution = None
        raw_attr = row["driver_attribution"]
        if raw_attr:
            try:
                driver_attribution = json.loads(raw_attr)
            except (json.JSONDecodeError, TypeError):
                driver_attribution = None

        alerts.append({
            "id": row["id"],
            "indicator_id": row["indicator_id"],
            "observed_value": row["observed_value"],
            "p10_value": row["p10_value"],
            "p90_value": row["p90_value"],
            "severity": row["severity"],
            "driver_attribution": driver_attribution,
            "narrative": row["narrative"],
            "created_at": row["created_at"],
        })

    return {"alerts": alerts}


@app.get("/api/agents/{scenario_id}")
async def api_get_agents(scenario_id: int):
    """Return agent states for a given scenario (latest period only).

    Requirements: 10.1, 12.7, 12.8
    """
    # 1. Verify scenario exists
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM scenarios WHERE id = ?",
            (scenario_id,),
        )
        scenario_row = await cursor.fetchone()

    if not scenario_row:
        raise HTTPException(
            status_code=404,
            detail=f"Scenario with id {scenario_id} not found.",
        )

    # 2. Fetch agent states for the latest period of this scenario
    async with get_db() as db:
        # Find the max period for this scenario
        cursor = await db.execute(
            "SELECT MAX(period) as max_period FROM agent_states WHERE scenario_id = ?",
            (scenario_id,),
        )
        period_row = await cursor.fetchone()
        max_period = period_row["max_period"] if period_row else None

        if max_period is None:
            return {"agents": []}

        cursor = await db.execute(
            """
            SELECT agent_type, beliefs, action, rationale
            FROM agent_states
            WHERE scenario_id = ? AND period = ?
            ORDER BY id ASC
            """,
            (scenario_id, max_period),
        )
        rows = await cursor.fetchall()

    agents = []
    for row in rows:
        # Parse beliefs from JSON string
        beliefs = row["beliefs"]
        if isinstance(beliefs, str):
            try:
                beliefs = json.loads(beliefs)
            except (json.JSONDecodeError, TypeError):
                beliefs = {}

        agents.append({
            "agent_type": row["agent_type"],
            "beliefs": beliefs,
            "action": row["action"],
            "rationale": row["rationale"],
        })

    return {"agents": agents}
