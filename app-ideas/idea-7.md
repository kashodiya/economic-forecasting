## 1) Executive Summary

**Goal:** An interactive, AI‑powered economic indicators dashboard that (a) visualizes history, (b) produces short‑term forecasts, (c) runs credible what‑ifs, (d) explains reasoning in plain language, and (e) raises **actionable early‑warning alerts** with links to corroborating data and suggested **next checks**.

**Novelty:** Alerts are **narratives**, not threshold pings. The system triangulates anomalies with corroborating signals (e.g., core goods vs. services; USD strength; freight indices) and uses an LLM to assemble an **evidence‑based story** with confidence, links, and follow‑up steps.

**Stack constraints:**  
- **Data:** FRED + BEA APIs only (public).   
- **Forecasting:** Amazon **Forecast** (AutoPredictor) or **SageMaker DeepAR** (choose per data shape).   
- **LLM:** **Amazon Bedrock** (Anthropic Claude family) + **Bedrock Guardrails** for safe explanations.   
- **What‑if:** Uses **known‑in‑advance features** (DeepAR/Forecast) to simulate scenarios.   
- **Orchestration/alerts:** **EventBridge** + **SNS** (email/Slack via webhook).   
- **UI:** Lightweight web app (React/Next.js or Streamlit) on **S3+CloudFront** or **Amplify**.

---

## 2) Architecture Overview (Conceptual)

```
[FRED API]----\
               \--> [Ingestion & Normalization Lambda] --> [S3 Data Lake + Glue Catalog]
[BEA API]-----/                                         \
                                                     [Athena SQL Views] --> [App/API]
                                                     [Feature Store (DynamoDB/KV)]
                       [EventBridge]  ──────►  [Model Train/Refresh Step Functions]
                                              └► [Forecast (AutoPredictor) / SageMaker DeepAR]
                                                             │
                                  [Actuals + Forecasts + Residuals in S3/Athena]
                                                             │
                                       [Anomaly Detector Lambda + Evidence Picker]
                                                             │
                                      [Bedrock (Claude) Narrative Generator]
                                                             │
                                        [Guardrails] ──► [SNS Alerts + Dashboard Cards]
                                                             │
                                          [What-if Simulator API + UI Controls]
```

- **Data Lake:** S3 is the system of record; Glue catalogs tables for Athena queries.  
- **Forecasting:** Either **Amazon Forecast** AutoPredictor for multi‑series with quantiles/explainability, or **DeepAR** on SageMaker when fine control is needed.   
- **Anomaly + Narrative:** Lambda detects deviations; **Claude** on **Bedrock** generates an explanation with **links to release pages/series** and **suggested next checks**.   
- **Alerts:** **EventBridge** routes anomalies to **SNS**. 

---

## 3) Data Sources, Acquisition & Normalization

**Scope:** Core macro series (inflation/CPI, PCE, GDP, employment), auxiliary drivers (USD broad index, freight/transport indices, commodity proxies).  
- **FRED API** provides programmatic endpoints for **series metadata and observations** (e.g., `/fred/series/observations`), **releases**, and **vintage dates** (revision history), enabling “as‑of” views.   
- **BEA API** exposes **NIPA**, **GDP by industry**, and related tables via `GetData` and metadata endpoints; you register for a free API key.   
- BEA also publishes a **News Release RSS feed**, which can be parsed to attach release context to anomalies. 

**Design choices:**
- **Normalization:** Convert all time series to a uniform frequency (monthly or quarterly), align calendars, handle missing values; store raw and normalized forms in **S3** partitions by `dataset/source/series_id/frequency/yyyymm`.  
- **Revisions (“vintages”):** Persist **ALFRED/FRED vintage dates** to support “what was known when” narratives (e.g., “second estimate revised downward”).   
- **Semantic catalog:** Glue tables hold `series`, `observations`, `vintage`, `releases`, with Athena views for the app.

**Automation:**  
- **EventBridge** schedules daily/weekly ingestions; **Lambda** pulls JSON/XML from FRED/BEA and writes to S3. 

---

## 4) Forecasting Engine (Short‑Term Horizons)

**Option A: Amazon Forecast (AutoPredictor)**  
- **AutoPredictor** selects optimal algorithms per series (not one‑size‑fits‑all) and outputs **probabilistic forecasts (e.g., p10/p50/p90)**, ideal for anomaly detection.   
- Supports **Related Time Series** (e.g., USD index, oil price, holidays) and **Explainability** (driver importance), useful for the narrative.   
- **CLI/API design:** Use `create-auto-predictor` with configured horizon/frequency and enable predictor explainability; batch exports to S3 for dashboard. 

**Option B: SageMaker DeepAR**  
- Joint modeling across multiple series, **known‑in‑advance features** for **what‑ifs**, and strong performance with many related indicators.   
- DeepAR **automatically derives calendar features** (day‑of‑week, month‑of‑year), and lets us inject external drivers (e.g., policy rate path, dollar index) for scenario analysis. 

