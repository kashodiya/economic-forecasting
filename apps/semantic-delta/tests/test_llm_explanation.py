"""Tests for Task 3.2 — LLM driver explanation, validation, and fallback."""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import (
    _template_explanation,
    _validate_llm_explanation,
    _generate_driver_explanation,
)


# ── _template_explanation ────────────────────────────────────────────────

class TestTemplateExplanation:
    """_template_explanation produces correct text from numeric delta."""

    def test_up_direction(self):
        delta = {
            "direction": "up",
            "magnitude": 1.5,
            "prior_mean": 100.0,
            "current_mean": 101.5,
            "periods_affected": 3,
        }
        result = _template_explanation(delta)
        assert "moved up" in result
        assert "1.5" in result
        assert "100.0" in result
        assert "101.5" in result
        assert "3 periods" in result

    def test_down_direction(self):
        delta = {
            "direction": "down",
            "magnitude": 2.0,
            "prior_mean": 50.0,
            "current_mean": 48.0,
            "periods_affected": 1,
        }
        result = _template_explanation(delta)
        assert "moved down" in result
        assert "2.0" in result

    def test_unchanged_direction(self):
        delta = {
            "direction": "unchanged",
            "magnitude": 0.0,
            "prior_mean": 10.0,
            "current_mean": 10.0,
            "periods_affected": 0,
        }
        result = _template_explanation(delta)
        assert "unchanged" in result
        assert "10.0" in result


# ── _validate_llm_explanation ────────────────────────────────────────────

class TestValidateLlmExplanation:
    """_validate_llm_explanation catches direction contradictions."""

    def _up_delta(self):
        return {
            "direction": "up",
            "magnitude": 5.0,
            "prior_mean": 100.0,
            "current_mean": 105.0,
            "periods_affected": 2,
        }

    def _down_delta(self):
        return {
            "direction": "down",
            "magnitude": 3.0,
            "prior_mean": 100.0,
            "current_mean": 97.0,
            "periods_affected": 1,
        }

    def test_valid_up_explanation(self):
        is_valid, text = _validate_llm_explanation(
            "GDP increased due to strong consumer spending.", self._up_delta()
        )
        assert is_valid is True
        assert text == "GDP increased due to strong consumer spending."

    def test_invalid_up_with_decreased(self):
        is_valid, text = _validate_llm_explanation(
            "GDP decreased sharply this quarter.", self._up_delta()
        )
        assert is_valid is False
        assert "moved up" in text  # template fallback

    def test_invalid_up_with_declined(self):
        is_valid, text = _validate_llm_explanation(
            "The indicator declined due to weak demand.", self._up_delta()
        )
        assert is_valid is False

    def test_invalid_up_with_fell(self):
        is_valid, text = _validate_llm_explanation(
            "Output fell below expectations.", self._up_delta()
        )
        assert is_valid is False

    def test_invalid_up_with_dropped(self):
        is_valid, text = _validate_llm_explanation(
            "Consumer confidence dropped.", self._up_delta()
        )
        assert is_valid is False

    def test_valid_down_explanation(self):
        is_valid, text = _validate_llm_explanation(
            "GDP decreased due to reduced investment.", self._down_delta()
        )
        assert is_valid is True

    def test_invalid_down_with_increased(self):
        is_valid, text = _validate_llm_explanation(
            "GDP increased due to strong exports.", self._down_delta()
        )
        assert is_valid is False
        assert "moved down" in text  # template fallback

    def test_invalid_down_with_rose(self):
        is_valid, text = _validate_llm_explanation(
            "The indicator rose sharply.", self._down_delta()
        )
        assert is_valid is False

    def test_invalid_down_with_grew(self):
        is_valid, text = _validate_llm_explanation(
            "Output grew beyond expectations.", self._down_delta()
        )
        assert is_valid is False

    def test_invalid_down_with_surged(self):
        is_valid, text = _validate_llm_explanation(
            "Consumer spending surged.", self._down_delta()
        )
        assert is_valid is False

    def test_unchanged_always_valid(self):
        delta = {
            "direction": "unchanged",
            "magnitude": 0.0,
            "prior_mean": 10.0,
            "current_mean": 10.0,
            "periods_affected": 0,
        }
        is_valid, text = _validate_llm_explanation(
            "GDP increased but then decreased, netting out.", delta
        )
        assert is_valid is True

    def test_case_insensitive_check(self):
        is_valid, _ = _validate_llm_explanation(
            "GDP DECREASED sharply.", self._up_delta()
        )
        assert is_valid is False


# ── _generate_driver_explanation ─────────────────────────────────────────

class TestGenerateDriverExplanation:
    """_generate_driver_explanation calls Bedrock and handles failures."""

    @pytest.fixture
    def sample_delta(self):
        return {
            "direction": "up",
            "magnitude": 2.5,
            "prior_mean": 100.0,
            "current_mean": 102.5,
            "periods_affected": 3,
        }

    @pytest.mark.asyncio
    async def test_bedrock_success(self, sample_delta):
        """Successful Bedrock call returns LLM explanation."""
        import json

        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({
            "content": [{"text": "GDP rose due to strong consumer spending."}]
        }).encode()

        mock_client = MagicMock()
        mock_client.invoke_model.return_value = {"body": mock_body}

        mock_session = MagicMock()
        mock_session.client.return_value = mock_client

        with patch("boto3.Session", return_value=mock_session):
            explanation, is_valid = await _generate_driver_explanation(
                sample_delta, "GDP"
            )

        assert is_valid is True
        assert "GDP rose" in explanation

    @pytest.mark.asyncio
    async def test_bedrock_failure_falls_back(self, sample_delta):
        """Bedrock exception triggers template fallback."""
        with patch("boto3.Session", side_effect=Exception("Bedrock unavailable")):
            explanation, is_valid = await _generate_driver_explanation(
                sample_delta, "GDP"
            )

        assert is_valid is False
        assert "moved up" in explanation
        assert "2.5" in explanation

    @pytest.mark.asyncio
    async def test_invalid_llm_response_falls_back(self, sample_delta):
        """LLM response contradicting direction triggers template fallback."""
        import json

        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({
            "content": [{"text": "GDP decreased due to weak demand."}]
        }).encode()

        mock_client = MagicMock()
        mock_client.invoke_model.return_value = {"body": mock_body}

        mock_session = MagicMock()
        mock_session.client.return_value = mock_client

        with patch("boto3.Session", return_value=mock_session):
            explanation, is_valid = await _generate_driver_explanation(
                sample_delta, "GDP"
            )

        assert is_valid is False
        assert "moved up" in explanation  # template fallback

    @pytest.mark.asyncio
    async def test_invoke_model_exception_falls_back(self, sample_delta):
        """invoke_model raising an exception triggers template fallback."""
        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = Exception("throttled")

        mock_session = MagicMock()
        mock_session.client.return_value = mock_client

        with patch("boto3.Session", return_value=mock_session):
            explanation, is_valid = await _generate_driver_explanation(
                sample_delta, "GDP"
            )

        assert is_valid is False
        assert "moved up" in explanation
