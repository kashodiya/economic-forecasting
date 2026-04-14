## 1) Concept Overview — An AI‑mediated debate for economic forecasts

**Goal:** An interactive dashboard that:
- shows historical trends,
- produces short‑term forecasts (multiple models),
- runs “what‑if” scenarios,
- explains reasoning in plain language,
- alerts on significant deviations.

**Novelty:** Multiple forecasters produce candidate forecasts. A **Critic Agent** probes where each is brittle. An **Arbiter Agent** composes a consensus, **states the reasons**, and **lists conditions under which the consensus would flip** (e.g., “if unemployment weekly claims jump >0.5pp and energy prices spike +15%, switch to ETS”). This makes uncertainty **explicit** and actionable.

---

## 2) High‑Level Architecture

```
[FRED + BEA APIs]  -->  [Ingestion (Lambda/Glue)]  -->  [S3 Data Lake + Glue Catalog]
                               |                               |
                               v                               v
                         [Step Functions Orchestrator] --> [Athena/Parquet]
                               |                               |
                               v                               v
                   [Forecasters on SageMaker]  <-->  [Scenario Simulator]
                      (ARIMA/ETS/Prophet/DeepAR)         (feature shocks)
                               |                               |
                               v                               v
                        [Critic Agent (Bedrock, KB)]  --->  [Arbiter Agent (Bedrock)]
                               |                                       |
                               v                                       v
                          [Consensus Forecast + Rationale + Flip Conditions]
                               |
                               v
                  [Dashboard (QuickSight) + Alerts (EventBridge + SNS)]
```

**Key services & why:**
- **Data sources:** FRED & ALFRED (vintage revisions) + BEA APIs for GDP, CPI, PCE, employment, etc. (programmatic access, JSON/XML, full reference docs).   
- **Data lake & catalog:** Amazon S3 + AWS Glue (serverless data integration, crawlers, centralized metadata).   
- **Workflow:** AWS Step Functions (serverless, visual workflow, retries, parallel branches) to orchestrate ingestion, training, scoring, scenario runs, and agent calls.   
- **Forecasting:** Amazon SageMaker with built‑in **DeepAR** and classical baselines (ARIMA/ETS/Prophet via open‑source frameworks). DeepAR supports **known‑future features for what‑if scenarios**.   
  > Note: Official docs state **Amazon Forecast is no longer available to new customers** (existing customers can continue using it). Design therefore centers on SageMaker for broad accessibility. 
- **Generative AI:** **AWS Bedrock** foundation models (e.g., Claude / Amazon Nova / Amazon Titan) for Critic & Arbiter agents. Use **Knowledge Bases** (managed RAG) to ground explanations in authoritative sources (indicator definitions, methodology notes, release calendars).   
- **Safety:** Bedrock **Guardrails** (content filters, denied topics, sensitive info masking) on agent outputs.   
- **Visualization:** Amazon **QuickSight** (serverless BI, interactive drilldowns, embedded runtime filters, email subscriptions/alerts). 

---

## 3) Data Layer — ingest, normalize, and track vintages

### Sources & ingestion
- **FRED/ALFRED:** Pull series (e.g., CPIAUCSL, UNRATE, GDPC1) and their **vintage dates** to preserve “what was known when.” The API supports release/series endpoints for observations and vintage tracking.   
- **BEA:** Use BEA API datasets (NIPA, GDP by Industry, PCE, etc.; JSON/XML; documented parameters).   

**How it works:**  
- **Step Functions** nightly state machine:  
  1) Lambda fetches updates from FRED/BEA;  
  2) Writes raw JSON to **S3** in `/raw/fred/<series_id>/` and `/raw/bea/<dataset>/`;  
  3) Triggers **Glue crawlers** to update the **Glue Data Catalog**;  
  4) **Glue jobs** standardize to Parquet, enrich with metadata (units, frequency, seasonal flags, source, vintage), and persist to `/curated/<domain>/<series>/`. 

### Query layer
- **Athena** over S3 Parquet + Glue Catalog for fast dashboard queries and feature extraction for models. (Glue & Athena are designed to work with S3 data lakes). 

---

## 4) Forecasting Layer — multiple forecasters and scenario simulator

### Forecasters (short‑term horizon: 3–12 months)
- **Classical baselines** per indicator:
  - **ARIMA**, **ETS** (robust, interpretable on short horizons). (Via statsmodels in SageMaker processing jobs.) 
  - **Prophet** (additive trend/seasonality for monthly/weekly series). (Open‑source model, commonly used in AWS notebooks.) 
- **ML/global model:** **SageMaker DeepAR** (probabilistic, quantile forecasts; handles many related series; supports **known‑future features** like policy dummies, holiday effects, energy prices). Perfect for “what‑if” because it accepts **feature time series known in the future**. 

