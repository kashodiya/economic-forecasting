# App Ideas

## General Guidelines
- See [coding-agent-prompt.md](coding-agent-prompt.md) for tech stack and constraints that apply to all apps.
- Stack: Python/FastAPI backend, single `index.html` with Vue/Vuetify/VueRouter (CDN), SQLite if needed, AWS Bedrock for LLM.

## App Location & Structure
- All apps live under `economic-forecasting/apps/<folder-name>/`
- Each app follows this structure:
  ```
  economic-forecasting/apps/<folder-name>/
  ├── app.py              # FastAPI backend
  ├── requirements.txt    # Python dependencies
  ├── .env                # Environment variables (git-ignored)
  ├── templates/
  │   └── index.html      # Frontend (Vue/Vuetify/VueRouter via CDN)
  └── static/             # Static assets (if needed)
  ```

## Ideas

| # | Folder | One-liner | Status | Priority | Notes |
|---|--------|-----------|--------|----------|-------|
| 1 | digital-twin | Agentic Digital Twin with swarm simulation + SHAP + synthetic control | Not started | — | |
| 2 | causal-graph | AI-Generated Structural Causal Graph + counterfactual forecasting | Not started | — | |
| 3 | semantic-delta | Semantic Delta from BEA/FRED releases → auto-updated forecasts with evidence links | Not started | — | |
| 4 | eco4 | Expectations Track (narrative-derived beliefs fused with data forecasts, ESI index) | In progress | — | |
| 5 | policy-finder | Inverse Design / Policy Path-Finder with constrained RL | Not started | — | |
| 6 | explain-cards | Multi-Lens Explainability Cards (Drivers, Evidence, Counterfactual, Confidence) | Not started | — | |
| 7 | narrative-alerts | Narrative Alerts with triangulated evidence + suggested next checks | Not started | — | |
| 8 | forecast-debate | AI-Mediated Debate (Critic + Arbiter agents for consensus forecasts with flip conditions) | Not started | — | |
| 9 | composite-index | Adaptive Composite Indicators (AI proposes/backtests new metrics over time) | Not started | — | |
| 10 | story-mode | Story Mode (scene-based narrative: Shock → Expectations → Ripple → Delta → Confidence → Policy) | Not started | — | |
