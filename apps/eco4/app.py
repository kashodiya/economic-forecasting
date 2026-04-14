from __future__ import annotations

import os
import json
import traceback
from datetime import datetime
from typing import List

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import numpy as np
import pandas as pd
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from statsmodels.tsa.statespace.sarimax import SARIMAX
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from statsmodels.tsa.api import VAR as VARModel

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="eco4")

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-20250514-v1:0")

INDICATORS = {
    "CPI":      {"series": "CPIAUCSL", "name": "Consumer Price Index (CPI)",   "unit": "Index 1982-84=100", "freq": "MS"},
    "GDP":      {"series": "GDP",      "name": "Gross Domestic Product (GDP)", "unit": "Billions $",        "freq": "QS"},
    "UNRATE":   {"series": "UNRATE",   "name": "Unemployment Rate",            "unit": "%",                 "freq": "MS"},
    "FEDFUNDS": {"series": "FEDFUNDS", "name": "Federal Funds Rate",           "unit": "%",                 "freq": "MS"},
    "PCE":      {"series": "PCEPI",    "name": "PCE Price Index",              "unit": "Index 2017=100",    "freq": "MS"},
}

AVAILABLE_MODELS = {
    "SARIMAX":  {"name": "SARIMAX",              "description": "Seasonal ARIMA with exogenous variables"},
    "ETS":      {"name": "Exponential Smoothing", "description": "Holt-Winters triple exponential smoothing"},
    "Prophet":  {"name": "Prophet",               "description": "Meta's decomposable time series model"},
    "VAR":      {"name": "VAR",                   "description": "Vector Autoregression — models cross-indicator dynamics"},
    "Ensemble": {"name": "Ensemble",              "description": "Weighted average of all available models"},
}

ELASTICITIES = {
    "OIL_PRICE": {"CPI": 0.04, "GDP": -0.01, "PCE": 0.03},
    "UNRATE":    {"GDP": -0.02, "CPI": -0.01},
    "FEDFUNDS":  {"GDP": -0.015, "CPI": -0.005, "UNRATE": 0.008},
}

_cache: dict[str, pd.DataFrame] = {}


# ---------------------------------------------------------------------------
# FRED data helpers
# ---------------------------------------------------------------------------

async def _fetch_fred(series_id: str, start: str = "2000-01-01") -> pd.DataFrame:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(FRED_BASE, params={
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "observation_start": start,
            "sort_order": "asc",
        })
        resp.raise_for_status()
        obs = resp.json().get("observations", [])

    rows = [{"date": o["date"], "value": float(o["value"])}
            for o in obs if o["value"] != "."]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df


async def _get_series(indicator_key: str) -> pd.DataFrame:
    if indicator_key in _cache:
        return _cache[indicator_key]
    meta = INDICATORS[indicator_key]
    df = await _fetch_fred(meta["series"])
    _cache[indicator_key] = df
    return df


def _prepare_ts(df: pd.DataFrame, freq: str) -> pd.Series:
    """Convert DataFrame to a clean time series."""
    ts = df.set_index("date")["value"].asfreq(freq).ffill()
    return ts


def _calc_rmse(ts: pd.Series, n_test: int = 12) -> float:
    """Walk-forward RMSE on last n_test points (quick approximation)."""
    if len(ts) < n_test + 24:
        return float("nan")
    train, test = ts.iloc[:-n_test], ts.iloc[-n_test:]
    # Simple one-step naive forecast for baseline comparison
    return float(np.sqrt(np.mean((test.values - train.iloc[-n_test:].values) ** 2)))


# ---------------------------------------------------------------------------
# Forecasting models
# ---------------------------------------------------------------------------

