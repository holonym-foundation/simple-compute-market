"""Unit tests for SQLiteClient._requested_gpu_count.

Container/CPU slices request 0 GPUs — this guards that gpu_count=0 is accepted
(so container reservations work) while negatives / non-integers are rejected and
an unspecified count still defaults to 1 (a GPU demand wanting one GPU).
"""

import pytest

from market_storefront.utils.sqlite_client import SQLiteClient


class TestRequestedGpuCount:
    def test_none_or_empty_defaults_to_one(self):
        assert SQLiteClient._requested_gpu_count(None) == 1
        assert SQLiteClient._requested_gpu_count({}) == 1

    def test_zero_allowed_for_container(self):
        assert SQLiteClient._requested_gpu_count({"gpu_count": 0}) == 0
        assert SQLiteClient._requested_gpu_count({"gpu_count": "0"}) == 0

    def test_positive_passthrough(self):
        assert SQLiteClient._requested_gpu_count({"gpu_count": 4}) == 4

    def test_negative_rejected(self):
        with pytest.raises(ValueError, match=">= 0"):
            SQLiteClient._requested_gpu_count({"gpu_count": -1})

    def test_non_integer_rejected(self):
        with pytest.raises(ValueError, match="integer"):
            SQLiteClient._requested_gpu_count({"gpu_count": "not-a-number"})
