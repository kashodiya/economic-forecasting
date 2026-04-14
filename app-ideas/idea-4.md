## 1) Problem Reframing: Data + Beliefs

**Traditional** dashboards show historical data with a model forecast.  
**Novel Layer**: We add an “Expectations Track” derived from **real‑world narratives** (press releases, policy statements, sector news). This track expresses **beliefs** (e.g., “households expect inflation to be 3.0% next quarter”), which we then **fuse** with the data‑driven forecast. Viewers can **toggle** and **contrast** “data‑only” vs. “data+expectations.”

Key outcomes:
- **Expectation Priors** per indicator and horizon (1–4 quarters/months ahead).
- **Expectation Surprise Index (ESI)**: quantitative gap between model forecast and belief‑derived prior.
- **Scenario Sliders** for narrative counterfactuals (“hawkish Fed statement”, “energy price shock”, “fiscal stimulus pause”).

---

## 2) High-Level Architecture

```
+------------------------------+             +---------------------------+
| Public Sources               |             | AWS Ingestion             |
| - FRED API                   | --> S3 ---->| Glue (catalog + ETL)      |
| - BEA API                    |             | Lambda (pull schedulers)  |
| - Press Releases (RSS/HTML)  |             | EventBridge (schedules)   |
+------------------------------+             +---------------------------+
                                                       |
                                                       v
                                            +---------------------------+
                                            | Curated Data Lake (S3)    |
                                            | - econ_raw/               |
                                            | - econ_curated/           |
                                            | - narratives_raw/         |
                                            | - narratives_curated/     |
                                            +---------------------------+
                                                       |
                         +-----------------------------+------------------------------+
                         |                                                            |
                         v                                                            v
        +-----------------------------------+                         +-------------------------------------+
        | Forecasting (SageMaker/Forecast)  |                         | Expectations via Bedrock LLMs       |
        | - Feature engineering             |                         | - KB (S3 + OpenSearch Serverless)   |
        | - Model training & backtesting    |                         | - Prompt orchestration (Agents)     |
        | - Probabilistic forecasts (PI)    |                         | - Priors per indicator & horizon    |
        +-----------------------------------+                         +-------------------------------------+
                         |                                                            |
                         +-----------------------------+------------------------------+
                                                       |
                                                       v
                                            +---------------------------+
                                            | Fusion Layer              |
                                            | - Bayesian prior fusion   |
                                            | - ESI computation         |
                                            | - Scenario engine (VAR/IRF)|
                                            +---------------------------+
                                                       |
                                                       v
                                            +---------------------------+
                                            | Serving/Analytics         |
                                            | - Athena SQL              |
                                            | - OpenSearch (search)     |
                                            | - API Gateway + Lambda    |
                                            | - SNS alerts              |
                                            +---------------------------+
                                                       |
                                                       v
                                            +---------------------------+
                                            | Interactive Dashboard     |
                                            | - Time series views       |
                                            | - Expectations track      |
                                            | - What‑if scenarios       |
                                            | - GenAI explanations      |
                                            +---------------------------+
```

---

## 3) Data Ingestion & Curation

### 3.1 Economic Indicators (FRED + BEA)
- **Sources**: FRED (Federal Reserve Economic Data), BEA (US Bureau of Economic Analysis).
- **Access**: Pull via public APIs on schedules (EventBridge). Use Lambda for incremental updates (e.g., daily/weekly).
- **Storage**:  
  - `s3://<bucket>/econ_raw/` (source JSON/CSV)  
  - `s3://<bucket>/econ_curated/` (cleaned, standardized timeseries: indicator_id, date, value, unit, seasonal_adjustment, vintage_timestamp)
- **Schema (curated)**:
  - `indicator_id` (string, e.g., “CPI_U”)
  - `date` (ISO)
  - `value` (float)
  - `unit` (string)
  - `source` (enum: FRED/BEA)
  - `metadata` (JSON: frequency, seasonal adjustment, etc.)
  - `vintage_ts` (optional; for real‑time vintage analysis)

### 3.2 Narrative Corpus (Expectations Sources)
- **Sources**: Public press releases (Fed, Treasury, BEA), policy statements (FOMC statements, minutes), sector news (energy, labor, housing), reputable media.
- **Access**: RSS feeds + HTML scraping via Lambda (only publicly accessible pages), store raw HTML/text in `narratives_raw`.
- **Curation**:
  - Basic boilerplate removal, deduplication, language detection, timestamp normalization.
  - Metadata: `publisher`, `publish_date`, `topic_tags` (finance, labor, energy), `geo` (US), `doc_url`, `crawl_ts`.
  - Store at `narratives_curated/` (clean text + metadata).
- **Indexing**:
  - **OpenSearch Serverless** for full‑text search.
  - Bedrock Knowledge Base (KB) connected to S3 + OpenSearch for retrieval‑augmented generation (RAG).