def _forecast_sarimax(ts: pd.Series, periods: int, freq: str) -> dict:
    """SARIMAX(1,1,1)(1,1,0,s) with fallback."""
    seasonal_period = 4 if freq == "QS" else 12
    try:
        model = SARIMAX(ts, order=(1, 1, 1),
                        seasonal_order=(1, 1, 0, seasonal_period),
                        enforce_stationarity=False, enforce_invertibility=False)
        res = model.fit(disp=False, maxiter=200)
    except Exception:
        model = SARIMAX(ts, order=(1, 1, 0),
                        enforce_stationarity=False, enforce_invertibility=False)
        res = model.fit(disp=False, maxiter=200)

    fc = res.get_forecast(steps=periods)
    ci = fc.conf_int(alpha=0.2)

    # In-sample metrics
    aic = round(res.aic, 2)
    bic = round(res.bic, 2)
    fitted = res.fittedvalues.dropna()
    aligned = ts.loc[fitted.index]
    rmse = round(float(np.sqrt(np.mean((aligned - fitted) ** 2))), 4)

    return {
        "forecast": pd.DataFrame({
            "date": fc.predicted_mean.index,
            "predicted": fc.predicted_mean.values,
            "lower": ci.iloc[:, 0].values,
            "upper": ci.iloc[:, 1].values,
        }),
        "metrics": {"aic": aic, "bic": bic, "rmse": rmse},
    }


def _forecast_ets(ts: pd.Series, periods: int, freq: str) -> dict:
    """Holt-Winters Exponential Smoothing."""
    seasonal_period = 4 if freq == "QS" else 12
    try:
        model = ExponentialSmoothing(
            ts, trend="add", seasonal="add",
            seasonal_periods=seasonal_period,
            initialization_method="estimated",
        )
        res = model.fit(optimized=True)
    except Exception:
        # Fallback: no seasonality
        model = ExponentialSmoothing(ts, trend="add", initialization_method="estimated")
        res = model.fit(optimized=True)

    fc_vals = res.forecast(periods)
    # Approximate confidence bands using residual std
    resid_std = float(np.std(res.resid.dropna()))
    z80 = 1.28  # 80% CI

    dates = fc_vals.index
    predicted = fc_vals.values
    widening = np.sqrt(np.arange(1, periods + 1))
    lower = predicted - z80 * resid_std * widening
    upper = predicted + z80 * resid_std * widening

    aic = round(res.aic, 2)
    bic = round(res.bic, 2)
    fitted = res.fittedvalues.dropna()
    aligned = ts.loc[fitted.index]
    rmse = round(float(np.sqrt(np.mean((aligned - fitted) ** 2))), 4)

    return {
        "forecast": pd.DataFrame({
            "date": dates, "predicted": predicted,
            "lower": lower, "upper": upper,
        }),
        "metrics": {"aic": aic, "bic": bic, "rmse": rmse},
    }


def _forecast_prophet(ts: pd.Series, periods: int, freq: str) -> dict:
    """Meta Prophet model."""
    try:
        from prophet import Prophet
    except ImportError:
        return {"forecast": pd.DataFrame(), "metrics": {"error": "Prophet not installed"}}

    pdf = pd.DataFrame({"ds": ts.index, "y": ts.values})
    m = Prophet(yearly_seasonality=True, interval_width=0.8)
    m.fit(pdf)

    future = m.make_future_dataframe(periods=periods, freq=freq)
    fc = m.predict(future)
    fc_out = fc.tail(periods)

    # Metrics from in-sample
    in_sample = fc.iloc[:len(ts)]
    rmse = round(float(np.sqrt(np.mean((ts.values - in_sample["yhat"].values) ** 2))), 4)

    return {
        "forecast": pd.DataFrame({
            "date": fc_out["ds"].values,
            "predicted": fc_out["yhat"].values,
            "lower": fc_out["yhat_lower"].values,
            "upper": fc_out["yhat_upper"].values,
        }),
        "metrics": {"aic": None, "bic": None, "rmse": rmse},
    }