**Selection heuristic:**  
- Use **Forecast** when you want **managed AutoML + multi‑series auto‑selection**, quantiles, and explainability quickly. Use **DeepAR** when you need **tight control** over features/hyperparameters or custom scenario handling. 

---

## 5) Anomaly Detection & Early‑Warning

**Signal foundation:**  
- Compute **residuals** = actual − forecast(p50) and **surprise score** relative to forecast intervals (e.g., outside p10–p90 band → “material deviation”).   
- Layer a **change‑point** detector (statistical) to catch **trend breaks** even when values stay within quantiles.

**Evidence picker (triangulation):**  
When an anomaly fires for a headline series (e.g., CPI services), the engine queries auxiliary signals for the same window (USD index, freight indices, core goods/services subcomponents, BEA release notes) and compiles **corroboration**:
- FRED endpoints provide **related series** and **release tables** to pull subcomponents and context.   
- BEA **NIPA tables** and **release metadata** help distinguish “core goods” vs. “services” drivers and attach release timing. 

**Alert policy:**  
- **Severity** is computed from: magnitude relative to quantile band, persistence across 2–3 periods, cross‑signal corroboration.  
- **EventBridge** emits an alert event; **SNS** delivers to email/Slack with the **narrative card** attached. 

---

## 6) Narrative Generator (Actionable Explanations)

**Objective:** Convert anomaly + evidence into a **plain‑language, hypothesis‑driven narrative** with **links** and **next checks** (e.g., “Core goods drove the break; USD strength and freight indices suggest transitory pressure”).

**Pipeline:**
1. **Compile context:**  
   - Indicator name, window, residuals, quantile breach.  
   - Top 3 corroborating signals (delta vs. baseline), linked to **FRED** series pages and **BEA** interactive tables/release notes. 
2. **Prompting strategy (Bedrock Claude):**  
   - **Structured prompt** with (a) facts, (b) links, (c) confidence scores, (d) requested output format:  
     - **Summary (2–3 sentences)**  
     - **Drivers & evidence bullets (with series IDs/links)**  
     - **Suggested next checks** (e.g., “Validate with BEA Table 2.3.6 PCE breakdown,” “Monitor USD broad index next release”).  
   - **Model choice:** Claude **Sonnet** for balance (fast + reasoning), **Opus** if available/needed for complex cases. 
3. **Guardrails:**  
   - Apply **Bedrock Guardrails** to block inappropriate content, redact sensitive items, and enforce denied topics (e.g., no **personal investment advice**). 

**Output:**  
- **Narrative card** with **evidence links** and **confidence** (e.g., *High* if corroboration is strong across multiple independent drivers).  
- Stored in DynamoDB/S3 for auditability.

---

## 7) “What‑If” Scenario Analysis (Interactive)

**Design principle:** Let users adjust **known‑in‑advance drivers** (policy rates, FX path, holiday effects, a freight cost proxy) and re‑run the forecast quickly.

- With **DeepAR**, **known‑future feature time series** are explicitly supported, enabling realistic counterfactuals (e.g., “USD +5% over next 3 months”).   
- With **Forecast**, use **Related Time Series**, **Weather/Holidays Featurization**, and retrain or simulate with adjusted inputs; **AutoPredictor** helps keep latency manageable. 

**UX:**  
- Sliders/drop‑downs for drivers; a run button computes new forecasts and **compares deltas** vs. baseline on charts and in a short LLM summary (“Under stronger USD, import‑heavy components decelerate; net CPI impact −0.2pp at p50”).

---

## 8) Dashboard Experience

**Pages:**
1. **Overview:** Key indicators with sparklines, latest actuals vs. p50 and bands, anomaly badges, and **narrative cards**.  
2. **Indicator Detail:** Rich chart (history, forecast bands), subcomponent decomposition, **Explainability** (Forecast’s feature importance / DeepAR contribution proxy), and **What‑if** controls.   
3. **Alerts Center:** Timeline of alerts; each shows narrative, links to FRED/BEA releases, and suggested next checks.   
4. **Release Calendar:** Upcoming BEA/FRED releases with badges—improves analyst workflow; BEA provides developer resources and feeds. 

**Tech:**  
- **Front‑end**: React/Next.js or Streamlit.  
- **Hosting**: **S3 static site + CloudFront** or **Amplify**.  
- **Data access**: **API Gateway + Lambda**, or direct **Athena** via backend API.  
- **Identity**: **Cognito** for auth (optional).

---

## 9) Alerts & Routing

- **Detection**: Batch post‑processing after new actuals or forecasts arrive.  
- **Eventing**: **EventBridge** rule triggers on anomaly events or schedule; targets **SNS** (email) and webhooks.   
- **Content**: Subject line summarizes indicator + deviation; body includes narrative, confidence, links (FRED series page, BEA table/release). 

---

## 10) Data/Model Ops

