## 1) Executive summary

**Goal:** An interactive, AI-powered macro dashboard that:
- shows historical US indicators (CPI, GDP, employment, etc.),
- produces short‑term forecasts,
- runs “what‑if” scenarios,
- **explains** model outcomes in plain language via a **scene‑based “Story Mode,”**
- and pushes alerts when reality deviates from expectations.

**Novelty:** “Story Mode” turns an exec’s natural question (e.g., *“Why is our CPI forecast higher than last release?”*) into a guided set of scenes:  
**Shock detected → Expectations updated → Causal ripple → Forecast delta → Confidence → Policy options**, each grounded with **retrieved evidence** (release notes, series metadata, historical vintages) and model diagnostics. The narrative is orchestrated by **Agents for Amazon Bedrock** with **Knowledge Bases** (RAG) and safety **Guardrails**. 

---

## 2) High‑level architecture

**Layers:**
1. **Data & ingestion:** FRED + BEA APIs → S3 data lake → Glue/Lambda transforms → optional SageMaker Feature Store.  
   - Track **vintages** (ALFRED) to compare “last release vs current.”   
   - BEA datasets & parameters via BEA API tooling. 
2. **Forecasting services (SageMaker):**
   - **Time-series models** (DeepAR, ARIMA/ETS, Prophet; or Autopilot ensemble) exposed behind real‑time endpoints for CPI, GDP, etc.   
   - Note: **Amazon Forecast** is being phased for new customers—prefer **SageMaker** (or GluonTS on SageMaker) for hackathon. 
3. **Scenario engine:** Counterfactuals using models that accept **future covariates** (e.g., energy prices, unemployment assumptions). (DeepAR‑style future time features). 
4. **Narrative & reasoning (Bedrock):**
   - **Agents for Amazon Bedrock** orchestrate: retrieve facts (Knowledge Base), call forecast endpoints, compute diagnostics, and compose the **scene‑based story**.   
   - **Knowledge Bases** store curated briefs (indicator primers, release notes, model cards) with **Titan embeddings** in OpenSearch Serverless.   
   - **Guardrails** ensure safe, grounded outputs and redact sensitive content. 
5. **Visualization & UX:** A web UI (Amplify/S3 static hosting) with interactive plots, scenario knobs, and **Story Mode** chat. Open‑source charting (Plotly/D3) to stay within “no paid third‑party” constraints.
6. **Alerting & ops:** **CloudWatch anomaly detection** on key indicators/forecast errors + **SNS** notifications; releases scheduled via **EventBridge Scheduler**. 

---

## 3) Data layer (how it works)

### Sources & access
- **FRED API** for series retrieval (e.g., CPIAUCSL, GDP), metadata, observations, **vintage dates** (to track revisions and “what was known when”).   
- **BEA API** for NIPA tables (GDP components, PCE breakdowns), regional/industry datasets, and metadata discovery. 

### Ingestion & synchronization
- **EventBridge Scheduler** runs daily/weekly jobs to poll for updates from FRED/BEA and to **pin vintage snapshots** (ALFRED).   
- **Lambda + Glue** normalize series into a common schema (id, frequency, seasonal adjustment, units), write to **S3** partitioned by dataset + series + vintageDate.

### Feature management (optional)
- **SageMaker Feature Store** catalogs engineered features (seasonal flags, holiday effects, lagged metrics, external covariates). Dual **online/offline stores** prevent training/serving skew. 

---

## 4) Forecasting layer

### Model catalog
- **SageMaker time‑series options**: DeepAR+, Prophet, ARIMA, ETS, NPTS—via Autopilot or custom GluonTS training; deploy per‑indicator endpoints (e.g., `forecast:cpi`, `forecast:gdp`). 
- **DeepAR‑family** models support dynamic covariates and probabilistic outputs; useful for “what‑if” because **future covariates** can encode scenarios.   
- **Prophet** excels with strong seasonality and holiday components; offer a “decomposed view” (trend/seasonality/holidays) for interpretability. 

### Training & evaluation
- Rolling-origin backtests per indicator; ensemble the top performers and expose **quantiles** (p10/p50/p90) for uncertainty bands.
- Publish **model cards** (KB docs) summarizing scope, data windows, assumptions, and diagnostics.

### Scenario engine (counterfactuals)
- Encode scenario variables (e.g., oil price path, unemployment trajectory, fiscal impulse) as **future covariates**; re‑score forecasts and show **delta vs baseline**. (DeepAR‑style inputs with `future_time_feat` + static covariates). 

