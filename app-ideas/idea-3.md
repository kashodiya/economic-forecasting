## 0) One‑liner (what makes this novel)

> **Semantic Delta → Forecast**: Every time BEA/FRED releases new data, a “release‑reader” agent **computes the semantic delta** from the last vintage (what actually changed and why), **updates forecasts automatically**, and **narrates evidence‑linked reasoning** with citations into the exact paragraph/table of the release—so there’s **no manual translation** from release text to model inputs.

---

## 1) End‑to‑end architecture (high level)

**Event-driven pipeline** orchestrated with Step Functions:
1. **Trigger** on a BEA/FRED release (RSS + schedule + daily poll via EventBridge Scheduler).
2. **Ingest** release text/tables + time‑series updates (BEA API + FRED API).
3. **Compute semantic delta** (Bedrock LLM + embeddings + OpenSearch vector RAG).
4. **Assimilate shocks** into ML forecasts (SageMaker time-series models).
5. **Explain** in plain language with **evidence links** (LLM + retrieval).
6. **Visualize** history, short‑term forecasts, and **what‑if** scenarios (QuickSight).
7. **Alert** on deviations/anomalies (SNS + QuickSight ML Insights).

**Core services**: Amazon EventBridge, AWS Step Functions, AWS Glue, Amazon S3, Amazon Timestream (or S3+Athena), Amazon OpenSearch Service (vector search), Amazon SageMaker (DeepAR), Amazon Bedrock (LLMs + embeddings), Amazon QuickSight (dashboard + ML insights), Amazon SNS. 

---

## 2) Data sources & release detection

- **BEA**:  
  - **API & tools** for NIPA tables, GDP, PCE, Personal Income, trade, etc. (JSON/XML).   
  - **Release Schedule** used to set scheduler windows (e.g., GDP Advance/Second/Third, Personal Income & Outlays). 
- **FRED**:  
  - REST **API** for series and **release-level pulls** (observations, vintage dates). 

**Design**:
- **EventBridge Scheduler** cron rules aligned to BEA release calendar, plus RSS polling fallback (exact time). Events route to a dedicated **event bus** with filter patterns (release type). 
- **Step Functions (Standard)** orchestrates the multi‑step workflow (long‑running, auditable, retries, human‑in‑the‑loop). 

---

## 3) Data ingestion & storage

- **Ingestion**:  
  - **AWS Glue** jobs: pull BEA API tables + FRED series; normalize to tidy time‑series; catalog with Glue Data Catalog for downstream (Athena/QuickSight).   
  - **Release artefacts** (PDF/HTML) stored in **S3** (“raw/releases/<date>/<release_type>”).  
- **Time‑series store**: two options (choose per hackathon scope):  
  1) **Amazon Timestream (LiveAnalytics)** for fast time‑series queries with built‑in functions and long horizons.   
  2) **S3 Parquet + Glue Catalog + Athena** (if you prefer pure lakehouse simplicity). (Glue citations above) 

**Why Timestream?** Purpose‑built time‑series, serverless scaling, SQL over recent+historical data; integrates with SageMaker & BI tools. 

---

## 4) “Shock Interpreter” (semantic delta engine)

**Goal**: Automatically translate a new BEA/FRED release into **structured shocks** and a human‑readable narrative, **anchored** to the source evidence.

**A) Document understanding & retrieval**
- **Embeddings**: Use **Amazon Titan Text Embeddings** via Bedrock to vectorize paragraphs, tables captions, and extracted cell notes. 
- **Vector store**: Index embeddings + metadata (series IDs, table, row, vintage) in **Amazon OpenSearch Service (vector search / Serverless)** with k‑NN (HNSW/IVF), cosine/dot metrics. 
- **RAG**: For any claim in the explanation, retrieve top‑k relevant passages/tables and attach the **evidence link** (S3 object path + paragraph offset + BEA/FRED URL). (OpenSearch vector RAG). 

**B) Semantic delta computation**
- **Compare current vs prior vintage** for each series in scope (e.g., PCE, CPI, components). Compute **Δlevel**, **Δgrowth**, **Δcontribution** by component.  
- **LLM reasoner** (Bedrock): Claude‑class models (e.g., Sonnet) for nuanced economic reasoning over retrieved passages; generates a **structured delta object** (`{series, measure, magnitude, direction, driver_text_span, confidence}`). 
- **Consistency checks**:  
  1) **Numeric cross‑check**: LLM output must match computed deltas from the data layer (guardrails on magnitude and sign).  
  2) **Evidence presence**: each reasoning segment must carry at least one **retrieved citation** (OpenSearch doc id + BEA/FRED URL).

**C) Output**  
- Persist **delta objects** in a “semantic_delta” table (Timestream or S3 Parquet), keyed by `release_id`.

---

## 5) Forecasting: assimilate shocks → update projections

**Models**: Use **SageMaker DeepAR** (built‑in time‑series algorithm) for probabilistic short‑term forecasts (quantiles). It supports related time‑series (dynamic_feat), enabling drivers (e.g., CPI components, PCE goods/services) to inform the target variable (GDPNow‑style themes). 