- **Pipelines**: **Step Functions** orchestrate ingest → normalize → train → forecast → anomaly → narrative → alert.  
- **Monitoring**: CloudWatch metrics on forecast latency, anomaly frequency; drift checks on residual distribution.  
- **Cost control**: Nightly training; on‑demand what‑if reuse of cached models.  
- **Versioning**: Keep **vintages** for reproducibility (FRED provides `series/vintagedates`). 

---

## 11) Governance, Safety & Compliance

- **Guardrails**: Deny topics (e.g., personalized investment advice), content filters, and sensitive info redaction applied to all narrative generations.   
- **Disclaimers**: Dashboard footer clarifies this is **economic research**, not individualized financial advice.  
- **Access**: IAM policies; S3 bucket policies; KMS encryption (optional).

---

## 12) Success Metrics & Measurement

- **Usability**: Time to insight (from alert open to action), scenario run time < **10s**.  
- **Explainability clarity**: Analyst survey on narrative usefulness; measure clicks on **evidence links**.  
- **Forecast quality**: Coverage of p10–p90 bands; MAPE/RMSE (where appropriate).  
- **Alert quality**: Fraction of alerts with corroboration across ≥2 independent drivers.

---

## 13) MVP vs. Stretch

**MVP (Hackathon‑ready, ~1–2 weeks):**
- Ingest 6–8 flagship series (CPI components, PCE, GDP, unemployment). **FRED** + **BEA**.   
- One forecaster (**Forecast** AutoPredictor) producing monthly p10/p50/p90 for 6 months.   
- Residual‑based anomaly detection + **Claude Sonnet** narrative with 2–3 evidence links and **next checks**; **Guardrails** enabled.   
- Simple React dashboard (S3/CloudFront) + **SNS** alert emails. 

**Stretch (Post‑hackathon):**
- **DeepAR** scenarios with multiple known‑in‑advance drivers and faster **what‑if** loops.   
- Broader evidence graph (FX, commodities, transport indices), and Forecast **Explainability** visualizations.   
- Release‑aware narratives (BEA RSS and FRED release tables). 

---

## 14) Why This Is “Novel” (Beyond Old‑Style Analysis)

1. **Narrative First:** Alerts read like an analyst memo with **drivers, links, and next checks**—not just “X > threshold.”  
2. **Triangulated Evidence:** The engine **corroborates anomalies** across independent signals before speaking.  
3. **Scenario Intelligence:** What‑ifs are **credible** because they propagate through **known‑in‑advance features** in the time‑series model.   
4. **Explainability Hooks:** Where supported (Amazon Forecast), include **driver importance** to anchor the LLM’s explanation. 

---

## 15) Implementation Notes (Pragmatic Choices)

- **Data pull:** Use **FRED** endpoints for `series/observations`, `releases`, `vintage`; **BEA** `GetData` for NIPA tables; store raw JSON + normalized parquet in S3.   
- **Forecast cadence:** Monthly retrain (or upgrade with **AutoPredictor**) and daily refresh of nowcasts as new inputs arrive.   
- **LLM prompts:** Keep **compact, structured** (facts → reasoning → formatted output).  
- **Guardrails config:** Denied topics (investment advice), profanity filter, contextual grounding checks.   
- **Alert routing:** **EventBridge** rule → **SNS** mail; add Slack webhook for team channels. 

---

## 16) Risk & Mitigation

- **Data revisions:** Use **vintages** to avoid narrative contradictions; include “as‑of” date in cards.   
- **Model drift:** Watch residual distribution; auto‑retrain via **Step Functions** when drift exceeds threshold.  
- **LLM hallucination:** Provide **grounding facts** + **Guardrails** + explicit links to source series/release pages.   
- **Latency/cost:** Cache evidence queries; restrict what‑if to preselected drivers; prefer **Sonnet** unless complex reasoning requires **Opus**. 

---

## 17) Demo Script (How You’ll Showcase It)

1. **Open Dashboard:** Show CPI panel—bands and latest outlier badge.  
2. **Click Alert:** The **narrative card** explains breach, cites **USD index** and **freight** as possible drivers, links to **FRED** series and **BEA** table.   
3. **Run a What‑If:** Increase USD strength path; watch forecast adjust and read the **LLM summary** of impact using **known‑in‑advance** features.   
4. **Show Email Alert:** From **SNS**, highlighting actionable next checks. 

---

## 18) Compliance With Constraints

- **LLM models:** **Amazon Bedrock** (Anthropic Claude).   
- **No paid 3rd‑party vendors:** Only **AWS** and **public FRED/BEA** data/APIs are used. 

---

### Quick Reference (Citations)
- FRED API: series, observations, releases, vintages.   
- BEA Developer/API & data tools, RSS: keys, datasets, NIPA.   
- Amazon Forecast (docs, AutoPredictor, CLI): features, quantiles, explainability.   
- SageMaker DeepAR (docs & known‑in‑advance features).   
- Bedrock models (Anthropic Claude) & Guardrails.   
- EventBridge + SNS (scheduling/alerts). 

