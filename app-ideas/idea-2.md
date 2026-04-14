**Design: AI‑Generated Structural Causal Graph (SCG) + Counterfactual Forecasting Dashboard**  
*(Economic Indicator Forecasting for FRED + BEA data; AWS ML for short‑term forecasts; generative explanations that are mechanistic—not post‑hoc)*

---

## 1) Experience goals & novelty

**What the user sees**

- A single interactive dashboard with:
  - **Historical trends** for CPI, unemployment, GDP, PCE, wages, energy prices, etc. pulled live from FRED/BEA.   
  - **Probabilistic short‑term forecasts** (the next 3–12 months/quarters) with prediction intervals and model cards. Powered by **SageMaker/GluonTS** (DeepAR/TFT) rather than classical single‑series models.   
  - A **“What‑if” scenario composer** (e.g., “keep unemployment at 4% while energy prices +15%”) that runs **counterfactual simulations** on a **structural causal graph (SCG)**. Narratives explain *how* and *why* forecast changes propagate. Explanations follow **Pearl’s abduction–action–prediction** cycle.   
  - **Alerting** for significant deviations (e.g., CPI regime shift, output gaps) triggered by **online change‑point detection** and retrospective segmentation. 

**What makes this novel**

- The SCG itself is **AI‑generated and maintained** from macroeconomic canon + current data, then **stress‑tested** with data‑driven **causal discovery** (NOTEARS, PCMCI+, Granger). Explanations are **mechanistic** (structural equations with causal edges) rather than generic feature importance. 

---

## 2) High‑level system architecture

**A. Data layer (FRED + BEA)**  
- **Connectors** pull series via official APIs; support metadata (frequency, units), and **vintage/ALFRED** revisions for “what was known when.”   
- **BEA datasets** (NIPA tables, GDP by industry, PCE details) via BEA’s API; Python **beaapi** library optional.   
- **Series catalogue** includes common macro indicators (e.g., CPI all items, unemployment rate, payrolls) with canonical FRED IDs referenced for discoverability. 

**B. Forecasting services (AWS ML)**  
- **SageMaker** runs **DeepAR/TFT** probabilistic models for short‑term multi‑series forecasts (global models that learn across indicators).   
- Note: **Amazon Forecast** is now **unavailable to new customers**; teams should use SageMaker/GluonTS or Canvas‑time‑series for similar capabilities. 

**C. Causal reasoning & validation**  
- **LLM‑Causal Architect** drafts an SCG using macroeconomic priors (e.g., CPI → real wages → consumption → GDP) grounded in **Structural Causal Models (SCM)**.   
- **Discovery stack**:  
  - **NOTEARS** (score‑based DAG learning) for contemporaneous structure from tabular snapshots.   
  - **PCMCI+** for lagged & contemporaneous links in autocorrelated time series.   
  - **Granger tests** (diagnostic predictive causality) as a sanity check.   
- **Governance engine** fuses priors + discovery output; flags conflicts; logs confidence and assumptions.

**D. Counterfactual simulation**  
- Structural equations are estimated where possible using **DoWhy/EconML** (orthogonal/DR methods; IVs) to provide **identifiable** effect estimates; counterfactuals computed by **abduction–action–prediction** on the SCM. 

**E. Alerts & monitoring**  
- **Bayesian Online Change Point Detection** for streaming regime breaks; **ruptures** for offline segmentation of historical series. 

**F. UX layer**  
- **Interactive exploration**: time‑brushed plots, credible intervals, graph navigator (hover to see causal paths), scenario composer, and **explanation cards** that trace the causal chain and quantify uncertainty.

---

## 3) Data scope, ingestion & normalization

- **FRED ingest**: “series/observations” endpoints per ID; add **ALFRED vintages** to support “as‑of” analyses and revision awareness.   
- **BEA ingest**: NIPA tables (e.g., real PCE 2.3.6), Regional/GDP‑by‑industry; respect BEA parameter schema (Frequency, Year, TableName).   
- **Temporal harmonization**: convert to common frequencies (monthly/quarterly), seasonal adjustment alignment, unit normalization, lag embeddings for causal discovery (**PCMCI+** benefits from explicit lagged features).   
- **Catalog examples** (non‑exhaustive): CPI (All Items), Unemployment rate (UNRATE), Payrolls (PAYEMS), Wages (CES… columns), Energy CPI subindex; curated via FRED catalogue references. 

---

## 4) AI‑Generated SCG: how it works

