"""Quick smoke test for narrative generator functions."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import (
    _format_evidence_link,
    _extract_evidence_links,
    _build_narrative_prompt,
    _template_narrative,
)


def test_format_evidence_link():
    result = _format_evidence_link("FRED", "GDP", "2024-06-01", 105.0)
    assert result == "[FRED:GDP, 2024-06-01, 105.0]"


def test_format_evidence_link_bea():
    result = _format_evidence_link("BEA", "BEA_T10101_A191RL", "2024-01-01", 3.2)
    assert result == "[BEA:BEA_T10101_A191RL, 2024-01-01, 3.2]"


def test_extract_evidence_links_basic():
    delta = {
        "series_id": "GDP",
        "vintage_date_new": "2024-06-01",
        "vintage_date_old": "2024-03-01",
        "direction": "up",
        "magnitude": 1.5,
        "prior_mean": 100.0,
    }
    links = _extract_evidence_links("some narrative text", delta)
    assert len(links) >= 1
    link = links[0]
    assert link["source"] == "FRED"
    assert link["series_id"] == "GDP"
    assert link["date"] == "2024-06-01"
    assert "label" in link
    assert "value" in link


def test_extract_evidence_links_with_old_vintage():
    delta = {
        "series_id": "GDP",
        "vintage_date_new": "2024-06-01",
        "vintage_date_old": "2024-03-01",
        "direction": "up",
        "magnitude": 1.5,
        "prior_mean": 100.0,
    }
    links = _extract_evidence_links("text", delta)
    assert len(links) == 2
    assert links[1]["date"] == "2024-03-01"


def test_extract_evidence_links_no_old_vintage():
    delta = {
        "series_id": "GDP",
        "vintage_date_new": "2024-06-01",
        "vintage_date_old": None,
        "direction": "initial",
        "magnitude": 100.0,
    }
    links = _extract_evidence_links("text", delta)
    assert len(links) == 1


def test_extract_evidence_links_bea_source():
    delta = {
        "series_id": "BEA_T10101_A191RL",
        "vintage_date_new": "2024-06-01",
        "vintage_date_old": None,
        "direction": "up",
        "magnitude": 2.0,
    }
    links = _extract_evidence_links("text", delta)
    assert links[0]["source"] == "BEA"


def test_build_narrative_prompt():
    delta = {
        "series_id": "GDP",
        "vintage_date_new": "2024-06-01",
        "direction": "up",
        "magnitude": 1.5,
        "driver_explanation": "Strong consumer spending",
    }
    forecast = {
        "indicator_id": "GDP",
        "periods": [
            {"period_date": "2024-09-01", "point_value": 102.0,
             "upper_bound": 104.0, "lower_bound": 100.0},
        ],
    }
    meta = {"label": "Gross Domestic Product"}
    prompt = _build_narrative_prompt(delta, forecast, meta)
    assert "Gross Domestic Product" in prompt
    assert "GDP" in prompt
    assert "up" in prompt
    assert "FRED:GDP" in prompt


def test_template_narrative():
    delta = {
        "series_id": "GDP",
        "vintage_date_new": "2024-06-01",
        "direction": "up",
        "magnitude": 1.5,
        "current_mean": 101.5,
        "prior_mean": 100.0,
    }
    forecast = {
        "indicator_id": "GDP",
        "periods": [
            {"period_date": "2024-09-01", "point_value": 102.0,
             "upper_bound": 104.0, "lower_bound": 100.0},
        ],
    }
    text = _template_narrative(delta, forecast)
    assert "Gross Domestic Product" in text
    assert "moved up" in text
    assert "1.5" in text
    assert "100.0" in text
    assert "101.5" in text
    assert "[FRED:GDP" in text
    assert "102.0" in text


def test_template_narrative_down():
    delta = {
        "series_id": "UNRATE",
        "vintage_date_new": "2024-06-01",
        "direction": "down",
        "magnitude": 0.3,
        "current_mean": 3.5,
        "prior_mean": 3.8,
    }
    forecast = {"indicator_id": "UNRATE", "periods": []}
    text = _template_narrative(delta, forecast)
    assert "moved down" in text
    assert "No forecast periods available" in text


def test_template_narrative_unchanged():
    delta = {
        "series_id": "GDP",
        "vintage_date_new": "2024-06-01",
        "direction": "unchanged",
        "magnitude": 0.0,
        "current_mean": 100.0,
        "prior_mean": 100.0,
    }
    forecast = {"indicator_id": "GDP", "periods": []}
    text = _template_narrative(delta, forecast)
    assert "remained unchanged" in text