> **Note on Amazon Forecast:** Documentation indicates it’s **no longer available to new customers**, so prefer SageMaker-based approaches for this hackathon. 

---

## 5) “Story Mode” conversational experience

### Orchestration
- **Agents for Amazon Bedrock** coordinate the scene flow:  
  1) **Understand** the user’s question,  
  2) **Retrieve** evidence (latest release notes, series metadata, prior forecast narrative) from **Knowledge Base**,  
  3) **Call** forecast/scenario endpoints,  
  4) **Compute** explanations & confidence,  
  5) **Compose** a structured **scene‑based narrative** with **citations** back to sources/data. 

### Knowledge grounding
- **Bedrock Knowledge Bases** implement **RAG**: index curated briefs and indicator “primers,” plus cached snippets from FRED/BEA documentation. Use **Amazon Titan Text Embeddings** in **OpenSearch Serverless** for retrieval. 

### Safety & consistency
- **Guardrails** apply configurable content filters, denied topics (e.g., non‑economic advice), word filters, **PII redaction**, and **contextual grounding** to reduce hallucinations. (Also supports automated reasoning checks). 

### The narrative “scenes”
1. **Shock detected**  
   - Detect release‑over‑release changes using ALFRED **vintage dates** (e.g., CPI revision, GDP update). Visualize the delta and mark a “shock.” 
2. **Expectations updated**  
   - Show how prior forecast bands shifted (p10/p50/p90) after ingesting the new data window; Bedrock explains why the band moved (model retraining notes, residuals).
3. **Causal ripple**  
   - Retrieve causal breadcrumbs (indicator‑component relationships from model card/KB); narrate plausible drivers (e.g., energy, housing, wages) with links to series in KB.
4. **Forecast delta**  
   - Quantify change vs baseline; highlight contributions (e.g., scenario or covariate attributions).
5. **Confidence**  
   - Present uncertainty intervals; “what could make this wrong” section; nudge to drill‑down.
6. **Policy options**  
   - Provide **options framing** (“If oil moderates to X, CPI path moves ↓Y”; “If payrolls surprise ↑Z, core services pressure ↑Q”), backed by **scenario engine** runs.

> The agent emits the story as **sections with embedded citations** and structured bullets/tables the UI can render cleanly.

---

## 6) Plain‑language explanations (model diagnostics)

**Explainability toolkit:**
- For statistical models (Prophet/ARIMA), the **decomposition** (trend/seasonality/holidays) provides *built‑in* narrative hooks. 
- For ML models (DeepAR/ensembles), use **feature/driver summaries**: e.g., lag contributions and scenario covariates. (General SHAP guidance is model‑agnostic; adapt carefully for time series.) 

**Design note:** Time series SHAP/LIME requires chronology‑aware setups; we keep explanations at **component level** (drivers/segments) rather than raw lag‑by‑lag charts to avoid misleading attributions. (Best‑practice caveats highlighted in SHAP tutorials and time‑series papers.) 

---

## 7) Alerts for significant deviations

**What to monitor:**
- Indicator values vs forecast **expected band** (above/below anomalies).  
- Forecast residuals (MAE/MAPE) breaches.  
- Release cadence gaps (missing updates).

**How:**
- **CloudWatch anomaly detection** builds an **expected value band** for metrics, factoring daily/weekly seasonality; create anomaly‑based alarms. 
- Wire alarms to **Amazon SNS** topics for email/SMS/Lambda notifications to the research team. 
- Use **EventBridge Scheduler** for cron/rate triggers (e.g., fetch new FRED/BEA data at specific times). 

---

## 8) UX & interaction design

**Main dashboard panels:**
- **Indicator explorer:** historical trend + forecast bands; toggle “first release vs latest revised” view (ALFRED vintage).
- **Scenario workbench:** sliders/dropdowns for key assumptions (oil, unemployment rate, housing rent growth); instant re‑forecast with deltas.
- **Story Mode:** chat pane with **scene cards**—each card shows retrieved snippets, charts, and a concise explanation with citations.

**Design choices:**
- Keep the narrative *short, structured, and scannable*; every claim has a **source badge** pointing to the underlying data/doc (from KB).
- Provide a **“Show me the data”** button opening the exact FRED/BEA series page / release notes via KB citations. 

---

## 9) Security, governance, and reliability