**Step 1: Draft causal map (LLM)**  
- The LLM ingests macro priors (textual canon + indicator descriptions) and proposes a **directed acyclic graph** (DAG) specifying parents/children (e.g., **energy prices → headline CPI → real wages → consumption (PCE) → GDP**). The modeling language is **Pearl‑style SCM** with functional relationships and exogenous shocks. 

**Step 2: Stress‑test with data discovery**  
- Run **NOTEARS** to learn sparse contemporaneous edges (scale constraints and thresholds applied; treat as suggestions).   
- Run **PCMCI+** on lagged series to orient edges using time order and conditional independence in autocorrelated data; add contemporaneous orientations where supported.   
- Run **Granger** (statsmodels) for diagnostic predictive causality; reject edges that fail across lags (with stationarity checks upstream). 

**Step 3: Fuse & adjudicate**  
- **Consensus logic**:
  - Keep edges strongly supported by at least two methods or by priors + one method.  
  - Mark **contentious edges** for analyst review; log sensitivity.  
  - Estimate functional forms for kept edges (nonlinear where needed) via **EconML/DoWhy**; store parameter posteriors and uncertainty bands. 

**Step 4: Guardrails & caveats**  
- Show **method limitations**: e.g., **NOTEARS scale non‑invariance** concerns and **PCMCI+ robustness** issues on limited/extreme datasets; prompt users to treat weak edges cautiously. 

---

## 5) Forecasting engine (short‑term horizons)

- **Global probabilistic models** (DeepAR/TFT) trained on multiple indicators yield coherent multi‑series forecasts with **quantile outputs**; these models often outperform local ARIMA/ETS on large related series sets.   
- **Feature design**: known‑in‑advance covariates (holidays, scheduled release calendars) feed the dynamic features path (supported in DeepAR/TFT).   
- **Model governance**: metrics (sMAPE, CRPS), backtests, distribution drift checks; present **model cards** summarizing data, assumptions, and fit. (Canvas can support table‑based time series forecasting with what‑if on future covariates if a no‑code path is needed.) 

---

## 6) Counterfactuals & scenario composer

**Mechanism (Pearl’s A–A–P)**  
- **Abduction**: condition the SCM on latest observations to infer latent shocks (e.g., demand shock in PCE).   
- **Action**: intervene with **do()** (e.g., do(unemployment=4%), do(energy CPI=+15%)). Sever incoming edges into treatment nodes per DAG semantics.   
- **Prediction**: propagate through estimated structural equations to obtain counterfactual outcomes (CPI, GDP paths), with uncertainty from parameter posteriors. 

**Example user flow**  
> *“What would inflation be if unemployment stayed at 4% but energy prices rose 15%?”*  
1) User sets constraints; system validates feasible ranges (historical envelope).  
2) Engine runs A–A–P and returns CPI quantile path, shows **causal chain** (energy → CPI; unemployment → wages → consumption) and **assumptions**.  
3) UI surfaces **contrastive explanation** (Δ vs. baseline forecast) and an **attribution table** (edge‑wise contributions) sourced from SCM estimates (**EconML/DoWhy**). 

---

## 7) Alerts for significant deviations

- **Streaming alerts**: Bayesian Online Change‑Point Detection monitors latest releases; spikes in run‑length posterior trigger alerts for regime changes (e.g., sudden CPI deceleration).   
- **Batch segmentation**: **ruptures** detects change points in mean/variance/trend over history; used to annotate plots and explain breaks (e.g., post‑pandemic shifts).   
- Alerts include **narratives** pointing to likely causal drivers via SCG paths.

---

## 8) Generative **mechanistic** explanations

- For each forecast or scenario, generate an **Explanation Card**:
  - *Causal Path*: chain of nodes and edges responsible for the change; provenance (prior vs. discovery evidence).  
  - *Assumptions*: identification (backdoor, IV), data sources, stationarity checks, revisions/vintages. (ALFRED handling makes data‑revision effects explicit.)   
  - *Uncertainty*: intervals, sensitivity to alternative edge sets (stress tests).  
  - *Plain‑language narrative*: grounded in SCM and **do‑calculus** semantics. 

---

## 9) Dashboard layout & user interactions

1) **Overview tab**:  
   - KPI tiles (CPI, GDP growth, unemployment), historical charts with annotated change points and forecast fans.   
2) **Causal graph tab**:  
   - Interactive SCG; click node to see parents/children, edge confidence, estimation details (**EconML/DoWhy** links).   
3) **Scenario tab**:  
   - Drag‑and‑set interventions; results side‑by‑side with baseline; download explanation card.  
4) **Alerts tab**:  
   - Timeline of detected changes (BOCPD + ruptures) with impact summaries. 

---

## 10) Governance, limitations & ethics

