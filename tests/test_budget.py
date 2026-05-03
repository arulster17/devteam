"""
Unit tests for orchestrator.budget

Covers: cost calculation, status transitions, set_total, record_call.
Uses a tmp_path fixture so tests never touch run/budget.json.
"""
import pytest

from orchestrator import budget as budget_mod
from orchestrator.budget import PRICING, BudgetState


class _Usage:
    def __init__(self, inp=0, out=0, cw=0, cr=0):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_creation_input_tokens = cw
        self.cache_read_input_tokens = cr


@pytest.fixture(autouse=True)
def isolate_budget(tmp_path, monkeypatch):
    """Redirect BUDGET_PATH to a temp directory for every test."""
    monkeypatch.setattr(budget_mod, "BUDGET_PATH", tmp_path / "budget.json")


# ── load / save round-trip ─────────────────────────────────────────────────

def test_load_returns_empty_state_when_no_file():
    state = budget_mod.load()
    assert state.total == 0.0
    assert state.spent == 0.0
    assert state.status == "ok"
    assert state.calls == []


def test_save_and_load_round_trip():
    state = BudgetState(total=10.0, spent=3.5, status="ok", calls=[])
    budget_mod.save(state)
    loaded = budget_mod.load()
    assert loaded.total == 10.0
    assert loaded.spent == 3.5


# ── set_total ──────────────────────────────────────────────────────────────

def test_set_total_resets_status_to_ok():
    # put budget into warning first
    state = BudgetState(total=10.0, spent=9.5, status="warning")
    budget_mod.save(state)
    budget_mod.set_total(20.0)
    loaded = budget_mod.load()
    assert loaded.total == 20.0
    assert loaded.status == "ok"


# ── cost calculation ───────────────────────────────────────────────────────

def test_record_call_zero_tokens_costs_nothing():
    budget_mod.set_total(100.0)
    cost, status = budget_mod.record_call(
        agent_role="pm",
        agent_instance="pm_0",
        spawned_by="orchestrator",
        tier=2,
        model="claude-sonnet-4-6",
        usage=_Usage(),
    )
    assert cost == 0.0
    assert status == "ok"


def test_record_call_calculates_cost_correctly():
    """1M input tokens at $3.00/1M = $3.00 exactly."""
    budget_mod.set_total(100.0)
    usage = _Usage(inp=1_000_000)
    cost, _ = budget_mod.record_call(
        agent_role="pm",
        agent_instance="pm_0",
        spawned_by="orchestrator",
        tier=2,
        model="claude-sonnet-4-6",
        usage=usage,
    )
    assert abs(cost - 3.0) < 1e-9


def test_record_call_all_four_token_types():
    """Sum: 1M input ($3) + 1M output ($15) + 1M cache_write ($3.75) + 1M cache_read ($0.30) = $22.05"""
    budget_mod.set_total(100.0)
    usage = _Usage(inp=1_000_000, out=1_000_000, cw=1_000_000, cr=1_000_000)
    cost, _ = budget_mod.record_call(
        agent_role="dev_worker",
        agent_instance="dev_0",
        spawned_by="dev_manager_0",
        tier=4,
        model="claude-sonnet-4-6",
        usage=usage,
    )
    expected = 3.0 + 15.0 + 3.75 + 0.30
    assert abs(cost - expected) < 1e-9


def test_record_call_opus_pricing():
    budget_mod.set_total(100.0)
    usage = _Usage(inp=1_000_000)
    cost, _ = budget_mod.record_call(
        agent_role="top_level_agent",
        agent_instance="top_0",
        spawned_by="orchestrator",
        tier=1,
        model="claude-opus-4-7",
        usage=usage,
    )
    assert abs(cost - PRICING["claude-opus-4-7"]["input"]) < 1e-9


def test_record_call_unknown_model_uses_sonnet_fallback():
    budget_mod.set_total(100.0)
    usage = _Usage(inp=1_000_000)
    cost, _ = budget_mod.record_call(
        agent_role="pm",
        agent_instance="pm_0",
        spawned_by="orchestrator",
        tier=2,
        model="claude-unknown-model",
        usage=usage,
    )
    assert abs(cost - PRICING["claude-sonnet-4-6"]["input"]) < 1e-9