> Note: **Amazon Forecast** is not available to new customers; for a hackathon and future‑proofing, prefer **SageMaker** DeepAR/Autopilot/Chronos. 

**Assimilation layer (novel bit)**:
- Treat each semantic delta as a **shock** to the latest observed drivers.  
- Run a **Rapid Update**:  
  - Update the last observation(s) of driver series per delta.  
  - Re‑score the forecast via **SageMaker endpoint** (no full retrain).  
  - Optionally apply a **Bayesian adjustment** (Kalman‑style) to the target series mean forecast to reflect the delta magnitude.  
- Store **quantile forecasts (P10/P50/P90)** + feature influence (from DeepAR explainability & SHAP via SageMaker Clarify if used). (DeepAR primary doc; Clarify reference can be added if you plan feature importance.)

**Explainability companion**:
- **Bedrock LLM** composes a **plain‑language “forecast change note”** (“GDP Q/Q nowcast moved from 2.1% to 1.8% because goods PCE slowed; see Table X, para Y”) with **evidence‑linked RAG** to the BEA release table/paragraph. 

---

## 6) Interactive dashboard (history, forecasts, what‑if, narratives)

**Amazon QuickSight** for the front end:
- **Historical visualizations**: multi‑series trends (GDP, CPI, UNRATE, PCE); filters by vintage date.  
- **Forecast panels**: fan charts (quantiles), next 1–6 months/quarters.  
- **What‑if control**: viewers **adjust driver assumptions** (e.g., “CPI next month +0.4% vs +0.2%”) via parameters; the app calls the SageMaker endpoint, returns updated forecasts and narratives.  
  - If embedding in a web app, use **QuickSight Embedding SDK runtime filtering/theming** to pass scenario inputs and keep the UX native to your application. 
- **Narratives**: “Executive summary” and **autonarratives** (plain‑language summaries) on each sheet. 
- **Anomaly insights & alerts**: enable **ML‑powered anomaly detection** to surface outliers and key drivers (Random Cut Forest under the hood) directly in the dashboard. 
- **General interactivity**: drill‑downs, filters, bookmarks—standard QuickSight capabilities for rich UX. 

---

## 7) Alerts & notifications

- **Deviation alerts**:  
  - **Automated**: QuickSight ML Insights flags anomalies; publish event → **SNS** topics (email/SMS/webhooks) for significant deviations beyond thresholds (e.g., forecast error vs last release, or spike in CPI).   
  - **Manual thresholds**: EventBridge rules watch CloudWatch metrics from the inference service; on breach, **SNS** push to stakeholders. 

---

## 8) Orchestration & flow (Step Functions state machine)

**Workflow outline** (Standard type):
1. **Await Release Event** (EventBridge match on BEA schedule or FRED release).   
2. **Fetch Release Data** (Glue/Lambda → BEA API + FRED API; store in S3, update tables).   
3. **Vectorize Artefacts** (Bedrock Titan Embeddings → OpenSearch index).   
4. **Compute Semantic Delta** (LLM reasoning + numeric checks; persist).   
5. **Update Forecasts** (SageMaker endpoint inference; record quantiles).   
6. **Generate Narratives** (Bedrock LLM with RAG; write to S3/Glue).   
7. **Publish to Dashboard** (QuickSight dataset refresh).   
8. **Alert** (SNS if deviations/anomalies). 

Step Functions provides visual/auditable execution, retries, and human approval steps (e.g., optional “Analyst approve” gate in hackathon demo). 

---

## 9) Data model & evidence linkage

**Key tables / objects**:
- `time_series_raw` (source series with vintage date, unit, frequency, source_id).  
- `semantic_delta` (release_id, series_id, magnitude, direction, component, evidence_doc_id, evidence_span, confidence).  
- `forecast_output` (target series, horizon, quantiles, timestamp, model_id).  
- `narratives` (release_id, section_id, markdown_text with embedded links to BEA/FRED).

**Evidence IDs** point to:
- S3 object + byte offset (PDF) or anchor (HTML),  
- **OpenSearch doc_id** to enable click‑through retrieval in the dashboard RAG panel. 

---

## 10) Security, governance, and compliance

- **IAM** scoped roles for Glue/Step Functions/EventBridge; **KMS** encryption for OpenSearch vector indices and S3 buckets; private VPC endpoints for Bedrock and OpenSearch. (Service docs provide standard practices.) 

---

## 11) Novelty highlights (why judges lean in)

1. **Automated, evidence‑linked reasoning**: Every sentence of the explanation carries a **retrieved citation** to the release (paragraph/table)—*not* a generic summary. (OpenSearch vector RAG + Titan embeddings).   
2. **Semantic delta assimilation**: LLM extracts **economic shocks**; the system **updates forecasts instantly** via SageMaker inference—no human translation of releases to model inputs.   
3. **What‑if sliders**: Interactive adjustment of drivers, with **runtime filters/themes** in embedded QuickSight; users see immediate **forecast and narrative** shifts.   
4. **Alerting on “surprises”**: ML anomaly detection in the dashboard + SNS alerts when deltas imply significant deviations. 