### Scenario simulator
- A small service that **perturbs features** (e.g., “oil +20% for next 3 months”, “fed funds +50bps”, “payrolls −1%”) and **re‑scores** DeepAR (and any forecaster with exogenous features).  
- This aligns with DeepAR guidance: use feature time series **known in the future** for “what‑if” runs. 

### Backtesting & scoring
- Rolling‑origin backtests per indicator (MAPE/RMSE; pinball loss for quantiles; coverage of p10/p90).  
- Store diagnostics in `/metrics/<model>/<indicator>/`.

---

## 5) AI Debate Layer — Critic & Arbiter agents (Bedrock)

### Knowledge grounding
- Build a **Bedrock Knowledge Base**: ingest BEA methodology PDFs, FRED series metadata, release calendars, and custom “indicator cards” explaining units, lags, seasonal factors. Responses include **citations** to source docs automatically. 

### Critic Agent (Bedrock)
- **Role:** Review each forecast (point & quantiles) with its diagnostics and **challenge** it.
- **Inputs:**  
  - Forecast trace, residual plots, backtest metrics, scenario sensitivities.  
  - Data release notes from the Knowledge Base (e.g., BEA GDP revisions, CPI seasonal adjustments).  
- **Outputs (structured JSON):**  
  - **Brittleness points:** e.g., “ARIMA residuals show autocorrelation at lag 12; vulnerable to seasonality shift.”  
  - **Data gaps:** e.g., “CPI shelter components lag market rents; caution for near‑term turning points.”  
  - **Stress conditions:** conditions that cause large forecast shifts (from scenario simulator).  
- **How it’s built:** Invoke a Bedrock model (e.g., Claude/Nova), pass metrics + retrieved snippets via Knowledge Bases; use **Bedrock Guardrails** to enforce safe outputs and block inappropriate content. 

### Arbiter Agent (Bedrock)
- **Role:** Compose a **consensus forecast** from candidates + Critic notes, **explain why**, and **declare flip conditions** (decision thresholds that would switch the preferred model).  
- **Logic examples:**  
  - Weight models by recent backtest performance; increase weight for models that were robust under stress; penalize models flagged brittle by the Critic.  
  - Emit **human‑readable rationale**: “DeepAR led due to better quantile coverage and lower pinball loss; ETS serves as secondary if weekly claims trend deteriorates.”  
- **Orchestration:**  
  - Use **Bedrock Agents** with **custom orchestrator** (Lambda) to control how Critic and Arbiter interact (ReAct or ReWOO patterns) and to gate calls to scenario testing and RAG retrieval for citations. 

> If you want to go further, AWS has sample guidance for **multi‑agent orchestration** (AgentCore/Strands), showing patterns for a central orchestrator routing to domain agents—good inspiration for your Critic/Arbiter choreography. 

---

## 6) Consensus Output — what the Arbiter produces

**Artifacts per indicator:**
1) **Consensus forecast** (point & quantiles) for 3–12 months.  
2) **Rationale card** (plain language, cited to KB sources): “What drives the result?”  
3) **Flip conditions** (explicit thresholds): “If X happens, swap preferred model.”  
4) **Scenario map**: pre‑computed responses to standard shocks (rates, energy, demand).

All four are written to `/consensus/<indicator>/` and surfaced in the dashboard.

---

## 7) Dashboard UX (QuickSight)

**Pages & interactions:**
- **Trends:** Historical charts by indicator; drilldown by frequency & source; vintage toggle. (QuickSight supports interactive dashboards with drilldowns and filters.)   
- **Forecasts:** Overlay individual models + consensus quantiles; hover tooltips with model metrics.  
- **What‑if panel:** Sliders/dropdowns for shocks; on change, Step Functions triggers scenario simulator and updates visuals; embedded runtime filter methods (via QuickSight Embedding SDK).   
- **Explain tab:** Arbiter’s rationale (plain language), citations from KB; the **flip conditions** are displayed prominently (green/yellow/red bands).  
- **Alerts:** Subscriptions for material deviations (QuickSight supports email subscriptions/alerts), plus SNS push if incoming reality diverges from forecast beyond threshold. 

---

## 8) Alerts & Deviation Monitoring

- **EventBridge** schedules daily updates; **Lambda** compares latest BEA/FRED values vs consensus forecast envelopes (e.g., p10–p90).  
- If **outside envelope** or trend change detected, publish **SNS** notification and update a “Deviation Flag” on the dashboard.  
- QuickSight email subscriptions keep stakeholders notified of dashboard changes. 

---

## 9) Data Governance & Safety

