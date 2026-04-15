"""Tests for _numeric_delta (Task 3.1) — Delta Engine numeric computation."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import _numeric_delta


class TestNumericDeltaDirection:
    """Direction should reflect mean comparison."""

    def test_up_when_current_mean_higher(self):
        result = _numeric_delta([10.0, 20.0], [5.0, 10.0])
        assert result["direction"] == "up"

    def test_down_when_current_mean_lower(self):
        result = _numeric_delta([5.0, 10.0], [10.0, 20.0])
        assert result["direction"] == "down"

    def test_unchanged_when_means_equal(self):
        result = _numeric_delta([10.0, 20.0], [10.0, 20.0])
        assert result["direction"] == "unchanged"

    def test_unchanged_with_different_values_same_mean(self):
        # [5, 15] mean=10, [10, 10] mean=10
        result = _numeric_delta([5.0, 15.0], [10.0, 10.0])
        assert result["direction"] == "unchanged"


class TestNumericDeltaMagnitude:
    """Magnitude should be absolute difference of means."""

    def test_positive_magnitude(self):
        result = _numeric_delta([20.0], [10.0])
        assert result["magnitude"] == pytest.approx(10.0)

    def test_magnitude_always_positive(self):
        result = _numeric_delta([10.0], [20.0])
        assert result["magnitude"] == pytest.approx(10.0)
        assert result["magnitude"] >= 0

    def test_zero_magnitude_when_equal(self):
        result = _numeric_delta([7.5, 12.5], [7.5, 12.5])
        assert result["magnitude"] == pytest.approx(0.0)


class TestNumericDeltaMeans:
    """current_mean and prior_mean should be correct."""

    def test_means_computed(self):
        result = _numeric_delta([10.0, 30.0], [5.0, 15.0])
        assert result["current_mean"] == pytest.approx(20.0)
        assert result["prior_mean"] == pytest.approx(10.0)

    def test_single_element_lists(self):
        result = _numeric_delta([42.0], [17.0])
        assert result["current_mean"] == pytest.approx(42.0)
        assert result["prior_mean"] == pytest.approx(17.0)


class TestNumericDeltaPeriodsAffected:
    """periods_affected counts periods where values differ."""

    def test_all_periods_changed(self):
        result = _numeric_delta([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])
        assert result["periods_affected"] == 3

    def test_no_periods_changed(self):
        result = _numeric_delta([1.0, 2.0], [1.0, 2.0])
        assert result["periods_affected"] == 0

    def test_some_periods_changed(self):
        result = _numeric_delta([1.0, 99.0, 3.0], [1.0, 2.0, 3.0])
        assert result["periods_affected"] == 1

    def test_unequal_lengths_extra_counted(self):
        # 2 common periods (both differ) + 1 extra in current
        result = _numeric_delta([10.0, 20.0, 30.0], [1.0, 2.0])
        assert result["periods_affected"] == 3

    def test_prior_longer_than_current(self):
        result = _numeric_delta([1.0], [1.0, 2.0, 3.0])
        assert result["periods_affected"] == 2  # 0 common diffs + 2 extra


class TestNumericDeltaReturnStructure:
    """Return dict should have all required keys."""

    def test_all_keys_present(self):
        result = _numeric_delta([1.0], [2.0])
        assert set(result.keys()) == {
            "direction", "magnitude", "current_mean", "prior_mean", "periods_affected"
        }