---

## 12) Implementation plan (hackathon‑friendly)

**Day 0–1: Data & plumbing**
- Configure EventBridge Scheduler for the next BEA release windows; stub RSS poll.   
- Build Glue jobs to ingest 2–3 exemplar series (e.g., CPI, PCE, GDP components) from BEA + FRED; store in S3/Glue Catalog. 

**Day 2–3: RAG & delta extraction**
- Index one release PDF + HTML with Titan embeddings → OpenSearch vector collection.   
- LLM prompt engineering to extract `{series, component, magnitude, driver_text}`; wire numeric cross‑checks vs BEA API pulls. 

**Day 4–5: Forecasting**
- Train a small **DeepAR** model on CPI/PCE target with 24–60 months history; deploy inference endpoint; implement the “Rapid Update” shock assimilation. 

**Day 6–7: Dashboard & alerts**
- QuickSight analysis → history + forecast panel + autonarratives; embed in a lightweight web app; wire SNS alerts. 

**Day 8: Polish & demo script**
- Add the “Click evidence” panel showing the exact BEA paragraph/table used by the explanation.
- Prepare a live “new release simulation” with EventBridge to show end‑to‑end flow. 

---

## 13) Dashboard flow (user experience)

- **Sheet 1 — Overview**: KPI cards (Inflation, GDP, Employment), trend lines, “Executive summary” (autonarrative).   
- **Sheet 2 — Forecasts**: Fan chart + quantiles; sidebar shows “Latest shocks” with chips (e.g., “PCE goods −0.3 pp; driver: durable goods”).  
- **Sheet 3 — What‑if**: Sliders/dropdowns for key drivers; **Apply** triggers inference update; narrative updates with fresh **evidence links**.   
- **Sheet 4 — Alerts**: Anomaly list (QuickSight ML); **Subscribe** to SNS topic from the UI. 

---

## 14) Success metrics (aligned to hackathon brief)

- **Usability & insights**:  
  - ≤2 clicks from release to story; click‑through to source table/paragraph; **time‑to‑update** measured in minutes.  
- **Clarity of AI explanations**:  
  - Each forecast change note includes **linked evidence** and **delta magnitudes**; human evaluator rating on clarity.  
- **Alerting**:  
  - SNS alerts with threshold + QuickSight anomaly detection consistency (precision/recall on flagged events). 

---

## 15) AWS service choices — quick rationale

- **Bedrock models**: Access to **Claude** (reasoning), **Titan Embeddings** (vector/RAG) under AWS security and billing—ideal for enterprise demos.   
- **OpenSearch vector search**: Native, scalable vector + keyword hybrid; perfect for **evidence‑linked RAG** over releases.   
- **SageMaker DeepAR**: Time‑series forecasting with probabilistic outputs and related features; **Forecast** deprecation makes this the right path.   
- **QuickSight**: Interactive dashboard, generative **autonarratives**, ML anomaly detection; embeddable with runtime control.   
- **EventBridge + Step Functions**: Event‑driven orchestration with visual auditing and retries; ideal for **release‑triggered** workflows.   
- **SNS**: Simple, multi‑channel notifications for deviations or anomalies. 

---

## 16) Risks & mitigations

- **PDF parsing fidelity**: Prefer BEA API tables for numeric truth; use document text for **context**. (BEA Dev portal)   
- **Hallucination risk** in LLM narratives: Hard **numeric guardrails**; require **evidence spans** from RAG for each claim (OpenSearch doc_id).   
- **Latency** at release time: Cache embeddings for common sections (e.g., GDP release structure), pre‑warm SageMaker endpoints, and run parallel states in Step Functions. 

---

## 17) Demo storyline (judges’ view)

1) **Event fires** at “GDP Advance” release time → pipeline runs.   
2) Dashboard flashes: **“New release ingested”**, **Executive summary updates**; fan chart shifts slightly.   
3) Judge clicks “Why?” → sees **delta chips** and a narrative with **links** to the BEA table row and paragraph.   
4) Judge tweaks **what‑if** (“CPI +0.4% next month”) → instant recompute; updated narrative appears.   
5) Anomaly panel flags a meaningful deviation; **SNS** email alert arrives. 

---

## 18) Optional extensions (if you have time)

- **Hybrid search** in OpenSearch (keyword + vector) to improve RAG accuracy for table queries.   
- **Sparse vectors** for efficiency on large release corpora.   
- **Timestream + InfluxDB engine** for ultra‑low latency “nowcast” micro‑interactions. 

---

### Quick note on data sources you’ll show in the demo
- BEA GDP page and **Release Schedule** for concrete examples and dates.   
- FRED **series/observations** endpoints for CPI/UNRATE updates. 

---

## Final thought

This design puts **AI where it matters**: in automating the **translation from public releases to model‑ready shocks** and then **explaining** the **forecast movement** with **traceable evidence**—all wrapped in an interactive, enterprise‑grade dashboard.