async def _forecast_var(indicator_key: str, periods: int, freq: str) -> dict:
    """Vector Autoregression using all monthly indicators together."""
    # VAR only works with same-frequency series; use monthly ones
    monthly_keys = [k for k, v in INDICATORS.items() if v["freq"] == "MS"]
    if indicator_key not in monthly_keys:
        # For GDP (quarterly), fall back to SARIMAX
        return {"forecast": pd.DataFrame(), "metrics": {"error": "VAR requires monthly data; GDP is quarterly"}}

    # Fetch all monthly series
    series_dict = {}
    for k in monthly_keys:
        try:
            s_df = await _get_series(k)
            s_ts = _prepare_ts(s_df, "MS")
            series_dict[k] = s_ts
        except Exception:
            pass

    if len(series_dict) < 2:
        return {"forecast": pd.DataFrame(), "metrics": {"error": "Need at least 2 series for VAR"}}

    # Align all series to common date range
    combined = pd.DataFrame(series_dict).dropna()
    if len(combined) < 30:
        return {"forecast": pd.DataFrame(), "metrics": {"error": "Insufficient overlapping data for VAR"}}

    # Fit VAR
    try:
        model = VARModel(combined)
        # Select optimal lag order (max 12, use AIC)
        lag_order = model.select_order(maxlags=12)
        optimal_lag = lag_order.aic
        if optimal_lag < 1:
            optimal_lag = 2
        res = model.fit(optimal_lag)
    except Exception as e:
        return {"forecast": pd.DataFrame(), "metrics": {"error": f"VAR fit failed: {e}"}}

    # Forecast
    fc_array = res.forecast(combined.values[-res.k_ar:], steps=periods)
    fc_df = pd.DataFrame(fc_array, columns=combined.columns)

    # Generate dates
    last_date = combined.index[-1]
    fc_dates = pd.date_range(start=last_date, periods=periods + 1, freq="MS")[1:]

    # Extract the target indicator column
    target_col = indicator_key
    predicted = fc_df[target_col].values

    # Confidence bands from residual covariance
    resid_std = float(np.std(res.resid[target_col].dropna()))
    z80 = 1.28
    widening = np.sqrt(np.arange(1, periods + 1))
    lower = predicted - z80 * resid_std * widening
    upper = predicted + z80 * resid_std * widening

    # Metrics
    aic = round(float(res.aic), 2)
    bic = round(float(res.bic), 2)
    fitted = res.fittedvalues[target_col].dropna()
    aligned = combined[target_col].loc[fitted.index]
    rmse = round(float(np.sqrt(np.mean((aligned - fitted) ** 2))), 4)

    return {
        "forecast": pd.DataFrame({
            "date": fc_dates, "predicted": predicted,
            "lower": lower, "upper": upper,
        }),
        "metrics": {"aic": aic, "bic": bic, "rmse": rmse, "lag_order": int(optimal_lag)},
    }


def _forecast_ensemble(results: dict) -> dict:
    """Weighted average of all successful model forecasts."""
    valid = {k: v for k, v in results.items()
             if k != "Ensemble" and len(v["forecast"]) > 0}
    if not valid:
        return {"forecast": pd.DataFrame(), "metrics": {"error": "No models to ensemble"}}

    # Use inverse-RMSE weighting (lower RMSE = higher weight)
    weights = {}
    for k, v in valid.items():
        rmse = v["metrics"].get("rmse")
        if rmse and rmse > 0 and not np.isnan(rmse):
            weights[k] = 1.0 / rmse
        else:
            weights[k] = 1.0

    total_w = sum(weights.values())
    weights = {k: w / total_w for k, w in weights.items()}

    # Use the first model's dates as reference
    ref_key = list(valid.keys())[0]
    ref_fc = valid[ref_key]["forecast"]
    n = len(ref_fc)

    predicted = np.zeros(n)
    lower = np.zeros(n)
    upper = np.zeros(n)

    for k, v in valid.items():
        fc = v["forecast"]
        if len(fc) != n:
            continue
        w = weights[k]
        predicted += w * fc["predicted"].values
        lower += w * fc["lower"].values
        upper += w * fc["upper"].values

    # Ensemble RMSE: weighted average of component RMSEs
    ens_rmse = sum(weights[k] * valid[k]["metrics"].get("rmse", 0)
                   for k in valid if valid[k]["metrics"].get("rmse"))

    return {
        "forecast": pd.DataFrame({
            "date": ref_fc["date"].values,
            "predicted": predicted,
            "lower": lower,
            "upper": upper,
        }),
        "metrics": {"rmse": round(ens_rmse, 4), "weights": {k: round(w, 3) for k, w in weights.items()}},
    }


def _apply_scenario(indicator_key: str, forecast_df: pd.DataFrame, shocks: dict) -> pd.DataFrame:
    adj = forecast_df.copy()
    total = 0.0
    for shock_name, pct in shocks.items():
        total += ELASTICITIES.get(shock_name, {}).get(indicator_key, 0) * pct
    adj["predicted"] *= (1 + total)
    adj["lower"] *= (1 + total)
    adj["upper"] *= (1 + total)
    return adj