---

## 4) Forecasting Stack (Data-Driven)

- **Modeling Options** (choose per indicator):
  - **Amazon Forecast** (ETS/ARIMA/Prophet-like plus DeepAR) for fast, managed time‑series forecasting with probabilistic outputs.
  - **SageMaker** for custom models (e.g., **DeepAR**, **Temporal Fusion Transformer (TFT)**, or classical **ARIMAX** using exogenous variables like oil prices, rates).
- **Feature Engineering**:
  - Frequency normalization (monthly/quarterly).
  - Transformations: log, differencing if needed.
  - Exogenous regressors: energy prices, policy rates, consumer sentiment, labor claims.
  - Seasonality flags (month/quarter dummies), holiday calendars.
- **Outputs**:
  - Point forecasts + prediction intervals (e.g., P10/P50/P90).
  - Backtesting metrics (MAPE, RMSE, CRPS) for calibration.
- **Explainability**:
  - Model‑level SHAP (where applicable, e.g., TFT in SageMaker).
  - Contribution charts per feature/time‑step.

---

## 5) Expectation Formation Layer (Novel GenAI)

### 5.1 Retrieval-Augmented Expectation Extraction
- **Prompting Strategy (Bedrock LLMs)**:
  - System: Domain role (macro analyst).  
  - Instruction: Summarize and **quantify** expectations for a specific indicator and horizon based on retrieved documents (with timestamps).  
  - Constraints: Use only retrieved evidence; return numeric central estimate + uncertainty (e.g., mean ± 95% CI) and a **confidence score** (0–1).
- **Outputs per Indicator/Horizon**:
  - `E[Indicator, horizon]` (e.g., CPI next quarter = 2.8%)  
  - `uncertainty_band` (e.g., ±0.4pp)  
  - `supporting_evidence` (top 3 doc snippets + URLs + publish_dates)  
  - `confidence` (e.g., 0.73)  
  - `stance` (e.g., hawkish/dovish for policy texts; inflationary/deflationary for sector narratives)

### 5.2 Calibration & Quality Controls
- **De‑biasing**:
  - Publisher weighting (e.g., official sources > general media).
  - Recency decay (more recent narratives carry more weight).
  - Diversity checks (avoid single‑source dominance).
- **Consistency Rules**:
  - Horizon alignment (monthly vs quarterly).
  - Unit harmonization (YoY vs QoQ vs SAAR).
- **Sanity Filters**:
  - Clip expectations to plausible ranges (e.g., CPI within historical bounds unless strong evidence).
  - Confidence down‑weight if evidence conflicts.

### 5.3 Expectation Priors as Bayesian Inputs
- Construct a **prior distribution** for each indicator/horizon:
  - Normal prior with mean = expectation, variance from uncertainty_band (mapped to σ²).
  - Convert to conjugate priors for models that support it (e.g., Bayesian ARIMA/VAR).
- If using Amazon Forecast (non‑Bayesian), use expectations as **exogenous signal**:
  - Feature: `expectation_prior_t+h` as a regressor for learning alignment.

---

## 6) Fusion Layer: Data + Expectations

### 6.1 Prior Fusion Strategies
- **Bayesian Posterior** (preferred where feasible):
  - Posterior = combine likelihood from the data model with prior from narratives.
  - Outputs: posterior mean/intervals; quantify **weight of evidence** (data vs. prior).
- **Weighted Blending** (when using managed services without custom Bayesian hooks):
  - Final forecast = `w * model_forecast + (1-w) * expectation_prior`, where `w` is learned via cross‑validation or dynamic rule:
    - Increase `w` when model calibration is good (recent backtests strong).
    - Decrease `w` when narratives have high confidence & recent structural breaks.
- **Expectation Surprise Index (ESI)**:
  - `ESI = model_forecast - expectation_prior` (normalized by joint uncertainty).
  - Use ESI to drive dashboard highlights and alerts.

### 6.2 Scenario Engine
- **Shock Types**:
  - **Policy Communication Shock**: Set stance to “hawkish/dovish” → adjust expectation prior, propagate to rates, then to inflation/unemployment via VAR impulse responses.
  - **Commodity Price Shock**: Oil +10% → pass-through to CPI/PPI; adjust exogenous features.
  - **Labor Shock**: Surprise in NFP → affect consumption/inflation expectations.
- **Mechanism**:
  - Lightweight **VAR** (or **Dynamic Factor Model**) trained in SageMaker.
  - Precomputed **Impulse Response Functions (IRFs)** for canonical shocks.
  - Scenario sliders manipulate shock size + duration; engine recomputes paths and updates charts.

---

## 7) Generative Explanations (Plain Language)