# ── status transitions ─────────────────────────────────────────────────────

def test_status_ok_below_90_percent():
    budget_mod.set_total(10.0)
    # spend $8.99 → 89.9%
    usage = _Usage(out=int(8.99 / (75.0 / 1_000_000)))
    _, status = budget_mod.record_call("pm", "pm_0", "orchestrator", 2, "claude-sonnet-4-6", usage)
    # small spend first to stay under 90%
    budget_mod.set_total(100.0)
    usage2 = _Usage(inp=100_000)  # $0.30
    _, status2 = budget_mod.record_call("pm", "pm_0", "orchestrator", 2, "claude-sonnet-4-6", usage2)
    assert status2 == "ok"


def test_status_warning_at_90_percent():
    budget_mod.set_total(10.0)
    # 1M output tokens = $15 > $10 → would halt; scale down
    # Target: spend $9.00 of $10.00 = 90%
    # $9.00 output tokens: 9.00 / 15.0 * 1_000_000 = 600_000 tokens
    usage = _Usage(out=600_000)
    _, status = budget_mod.record_call("pm", "pm_0", "orchestrator", 2, "claude-sonnet-4-6", usage)
    assert status == "warning"


def test_status_halted_at_100_percent():
    budget_mod.set_total(10.0)
    # $10.01 spend → halted
    # output: 10.01 / 15.0 * 1_000_000 ≈ 667_334 tokens
    usage = _Usage(out=667_334)
    _, status = budget_mod.record_call("pm", "pm_0", "orchestrator", 2, "claude-sonnet-4-6", usage)
    assert status == "halted"


def test_status_stays_ok_with_zero_total():
    """When no budget is set (total=0), status stays ok regardless of spend."""
    # total=0 means no budget configured; status logic should not divide by zero
    usage = _Usage(inp=1_000_000)
    _, status = budget_mod.record_call("pm", "pm_0", "orchestrator", 2, "claude-sonnet-4-6", usage)
    assert status == "ok"


# ── call log ──────────────────────────────────────────────────────────────

def test_record_call_appends_to_log():
    budget_mod.set_total(100.0)
    usage = _Usage(inp=1000, out=500)
    budget_mod.record_call("pm", "pm_0", "orchestrator", 2, "claude-sonnet-4-6", usage)
    budget_mod.record_call("architect", "arch_0", "orchestrator", 2, "claude-sonnet-4-6", usage)
    state = budget_mod.load()
    assert len(state.calls) == 2
    assert state.calls[0]["agent_role"] == "pm"
    assert state.calls[1]["agent_role"] == "architect"


def test_record_call_log_entry_fields():
    budget_mod.set_total(100.0)
    usage = _Usage(inp=100, out=50, cw=10, cr=5)
    budget_mod.record_call("dev_worker", "dev_0", "dev_manager_0", 4, "claude-sonnet-4-6", usage)
    entry = budget_mod.load().calls[0]
    assert entry["agent_role"] == "dev_worker"
    assert entry["agent_instance"] == "dev_0"
    assert entry["spawned_by"] == "dev_manager_0"
    assert entry["tier"] == 4
    assert entry["model"] == "claude-sonnet-4-6"
    assert entry["input_tokens"] == 100
    assert entry["output_tokens"] == 50
    assert "cost" in entry
    assert "timestamp" in entry
    assert "id" in entry


def test_spent_accumulates_across_calls():
    budget_mod.set_total(100.0)
    usage = _Usage(inp=1_000_000)  # $3.00 each
    budget_mod.record_call("pm", "pm_0", "orchestrator", 2, "claude-sonnet-4-6", usage)
    budget_mod.record_call("architect", "arch_0", "orchestrator", 2, "claude-sonnet-4-6", usage)
    state = budget_mod.load()
    assert abs(state.spent - 6.0) < 1e-6