# ---------------------------------------------------------------------------
# Bedrock AI explanation
# ---------------------------------------------------------------------------

def _bedrock_explain(indicator: str, summary: dict, models_used: list = None, scenario: dict = None) -> str:
    try:
        import boto3
        client = boto3.client("bedrock-runtime", region_name=AWS_REGION)

        prompt_parts = [
            f"You are an economic analyst. Explain the following forecast for {indicator} in 3-4 sentences, using clear language suitable for economists and statisticians.",
            f"Latest value: {summary.get('latest_value')} ({summary.get('latest_date')})",
            f"Forecast next period: {summary.get('forecast_next')}",
            f"Forecast range: {summary.get('forecast_low')} to {summary.get('forecast_high')}",
            f"Trend over last 12 observations: {summary.get('trend')}",
        ]
        if models_used:
            prompt_parts.append(f"Models used: {', '.join(models_used)}")
            prompt_parts.append("Compare the models briefly if multiple were used.")
        if scenario:
            prompt_parts.append(f"Scenario shocks applied: {json.dumps(scenario)}")
            prompt_parts.append("Explain how the scenario shocks affect this indicator.")

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 400,
            "messages": [{"role": "user", "content": "\n".join(prompt_parts)}],
        })
        resp = client.invoke_model(modelId=BEDROCK_MODEL, body=body, contentType="application/json")
        result = json.loads(resp["body"].read())
        return result["content"][0]["text"]
    except Exception as e:
        return f"AI explanation unavailable: {e}"


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/api/indicators")
async def list_indicators():
    return [{"key": k, **v} for k, v in INDICATORS.items()]


@app.get("/api/models")
async def list_models():
    return [{"key": k, **v} for k, v in AVAILABLE_MODELS.items()]


@app.get("/api/data/{indicator}")
async def get_data(indicator: str):
    if indicator not in INDICATORS:
        return JSONResponse({"error": "Unknown indicator"}, 400)
    try:
        df = await _get_series(indicator)
        return {
            "indicator": indicator,
            "meta": INDICATORS[indicator],
            "data": df.to_dict(orient="records"),
        }
    except Exception as e:
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()}, 500)


