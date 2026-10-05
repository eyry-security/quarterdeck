"""Tests for prompt-cache tracking in usage logging (vector/quarterdeck-cache-tracking)."""
import json

import pytest


@pytest.fixture
def home(tmp_path):
    return tmp_path


def _pinnace_rec(**kw):
    """A Pinnace-style usage record with normalized cache token keys."""
    r = {
        "usage": {
            "input_tokens": 10000,
            "output_tokens": 500,
            "uncached_input_tokens": 1000,
            "cache_read_tokens": 8000,
            "cache_write_5m_tokens": 1000,
            "cache_write_1h_tokens": 0,
            "cache_write_unclassified_tokens": 0,
        },
        "cost": {"amount": None, "status": "usage_unavailable"},
        "model": "claude-haiku-4-5",
        "model_ref": "anthropic:claude-haiku-4-5",
    }
    r.update(kw)
    return r


class TestCacheCapture:
    def test_cache_tokens_recorded(self, home):
        from quarterdeck import usage as u
        n = u.record_pinnace_usage("scout", [_pinnace_rec()], home=home)
        assert n == 1
        line = (home / "usage.jsonl").read_text().strip()
        rec = json.loads(line)
        assert rec["cache_read_tokens"] == 8000
        assert rec["cache_write_tokens"] == 1000
        assert rec["uncached_input_tokens"] == 1000

    def test_native_anthropic_field_names(self, home):
        from quarterdeck import usage as u
        rec = _pinnace_rec(usage={
            "input_tokens": 10000,
            "output_tokens": 500,
            "cache_read_input_tokens": 8000,
            "cache_creation_5m_input_tokens": 1000,
        })
        u.record_pinnace_usage("scout", [rec], home=home)
        stored = json.loads((home / "usage.jsonl").read_text().strip())
        assert stored["cache_read_tokens"] == 8000
        assert stored["cache_write_tokens"] == 1000

    def test_records_without_cache_still_work(self, home):
        from quarterdeck import usage as u
        rec = _pinnace_rec(usage={"input_tokens": 100, "output_tokens": 50})
        n = u.record_pinnace_usage("scout", [rec], home=home)
        assert n == 1
        stored = json.loads((home / "usage.jsonl").read_text().strip())
        assert stored["cache_read_tokens"] == 0
        assert stored["cache_write_tokens"] == 0


class TestCachePricing:
    def test_fallback_cost_uses_cache_pricing(self, home):
        from quarterdeck import usage as u
        # 10k input all uncached @ $1/M = $0.01
        assert u._fallback_cost("claude-haiku-4-5", 10000, 0) == pytest.approx(0.01)
        # 10k input all cache-read @ 0.1x = $0.001
        c = u._fallback_cost("claude-haiku-4-5", 10000, 0, cache_read=10000)
        assert c == pytest.approx(0.001)
        # 10k input all cache-write @ 1.25x = $0.0125
        c = u._fallback_cost("claude-haiku-4-5", 10000, 0, cache_write=10000)
        assert c == pytest.approx(0.0125)

    def test_fallback_cost_recorded_with_cache(self, home):
        from quarterdeck import usage as u
        u.record_pinnace_usage("scout", [_pinnace_rec()], home=home)
        s = u.summary(home=home)
        # uncached 1000 @ $1/M = 0.001; out 500 @ $5/M = 0.0025;
        # read 8000 @ $0.1/M = 0.0008; write 1000 @ $1.25/M = 0.00125
        expected = 0.001 + 0.0025 + 0.0008 + 0.00125
        assert s["cost_usd"] == round(expected, 4)
        assert s["cost_estimated"] is True


class TestCacheSummary:
    def test_hit_rate_and_savings(self, home):
        from quarterdeck import usage as u
        u.record_pinnace_usage("scout", [_pinnace_rec()], home=home)
        s = u.summary(home=home)
        assert s["cache_read_tokens"] == 8000
        assert s["cache_write_tokens"] == 1000
        # hit rate = read / (read + uncached) = 8000/9000
        assert s["cache_hit_rate"] == round(8000 / 9000, 4)
        # savings = no-cache cost - actual cost
        # no-cache: 10000/1e6*1.0 + 500/1e6*5.0 = 0.0125
        # actual: 0.00555 (see above)
        assert s["cache_savings_usd"] == round(0.0125 - 0.00555, 4)
        assert s["by_agent"]["scout"]["cache_read_tokens"] == 8000

    def test_hit_rate_none_without_input(self, home):
        from quarterdeck import usage as u
        s = u.summary(home=home)
        assert s["cache_hit_rate"] is None
        assert s["cache_read_tokens"] == 0

    def test_hit_rate_zero_when_no_cache(self, home):
        from quarterdeck import usage as u
        rec = _pinnace_rec(usage={"input_tokens": 100, "output_tokens": 50})
        u.record_pinnace_usage("scout", [rec], home=home)
        s = u.summary(home=home)
        assert s["cache_hit_rate"] == 0.0
        assert s["cache_savings_usd"] == 0.0


class TestCacheApi:
    def test_usage_api_returns_cache_section(self, home):
        from fastapi.testclient import TestClient
        from quarterdeck.web import create_app
        from quarterdeck import usage as u
        u.record_pinnace_usage("scout", [_pinnace_rec()], home=home)
        client = TestClient(create_app(home=home))
        r = client.get("/api/usage")
        assert r.status_code == 200
        body = r.json()
        assert body["cache"]["hit_rate"] == round(8000 / 9000, 4)
        assert body["cache"]["read_tokens"] == 8000
        assert body["cache"]["write_tokens"] == 1000
        assert body["cache"]["savings_usd"] > 0
        # summary also carries the fields for the top bar
        assert body["summary"]["cache_hit_rate"] is not None
