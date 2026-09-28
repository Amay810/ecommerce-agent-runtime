# -*- coding: utf-8 -*-
"""Stdlib-only tests for the freshness guardrail (no model needed)."""

import unittest
from datetime import datetime, timezone

from ecommerce_rag import freshness

NOW = datetime(2026, 6, 12, tzinfo=timezone.utc)


def _snap(updated_at):
    return {"products": [{
        "doc_id": "product:P1", "title": "x", "price": 99,
        "inventory": "现货", "version": None,
        "default_updated_at": updated_at,
    }], "policies": []}


class FreshnessTests(unittest.TestCase):
    def test_detect_claims(self):
        assert freshness.detect_claims("价格 99 元，现货") == {"price", "inventory"}
        assert freshness.detect_claims("支持七天无理由退货") == {"policy"}
        assert freshness.detect_claims("这款耳机适合跑步") == set()  # no transactional claim

    def test_no_claim_not_triggered(self):
        v = freshness.assess(_snap("2026-06-10"), "product_qa", "这款耳机适合跑步", now=NOW)
        assert v["triggered"] is False and v["status"] == "n/a"

    def test_fresh_path(self):
        v = freshness.assess(_snap("2026-06-10"), "recommend", "价格 99 元", now=NOW, max_age_days=30)
        assert v["status"] == "fresh"

    def test_stale_path(self):
        v = freshness.assess(_snap("2026-01-01"), "recommend", "价格 99 元", now=NOW, max_age_days=30)
        assert v["status"] == "stale"
        assert v["reasons"]

    def test_unverified_path(self):
        v = freshness.assess(_snap(None), "recommend", "价格 99 元", now=NOW)
        assert v["status"] == "unverified"

if __name__ == "__main__":
    unittest.main()