- **Audience‑friendly narratives** generated via Bedrock:
  - “**What the data says**” (summarize drivers from model explainability).
  - “**What people expect**” (summarize expectation priors with cited snippets).
  - “**Why they differ**” (ESI explanation with causes: policy tone, commodity prices, labor prints).
  - “**Risks & uncertainties**” (list 3 plausible risks with confidence).
- **Guardrails**:
  - Ground explanations in retrieved evidence; include **citations** (title, source, date).
  - Avoid speculative content beyond corpus; abstain when evidence weak.

---

## 8) Alerts & Monitoring

- **Deviation Alerts** (SNS/Email/Slack):
  - Trigger when `|ESI|` exceeds threshold or when posterior intervals widen > X%.
  - Trigger when realized values diverge from expectations beyond calibrated bounds.
- **Data Freshness & Quality Alerts**:
  - Glue data quality checks (nulls, duplicates, unit mismatches).
  - Lambda health (ingestion failures) → CloudWatch alarms.

---

## 9) Dashboard UX (What the User Sees)

### 9.1 Main Views
1) **Indicator Overview**  
   - Historical curve (actuals) with **forecast bands** (P10–P90).  
   - Toggle: **Data‑only vs Data+Expectations**.  
   - **Expectations Track** (point + uncertainty shaded band).  
   - **ESI meter** with color coding.

2) **Narrative Evidence Panel**  
   - Top 3 excerpts with source, date, stance tag; “View more” opens searchable corpus.

3) **Scenario Lab**  
   - Sliders: policy tone, oil shock, fiscal impulse, supply chain tightness.  
   - “Apply” recomputes paths (VAR IRFs) and updates posterior bands.

4) **Explainability Tab**  
   - SHAP driver chart (e.g., energy price contributes +0.3pp to CPI).  
   - GenAI summary with citations and confidence.

5) **Alerts & Changes**  
   - Log of alerts; clickable entries show the context and charts at alert time.

### 9.2 Interaction Design
- **Hover** on timeline to see data vs expectation gap at any date.  
- **Pin Scenarios** to compare side‑by‑side (“Baseline”, “Hawkish”, “Oil+10%”).  
- **Export** to CSV/PDF of charts and explanation summaries (with citations).  
- **Accessibility**: high‑contrast, keyboard navigation, descriptive alt‑texts.

---

## 10) AWS Services & Roles

- **Data Lake**: Amazon S3  
- **Metadata & ETL**: AWS Glue (crawler, jobs), AWS Lambda (ingestion), Amazon EventBridge (schedules)  
- **Search & RAG Indexing**: Amazon OpenSearch Serverless; Bedrock **Knowledge Bases** to connect S3 + OpenSearch  
- **LLMs**: AWS Bedrock (e.g., Claude / Amazon Titan) for expectation extraction + explanations  
- **Forecasting**: Amazon Forecast (managed) and/or Amazon SageMaker (custom models, VAR, TFT)  
- **Serving**: Amazon API Gateway + AWS Lambda for REST endpoints; Amazon Athena for SQL queries over S3  
- **Monitoring & Alerts**: Amazon CloudWatch, Amazon SNS  
- **Auth**: Amazon Cognito; IAM for least privilege  
- **Front‑end**: Static SPA (React/Vue) hosted on S3 + CloudFront (or Amplify for rapid orchestration)

---

## 11) Data Models & APIs

### 11.1 Core Tables (Athena/Glue Catalog)
- `econ_indicators_curated`  
  - `indicator_id`, `date`, `value`, `unit`, `source`, `seasonal_adj`, `vintage_ts`
- `model_forecasts`  
  - `indicator_id`, `horizon`, `forecast_date`, `mean`, `p10`, `p50`, `p90`, `model_type`, `train_window`, `metrics_json`
- `expectation_priors`  
  - `indicator_id`, `horizon`, `asof_date`, `prior_mean`, `prior_sigma`, `confidence`, `evidence_json`
- `fusion_posteriors`  
  - `indicator_id`, `horizon`, `asof_date`, `posterior_mean`, `posterior_p10`, `posterior_p90`, `method`, `weights_json`
- `narratives_index`  
  - `doc_id`, `publisher`, `publish_date`, `title`, `url`, `stance`, `topics`, `text_vector_id`

### 11.2 API Endpoints (API Gateway)
- `GET /indicators` → list & metadata  
- `GET /series/{indicator_id}` → historical + latest forecast bands  
- `GET /expectations/{indicator_id}` → priors + evidence snippets  
- `POST /scenarios` → apply shock; return adjusted posterior bands  
- `GET /alerts` → list deviation alerts  
- `GET /explain/{indicator_id}` → genAI explanation with citations

---

## 12) Model Governance, Safety, and Guardrails