@app.get("/api/forecast/{indicator}")
async def get_forecast(
    indicator: str,
    periods: int = Query(12, ge=1, le=48),
    models: str = Query("SARIMAX"),  # comma-separated: SARIMAX,ETS,Prophet,VAR,Ensemble
    shocks: str = Query("{}"),
):
    if indicator not in INDICATORS:
        return JSONResponse({"error": "Unknown indicator"}, 400)
    try:
        df = await _get_series(indicator)
        meta = INDICATORS[indicator]
        ts = _prepare_ts(df, meta["freq"])

        model_list = [m.strip() for m in models.split(",") if m.strip()]
        shock_dict = json.loads(shocks) if shocks else {}

        # Run each requested model
        model_results = {}
        for model_name in model_list:
            if model_name == "Ensemble":
                continue  # computed after others
            try:
                if model_name == "SARIMAX":
                    model_results["SARIMAX"] = _forecast_sarimax(ts, periods, meta["freq"])
                elif model_name == "ETS":
                    model_results["ETS"] = _forecast_ets(ts, periods, meta["freq"])
                elif model_name == "Prophet":
                    model_results["Prophet"] = _forecast_prophet(ts, periods, meta["freq"])
                elif model_name == "VAR":
                    model_results["VAR"] = await _forecast_var(indicator, periods, meta["freq"])
            except Exception as e:
                model_results[model_name] = {
                    "forecast": pd.DataFrame(),
                    "metrics": {"error": str(e)},
                }

        # Ensemble if requested
        if "Ensemble" in model_list and model_results:
            model_results["Ensemble"] = _forecast_ensemble(model_results)

        # Build response
        latest = df.iloc[-1]
        vals = df["value"].tail(12).tolist()
        trend = "rising" if vals[-1] > vals[0] else "falling" if vals[-1] < vals[0] else "flat"

        # Use first successful model for summary
        first_fc = None
        for r in model_results.values():
            if len(r["forecast"]) > 0:
                first_fc = r["forecast"]
                break

        summary = {
            "latest_value": round(latest["value"], 2),
            "latest_date": latest["date"].strftime("%Y-%m-%d"),
            "forecast_next": round(first_fc["predicted"].iloc[0], 2) if first_fc is not None else None,
            "forecast_low": round(first_fc["lower"].iloc[0], 2) if first_fc is not None else None,
            "forecast_high": round(first_fc["upper"].iloc[0], 2) if first_fc is not None else None,
            "trend": trend,
        }

        # Serialize model results
        forecasts_out = {}
        metrics_out = {}
        scenario_out = {}
        for mname, mres in model_results.items():
            fc_df = mres["forecast"]
            # Sanitize metrics: convert numpy types to native Python
            raw_metrics = mres["metrics"]
            clean_metrics = {}
            for mk, mv in raw_metrics.items():
                if isinstance(mv, (np.integer,)):
                    clean_metrics[mk] = int(mv)
                elif isinstance(mv, (np.floating,)):
                    clean_metrics[mk] = float(mv)
                elif isinstance(mv, dict):
                    clean_metrics[mk] = {str(dk): float(dv) if isinstance(dv, (np.floating, np.integer)) else dv for dk, dv in mv.items()}
                else:
                    clean_metrics[mk] = mv
            metrics_out[mname] = clean_metrics
            if len(fc_df) > 0:
                # Convert to native Python types for JSON serialization
                fc_records = []
                for _, row in fc_df.iterrows():
                    fc_records.append({
                        "date": str(row["date"]),
                        "predicted": float(row["predicted"]),
                        "lower": float(row["lower"]),
                        "upper": float(row["upper"]),
                    })
                forecasts_out[mname] = fc_records
                if shock_dict:
                    sc = _apply_scenario(indicator, fc_df, shock_dict)
                    sc_records = []
                    for _, row in sc.iterrows():
                        sc_records.append({
                            "date": str(row["date"]),
                            "predicted": float(row["predicted"]),
                            "lower": float(row["lower"]),
                            "upper": float(row["upper"]),
                        })
                    scenario_out[mname] = sc_records
            else:
                forecasts_out[mname] = []

        # Serialize historical data with native types
        hist_records = []
        for _, row in df.tail(60).iterrows():
            hist_records.append({
                "date": str(row["date"]),
                "value": float(row["value"]),
            })

        result = {
            "indicator": indicator,
            "meta": meta,
            "historical": hist_records,
            "forecasts": forecasts_out,
            "metrics": metrics_out,
            "summary": summary,
        }
        if scenario_out:
            result["scenarios"] = scenario_out

        return result
    except Exception as e:
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()}, 500)


@app.get("/api/explain/{indicator}")
async def explain(
    indicator: str,
    models: str = Query("SARIMAX"),
    shocks: str = Query("{}"),
):
    if indicator not in INDICATORS:
        return JSONResponse({"error": "Unknown indicator"}, 400)
    try:
        df = await _get_series(indicator)
        meta = INDICATORS[indicator]
        ts = _prepare_ts(df, meta["freq"])
        res = _forecast_sarimax(ts, 6, meta["freq"])
        fc = res["forecast"]

        latest = df.iloc[-1]
        vals = df["value"].tail(12).tolist()
        trend = "rising" if vals[-1] > vals[0] else "falling" if vals[-1] < vals[0] else "flat"
        summary = {
            "latest_value": round(latest["value"], 2),
            "latest_date": latest["date"].strftime("%Y-%m-%d"),
            "forecast_next": round(fc["predicted"].iloc[0], 2),
            "forecast_low": round(fc["lower"].iloc[0], 2),
            "forecast_high": round(fc["upper"].iloc[0], 2),
            "trend": trend,
        }
        shock_dict = json.loads(shocks) if shocks else {}
        model_list = [m.strip() for m in models.split(",") if m.strip()]
        text = _bedrock_explain(INDICATORS[indicator]["name"], summary, model_list, shock_dict or None)
        return {"indicator": indicator, "explanation": text, "summary": summary}
    except Exception as e:
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()}, 500)


@app.get("/api/clear-cache")
async def clear_cache():
    _cache.clear()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# SPA fallback — must be last
# ---------------------------------------------------------------------------
@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    return FileResponse(os.path.join(os.path.dirname(__file__), "templates", "index.html"))