- **IAM** least‑privilege roles for Glue, Step Functions, SageMaker, Bedrock; **KMS** encryption for S3/Glue/Athena outputs.  
- **Bedrock Guardrails**: block undesired topics/terms; mask any sensitive info (e.g., if user uploads notes) and mitigate hallucination via contextual grounding.   
- **Knowledge Bases** provide **citations** to original documents; explanations are grounded rather than free‑form. 

---

## 10) Worked Example — CPI (Monthly)

1) **Ingest** CPI series + shelter/oil proxies from FRED; methodology notes from BLS/BEA into KB.   
2) **Forecasters:**  
   - ARIMA/ETS trained on CPI core series;  
   - **DeepAR** trained on CPI components with **future‑known features** (seasonal dummies, holiday, oil price scenario path, policy dummies).   
3) **Critic Agent:** Flags ARIMA brittle due to residual seasonality; notes shelter lag; requests shock test: “oil +20% for 2 months.”  
4) **Scenario simulator:** Re‑scores DeepAR with shocked oil path.  
5) **Arbiter Agent:** Picks DeepAR consensus (better quantile coverage), sets **flip condition**: “If WTI > $110 and weekly claims trend up for 4 weeks, prefer ETS.”  
6) **Dashboard:** Users slide “oil +20%,” see consensus shift; explanation reads with citations.

---

## 11) Success Metrics (Hackathon scoring)

- **Usability & insights:** time to run scenarios (<10s), clarity of flip conditions, drilldowns.  
- **Clarity of AI explanations:** Rationale card uses KB citations, concise language, no jargon. (KB supports grounded answers with citations.)   
- **Alerting:** deviation caught within 24h of new release; email/SNS delivered.

---

## 12) Phased Build Plan (MVP → Plus)

**MVP (Day 1–2):**
- Ingest 3–5 indicators (e.g., CPI, GDP, UNRATE).  
- Train ARIMA/ETS/Prophet + one **DeepAR** model on SageMaker.   
- Build **Critic** (Bedrock + KB) and simple **Arbiter** with weighted logic; guard outputs with **Guardrails**.   
- QuickSight dashboard: trends, forecast overlays, basic scenario sliders, alert subscriptions. 

**Plus (Stretch):**
- Add indicator‑specific **flip conditions** generator (Arbiter emits formal thresholds).  
- Multi‑agent **custom orchestrator** in Bedrock Agents for tight control over tool calling & latency.   
- Expand scenarios: policy rate path, fiscal impulse proxy, energy shock, supply chain index.

---

## 13) Why this feels “new” (vs. old dashboards)

- **Epistemic uncertainty surfaced:** Not just a forecast line—**debated**, stress‑tested, and **conditioned** on flip thresholds.  
- **Grounded explanations:** KB citations to BEA/FRED docs in plain language; less hand‑wavy narrative.   
- **Interactive agency:** Users can **negotiate** with the forecast via scenario sliders and instantly see the Arbiter’s stance update.  
- **Safety & governance baked in:** Bedrock **Guardrails** mitigate risky outputs while keeping explanations useful. 

---

## 14) Implementation Notes (no code, just how)

- **Model registry & metrics:** Store backtest results and metadata in S3/Athena; expose them to agents for critique.  
- **Prompt contracts:** Critic returns structured JSON (brittleness, stress summary, data issues). Arbiter consumes those plus metrics to emit consensus + flip conditions.  
- **Rationales:** Keep them short, cite KB sources (e.g., “BEA methodology table X; FRED release notes Y”).   
- **Orchestration:** Step Functions branches:  
  - A) nightly data update → retrain if drift;  
  - B) on demand scenario run → critic → arbiter → write consensus.

---

## 15) Risks & Mitigations

- **Amazon Forecast availability:** Some docs indicate it’s not available to new customers; rely on SageMaker DeepAR + classical baselines.   
- **Latency of multi‑agent calls:** Use **custom orchestrator** (Bedrock Agents) to reduce unnecessary tool hops; cache KB retrievals; prefer **ReWOO** for predictable steps.   
- **Data revisions:** Use ALFRED vintage tracking to explain why historical values changed; show “as‑of” timelines. 

---

## 16) Demo Script (on stage)

1) Show CPI trend with vintage toggle (ALFRED).   
2) Switch to Forecasts tab: ARIMA/ETS/Prophet vs. DeepAR; highlight quantile bands.   
3) Click **Critic** → see “brittleness points” & stress summary.  
4) Slide “oil +20%”: scenario simulator updates; **Arbiter** explains why consensus shifts and states the **flip condition**.  
5) Trigger an **alert** by loading a new release that falls outside the band; show QuickSight subscription email. 

---

### What I need from you to tailor the MVP
- Which 3–5 indicators do you want to spotlight (e.g., CPI, PCE, GDP, UNRATE, payrolls)?  
- Any specific scenario sliders you want (policy rate path, energy price shock, employment shock, consumer sentiment)?