- **Data revisions**: show vintage selection in UI to avoid hindsight bias.   
- **Causal discovery pitfalls**: note **NOTEARS** scale sensitivity and **PCMCI+** robustness constraints—edges marked “tentative” unless triangulated.   
- **Predictive vs. causal**: **Granger** is predictive causality only—diagnostic, not proof of causation; used as a weak filter.   
- **Identification**: SCG edges used for counterfactuals must pass identification checks (backdoor/IV) in **DoWhy/EconML**; otherwise scenarios labeled **exploratory**. 

---

## 11) Success metrics & evaluation

- **Usability & insight**  
  - Time‑to‑insight (median seconds to craft a scenario).  
  - Comprehension: user quiz on explanation cards (e.g., “which path moved CPI most?”).  
- **Clarity of AI explanations**  
  - Explanation rating rubric (accuracy, completeness, uncertainty transparency).  
- **Forecast quality**  
  - Backtest CRPS/Pinball loss; monitor stability across vintages. (DeepAR/TFT model card displays.)   
- **Alert utility**  
  - Precision/recall of change‑point alerts on known regime shifts; analyst validation. 

---

## 12) Delivery plan (hackathon scope)

**Day 1–2: Data + baseline forecasts**  
- Wire FRED/BEA connectors; import 6–10 core series; train **TFT/DeepAR** for 3–6 month horizons; surface forecast fans. 

**Day 2–3: Draft SCG + validation**  
- LLM produces initial SCG; run **PCMCI+** and **Granger** to accept/reject key edges; visually render graph. 

**Day 3–4: Counterfactuals + alerts + narratives**  
- Implement **A–A–P** counterfactuals for selected levers (unemployment, energy CPI); add **BOCPD/ruptures** alerts; generate explanation cards. 

**Stretch**: include **EconML** estimation where identification is feasible (e.g., IVs for energy price pass‑through). 

---

## 13) Example explanation card (mechanistic narrative)

**Scenario**: *do(unemployment=4%)* & *do(energy CPI=+15%)*, next 4 quarters  
- **Causal path**: Energy CPI ↑ → headline CPI ↑ (pass‑through) → real wages ↓ → PCE growth ↓ → GDP growth ↓. (Edges supported by macro priors + PCMCI+ orientation; Granger positive at lag 1 for CPI→PCE).   
- **Model outputs**: CPI median path +0.7 pp above baseline at Q+2 (90% PI shown) from **TFT**; GDP median −0.2 pp at Q+3.   
- **Assumptions**: Identification via backdoor adjustment using observed confounders; scenario validity limited by NOTEARS scale sensitivity; flagged edges reviewed.   
- **Uncertainty**: Contributions reported as ranges (quantile decomposition from SCM).  
- **Why it matters**: Explains *mechanism* rather than just “feature importance,” enabling policy reasoning under interventions. 

---

## 14) Implementation notes (non‑code)

- **Ingestion**: use FRED V2 endpoints; store **realtime_start/end** to preserve vintages; unify series units/frequencies.   
- **BEA**: register a free 36‑character API key; call NIPA tables (e.g., Real PCE 2.3.6).   
- **Forecasting**: start with **TFT** due to multi‑feature handling and interpretability; fallback to **DeepAR** where data is sparse.   
- **Discovery**: standardize variables to mitigate **NOTEARS** scale issues; treat NOTEARS as one vote in consensus.   
- **Diagnostics**: run **Granger** with stationarity checks; treat as predictive support only.   
- **Alerts**: BOCPD for online updates; **ruptures** PELT/kernel methods for historical annotation. 

---

## 15) Why this will impress judges

- It **raises the bar** from dashboards that forecast trends to a system that **reasons mechanistically** about the economy using a living SCG, gives **counterfactual answers**, and **justifies** them clearly—meeting *usability*, *insightfulness*, and *clarity of AI explanation* metrics simultaneously.  
- It’s practical: built on **FRED/BEA public APIs** and **AWS ML tooling** available to most teams today. 

---

### Quick follow‑ups for you
- Which **indicator set** should we prioritize for the demo (e.g., CPI, PCE, GDP, unemployment, wages, energy CPI)?  
- Do you want the **no‑code** path (Canvas) demonstrated beside SageMaker/GluonTS, or strictly code‑driven? 

**References**  
FRED API & vintage handling; BEA API & datasets; SageMaker DeepAR/TFT & Canvas time‑series; causal inference (Pearl SCM; DoWhy; EconML); causal discovery (NOTEARS; PCMCI+); Granger causality; change‑point detection (BOCPD; ruptures). 