- **Guardrails**: enable content filters, denied topics, word filters, **sensitive information redaction**, and **contextual grounding/hallucination checks** across models/agents to keep outputs safe and auditable. 
- **Grounded answers**: Bedrock Knowledge Bases include **citations** in responses, anchoring the narrative to the original source. 
- **Observability**: Agent traces (step‑by‑step) + CloudWatch metrics for data freshness and model latencies. (Agents provide orchestration tracing.) 
- **Access**: IAM‑scoped roles; private KB storage with OpenSearch Serverless; S3 bucket policies for datasets. 

---

## 10) Deployment & operations (how it would work)

- **Pipelines**  
  - **EventBridge Scheduler** invokes Lambda to ingest FRED/BEA.   
  - Transform & store in S3; update Feature Store (optional).   
  - Retrain models on schedule (weekly/monthly) or on release events; update SageMaker endpoints. 
- **Real‑time inference**  
  - UI calls API Gateway/Lambda → SageMaker endpoints → returns quantiles + diagnostics; results cached in DynamoDB/ElastiCache for fast Story Mode.
- **Narrative orchestration**  
  - Bedrock **Agent** receives user prompt; retrieves from KB; calls forecast/scenario tools; assembles the **scene script**; applies **Guardrails**; streams to UI. 
- **Alerts**  
  - CloudWatch **anomaly alarms** + **SNS** notifications to the research distribution list. 

---

## 11) Success metrics & evaluation

- **Usability:** time‑to‑insight (median seconds from question to answer), % of sessions using Story Mode, exec satisfaction score.
- **Clarity:** explanation quality rating (Likert), citation click‑through rate, % narratives with at least two verified sources (from KB).
- **Forecast performance:** rolling MAPE/Pinball loss, alert precision/recall (anomalies).

---

## 12) What you’ll demo (hackathon storyline)

1. **Data freshness:** Show CPI series with latest **vintage** overlay and a “compare to prior release” toggle.   
2. **Forecast & scenario:** Baseline CPI forecast; then adjust oil price path (scenario), see new quantiles & **delta**.
3. **Story Mode question:** *“Why did CPI p50 move up vs last release?”* → Agent walks through the six scenes with **citations** and charts.   
4. **Alert:** Trigger a **CloudWatch anomaly alarm** on a test metric to show **SNS** alert. 

---

## 13) Stretch ideas (if time permits)

- **Zero‑shot baseline** with **Chronos** (time‑series foundation model) for comparative narratives—keep as an optional showcase. 
- **Log anomaly detection** on pipeline logs to auto‑surface ingest issues (CloudWatch Logs Insights *anomaly*). 
- **Composite alarms** to reduce noise (CloudWatch). 

---

## 14) Why this is “novel” (and not just another dashboard)

- Moves from **visualization → comprehension**: The Story Mode produces an **executive narrative** with *cause‑effect framing*, **counterfactuals**, and **confidence**, all **grounded** with citations—no hallucinated punditry. 
- Uses **Agents** to *do work* (retrieve, compare vintages, call forecasts, run scenarios), not just chat. 
- Treats **vintage comparisons** as first‑class, so “last release vs this release” questions are **data‑provable**. 
- **Production‑ready safety** with Guardrails (PII, content filters, contextual grounding). 

---

### References (for the judges)
- **Agents for Amazon Bedrock** (overview, orchestration, prompt templates)   
- **Bedrock Knowledge Bases (RAG), Titan embeddings, OpenSearch Serverless**   
- **Bedrock Guardrails** (filters, denied topics, contextual grounding, automated reasoning checks)   
- **FRED API** (series, observations, vintage dates/ALFRED)   
- **BEA API** (datasets, parameters, NIPA)   
- **SageMaker time‑series algorithms** (Autopilot’s supported algorithms)   
- **Prophet** (components & seasonality)   
- **CloudWatch anomaly detection + SNS + EventBridge Scheduler**   
- **Amazon Forecast service status** (use SageMaker instead) 

---

## Quick next steps for hackathon prep (non‑coding)

1. **Curate the KB docs**: 1‑page primers per indicator, model cards, and release‑note snippets (with links). Wire into Bedrock **Knowledge Base** (OpenSearch Serverless + Titan embeddings).   
2. **Decide your first three scenarios**: e.g., Oil ↑20%, Payrolls surprise +150k, Housing rent disinflation.  
3. **Pick 2–3 models per indicator** (Prophet + DeepAR as a good pair) and define evaluation plots the agent can call.   
4. **Storyboard the six scenes** with sample outputs and **citations badges**.  
5. **Set one CloudWatch anomaly alarm** on a CPI residual metric and **SNS** email to the team for demo. 

