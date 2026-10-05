"""Token usage tracking for Quarterdeck agents.

Pinnace meters every inference (see its usage records on AgentResult).
This module funnels those records into ~/.quarterdeck/usage.jsonl and
serves aggregates for the UI header.

Record shape (one line per inference):
  {ts, agent, model, model_ref, input_tokens, output_tokens, cost_usd}
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock

from .agent_registry import home_dir

# Fallback $/1M tokens, used when Pinnace could not price a record.
# (Anthropic list prices; override is unnecessary — Pinnace prices first.)
FALLBACK_PRICING = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.25, "output": 1.25},
}

_lock = Lock()


def _usage_file(home: Path | None = None) -> Path:
    base = Path(home) if home else home_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / "usage.jsonl"


def _model_key(model_ref: str | None) -> str:
    ref = (model_ref or "").strip()
    return ref.split(":", 1)[-1] if ref else ""


def _fallback_cost(model_key: str, in_tok: int, out_tok: int) -> float | None:
    prices = FALLBACK_PRICING.get(model_key)
    if not prices:
        return None
    return in_tok / 1e6 * prices["input"] + out_tok / 1e6 * prices["output"]


def record_pinnace_usage(agent: str, records: list[dict],
                         home: Path | None = None) -> int:
    """Persist Pinnace usage records for an agent. Returns count written."""
    if not records:
        return 0
    path = _usage_file(home)
    lines = []
    for r in records:
        try:
            usage = r.get("usage", {}) or {}
            in_tok = int(usage.get("input_tokens") or 0)
            out_tok = int(usage.get("output_tokens") or 0)
            cost = r.get("cost", {}) or {}
            amount = cost.get("amount")
            cost_usd = float(amount) if amount is not None else None
            estimated = False
            if cost_usd is None:
                cost_usd = _fallback_cost(
                    _model_key(r.get("model_ref") or r.get("model")),
                    in_tok, out_tok)
                estimated = cost_usd is not None
            lines.append(json.dumps({
                "ts": time.time(),
                "agent": agent,
                "model": r.get("model") or r.get("model_ref") or "",
                "model_ref": r.get("model_ref") or "",
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost_usd": cost_usd,
                "cost_estimated": estimated,
            }))
        except Exception:
            continue
    if not lines:
        return 0
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return len(lines)


def _iter_records(home: Path | None = None):
    path = _usage_file(home)
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def summary(home: Path | None = None, since_ts: float = 0.0) -> dict:
    """Totals + per-agent breakdown. since_ts filters to recent records."""
    total_in = total_out = 0
    total_cost = 0.0
    cost_priced = True
    by_agent: dict[str, dict] = {}
    for r in _iter_records(home):
        if r.get("ts", 0) < since_ts:
            continue
        in_tok = int(r.get("input_tokens") or 0)
        out_tok = int(r.get("output_tokens") or 0)
        cost = r.get("cost_usd")
        total_in += in_tok
        total_out += out_tok
        agent = r.get("agent", "?")
        a = by_agent.setdefault(agent, {"input_tokens": 0, "output_tokens": 0,
                                        "cost_usd": 0.0, "calls": 0})
        a["input_tokens"] += in_tok
        a["output_tokens"] += out_tok
        a["calls"] += 1
        if cost is None:
            cost_priced = False
        else:
            total_cost += cost
            a["cost_usd"] += cost
            if r.get("cost_estimated"):
                cost_priced = False
    return {
        "input_tokens": total_in,
        "output_tokens": total_out,
        "total_tokens": total_in + total_out,
        "cost_usd": round(total_cost, 4),
        "cost_estimated": not cost_priced,
        "by_agent": by_agent,
    }


def _settings_file(home: Path | None = None) -> Path:
    base = Path(home) if home else home_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / "settings.json"


def get_credit_balance(home: Path | None = None) -> float | None:
    """User's total Anthropic credit balance in USD.

    Env var ANTHROPIC_CREDIT_BALANCE wins; otherwise the UI-stored setting.
    """
    env = os.environ.get("ANTHROPIC_CREDIT_BALANCE", "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    try:
        data = json.loads(_settings_file(home).read_text(encoding="utf-8"))
        bal = data.get("credit_balance_usd")
        return float(bal) if bal is not None else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def set_credit_balance(balance_usd: float | None,
                       home: Path | None = None) -> float | None:
    """Store the credit balance (None clears it). Returns the stored value."""
    path = _settings_file(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        data = {}
    if balance_usd is None:
        data.pop("credit_balance_usd", None)
    else:
        data["credit_balance_usd"] = float(balance_usd)
    with _lock:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data.get("credit_balance_usd")


def burn_rate(home: Path | None = None, window_hours: float = 1.0) -> dict:
    """Tokens/hour and $/hour over the trailing window."""
    since = time.time() - window_hours * 3600
    s = summary(home=home, since_ts=since)
    elapsed_h = window_hours
    # If the first record is newer than the window start, use actual span.
    first_ts = None
    for r in _iter_records(home):
        if r.get("ts", 0) >= since:
            ts = r.get("ts", 0)
            if first_ts is None or ts < first_ts:
                first_ts = ts
    if first_ts is not None:
        elapsed_h = max((time.time() - first_ts) / 3600, 1 / 3600)
    return {
        "window_hours": window_hours,
        "tokens_per_hour": round(s["total_tokens"] / elapsed_h, 1),
        "cost_per_hour": round(s["cost_usd"] / elapsed_h, 4),
        "cost_estimated": s["cost_estimated"],
    }


def credit_eta(home: Path | None = None) -> dict:
    """Project remaining credit lifetime from recent burn.

    Uses the max of the 1h and 24h burn rates (conservative).
    """
    balance = get_credit_balance(home)
    r1 = burn_rate(home, 1.0)
    r24 = burn_rate(home, 24.0)
    rate = max(r1["cost_per_hour"], r24["cost_per_hour"])
    spent = summary(home=home)["cost_usd"]
    out = {
        "credit_balance_usd": balance,
        "spent_usd": spent,
        "remaining_usd": round(balance - spent, 4) if balance is not None else None,
        "burn_1h_usd_per_h": r1["cost_per_hour"],
        "burn_24h_usd_per_h": r24["cost_per_hour"],
        "eta_hours": None,
    }
    if balance is not None and rate > 0:
        remaining = balance - spent
        out["eta_hours"] = round(remaining / rate, 1) if remaining > 0 else 0.0
    return out
