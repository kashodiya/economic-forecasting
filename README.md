# eco4 — Economic Indicator Forecasting Dashboard

Interactive dashboard for analyzing and forecasting U.S. macroeconomic indicators using multiple statistical models, with AI-powered explanations via Amazon Bedrock.

![Python](https://img.shields.io/badge/Python-3.9+-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.111-green) ![License](https://img.shields.io/badge/License-MIT-yellow)

## What It Does

- Pulls real-time economic data from **FRED** (Federal Reserve Economic Data)
- Forecasts CPI, GDP, Unemployment, Fed Funds Rate, and PCE using multiple models
- Lets users run **what-if scenarios** (oil price shocks, rate changes, unemployment shifts)
- Generates **AI narrative explanations** using Claude via Amazon Bedrock
- Compares model performance with RMSE, AIC, and BIC metrics

## Forecasting Models

| Model | Description |
|-------|-------------|
| **SARIMAX** | Seasonal ARIMA — the classic time series workhorse |
| **ETS** | Holt-Winters exponential smoothing |
| **Prophet** | Meta's decomposable time series model |
| **VAR** | Vector Autoregression — models cross-indicator dynamics |
| **Ensemble** | Inverse-RMSE weighted average of all models |

Users can select one or more models and overlay their forecasts on the same chart for visual comparison.

## Quick Start

```bash
# Install dependencies
cd apps/eco4
pip install -r requirements.txt

# Set your FRED API key (get one at https://fred.stlouisfed.org/docs/api/api_key.html)
# Create a .env file:
echo "FRED_API_KEY=your_key_here" > .env

# Run the server
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in your browser.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FRED_API_KEY` | Yes | API key for FRED data access |
| `AWS_REGION` | No | AWS region for Bedrock (default: `us-east-1`) |
| `BEDROCK_MODEL` | No | Bedrock model ID for AI explanations |

## Tech Stack

- **Backend**: Python, FastAPI, statsmodels, Prophet
- **Frontend**: Vue 3, Vuetify 3, Plotly.js (single-page, no build step)
- **Data**: FRED REST API
- **AI**: Amazon Bedrock (Claude) for narrative explanations
- **Infra**: Terraform, EC2, S3

## Project Structure

```
economic-forecasting/
├── apps/eco4/           # Main application
│   ├── app.py           # FastAPI backend with all forecasting models
│   ├── templates/       # Frontend (index.html)
│   ├── static/          # Static assets
│   └── requirements.txt
├── terraform/           # Infrastructure as code
├── lambda/              # Dashboard Lambda (EC2 start/stop)
├── scripts/             # EC2 setup scripts
├── config/              # App configuration
└── docs/                # Design documents
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/indicators` | List available economic indicators |
| `GET /api/models` | List available forecasting models |
| `GET /api/forecast/{indicator}?models=SARIMAX,ETS&periods=12` | Run forecasts |
| `GET /api/explain/{indicator}` | AI-generated explanation |
| `GET /api/data/{indicator}` | Raw historical data |

## Screenshots

*Dashboard with multi-model forecast comparison, scenario sliders, and AI narrative.*

## License

MIT