- **No PII**: Public macro sources only.  
- **Source Attribution**: Always show citation for narrative‑derived claims.  
- **Prompt Guardrails**: Define refusal policies for unsupported claims; require evidence anchors.  
- **Bias Mitigation**: Balance publisher weights; enforce recency windows; detect sentiment extremes.  
- **Versioning**: Store prompt versions, KB snapshots, model versions for reproducibility.  
- **Observability**: Log inputs/outputs (hashes), track latency, monitor token usage (Bedrock).  
- **Security**: IAM least privilege, per‑service roles, VPC endpoints for Bedrock/SageMaker as needed.

---

## 13) Evaluation & Success Metrics

- **Forecast Quality**:
  - Backtesting MAPE/RMSE/CRPS; calibration curves for PIs; coverage metrics (e.g., P90 contains ~90% of realizations).
- **Expectation Accuracy**:
  - Compare priors to realized values (Brier score for binary events like “inflation above 3% next quarter”).
  - Stability across publishers; dispersion diagnostics.
- **Fusion Benefit**:
  - ESI correlation with subsequent forecast errors (does ESI warn of regime shifts?).
- **Usability & Clarity**:
  - Time to insight (tasks completed), SUS score, comprehension quizzes on explanations.
- **Alert Utility**:
  - Precision/recall of alerts vs. meaningful deviations users care about.

---

## 14) Hackathon Demo Storyline (10–12 minutes)

1) **Opening** (1 min): Problem & novelty—introduce the **beliefs layer**.  
2) **Data‑Only Forecast** (2 min): Show baseline CPI/GDP chart with PIs.  
3) **Expectations Track** (3 min): Open the narrative panel; show **quantified prior** (e.g., CPI next quarter 2.8% ± 0.4pp), with three cited sources (Fed statement, energy sector report, BEA release).  
4) **Fusion & ESI** (2 min): Toggle data+expectations; highlight ESI spike after a hawkish statement.  
5) **Scenario Lab** (3 min): Apply “Oil +10%” and “Hawkish policy tone”; watch posterior bands shift; narrate plain‑language explanation generated by Bedrock with citations.  
6) **Alerts** (1 min): Show a triggered alert when ESI exceeded threshold; drill into evidence/context.  
7) **Wrap‑Up** (1 min): Benefits: earlier signal detection, transparent explanations, and user‑driven scenarios—no third‑party paid tools.

---

## 15) Implementation Notes (No Code, but Practical)

- **Bedrock Prompts**:
  - *Expectation Extractor*: “Given the retrieved documents (with dates), quantify expected **CPI YoY** for **next quarter**. Return JSON with `prior_mean`, `uncertainty_band`, `confidence`, and `citations` (title, URL, date). Use only the evidence provided; abstain if insufficient.”
  - *Explanation Generator*: “Explain, for a general audience, why the **data‑only** and **data+expectations** forecasts differ for CPI next quarter. Cite specific documents and dates. Mention top 2 drivers from model explainability. Include uncertainty and risk notes.”
- **KB Configuration**:
  - Chunk size tuned for policy statements (~1–2k tokens); overlap for context.  
  - Index fields: publisher, date, topic tags for filterable retrieval.  
- **Fusion Tuning**:
  - Start with weighted blending; add Bayesian posterior for at least one flagship indicator (e.g., CPI) to demonstrate rigor.
- **Scenario IRFs**:
  - Precompute for reproducibility and speed; store in S3; parameterize shock scale/duration.

---

## 16) Why This Feels Novel (and Useful)

- **Latent Beliefs Surface**: Most dashboards ignore how **narratives shape expectations**. We quantify those beliefs and visualize them.  
- **Evidence‑Backed Explanations**: The dashboard never “hand‑waves”—it cites the **actual sources** that formed the expectations.  
- **User‑Directed Counterfactuals**: Decision makers test “what if” communications or shocks and see **propagated effects** on macro paths.  
- **Calibrated Fusion**: Data and beliefs are **mathematically fused**, yielding more robust early signals during regime changes.

---

## 17) Next Steps (Hackathon Scope)

1) Stand up **S3 + Glue + Lambda + EventBridge** for FRED/BEA pulls; load 2–3 indicators (CPI, Unemployment, GDP).  
2) Build **narratives KB** (S3 + OpenSearch Serverless); ingest 20–50 documents (Fed statements, BEA releases, energy sector updates).  
3) Train a quick **Amazon Forecast** model for CPI; produce P10/P50/P90.  
4) Implement **Bedrock expectation extractor** for CPI (next quarter).  
5) Build **weighted fusion** + compute **ESI**; show toggle in dashboard.  
6) Implement **one scenario** (Oil +10%) via precomputed VAR IRF.  
7) Add **GenAI explanation** panel with citations.  
8) Configure **one alert** (ESI threshold) via SNS.  
9) Polish UX: expectations track, scenario sliders, explanation panel, and clear citations.

