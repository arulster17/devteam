import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.budget import BudgetState
from orchestrator.work_plan import BudgetHaltedError, execute

_STATE_OK = BudgetState(total=100.0, spent=10.0, status="ok", calls=[])
_STATE_HALTED = BudgetState(total=100.0, spent=110.0, status="halted", calls=[])

def _SPAWN(iid: str, deps: list) -> dict:
    return {
        "action": "spawn",
        "role": "dev_worker",
        "instance_id": iid,
        "depends_on": deps,
        "context": {},
        "model": "claude-haiku-4-5-20251001",
    }


@pytest.fixture(autouse=True)
def silence_log(monkeypatch):
    monkeypatch.setattr("orchestrator.work_plan.log_event", lambda *a, **kw: None)


@pytest.fixture
def ok_budget(monkeypatch):
    monkeypatch.setattr("orchestrator.work_plan.load_budget", lambda: _STATE_OK)


@pytest.fixture
def halted_budget(monkeypatch):
    monkeypatch.setattr("orchestrator.work_plan.load_budget", lambda: _STATE_HALTED)


# ---------------------------------------------------------------------------
# Dependency ordering
# ---------------------------------------------------------------------------

def test_dependency_ordering_sequential(ok_budget):
    call_order = []

    async def fake_call(**kwargs):
        call_order.append(kwargs["instance_id"])
        return "ok"

    plan = {"actions": [_SPAWN("w1", []), _SPAWN("w2", ["w1"])]}

    with patch("orchestrator.work_plan.agent_mod.call", side_effect=fake_call):
        result = asyncio.run(execute(plan))

    assert call_order.index("w1") < call_order.index("w2")
    assert result == {"w1": "ok", "w2": "ok"}


def test_three_level_chain_respects_order(ok_budget):
    call_order = []

    async def fake_call(**kwargs):
        call_order.append(kwargs["instance_id"])
        return "ok"

    plan = {
        "actions": [
            _SPAWN("w1", []),
            _SPAWN("w2", ["w1"]),
            _SPAWN("w3", ["w2"]),
        ]
    }

    with patch("orchestrator.work_plan.agent_mod.call", side_effect=fake_call):
        asyncio.run(execute(plan))

    assert call_order == ["w1", "w2", "w3"]


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------

def test_independent_actions_all_complete(ok_budget):
    with patch("orchestrator.work_plan.agent_mod.call", new_callable=AsyncMock) as m:
        m.return_value = "result"
        plan = {"actions": [_SPAWN("w1", []), _SPAWN("w2", []), _SPAWN("w3", [])]}
        result = asyncio.run(execute(plan))

    assert set(result.keys()) == {"w1", "w2", "w3"}
    assert m.call_count == 3


def test_parallel_and_sequential_mixed(ok_budget):
    call_order = []

    async def fake_call(**kwargs):
        call_order.append(kwargs["instance_id"])
        return "ok"

    # w1 and w2 are independent; w3 depends on both
    plan = {
        "actions": [
            _SPAWN("w1", []),
            _SPAWN("w2", []),
            _SPAWN("w3", ["w1", "w2"]),
        ]
    }

    with patch("orchestrator.work_plan.agent_mod.call", side_effect=fake_call):
        result = asyncio.run(execute(plan))

    assert set(result.keys()) == {"w1", "w2", "w3"}
    assert call_order.index("w3") > call_order.index("w1")
    assert call_order.index("w3") > call_order.index("w2")


# ---------------------------------------------------------------------------
# Checkpoint sequencing
# ---------------------------------------------------------------------------

def test_checkpoint_runs_after_depends_on(ok_budget):
    with patch("orchestrator.work_plan.agent_mod.call", new_callable=AsyncMock) as m:
        m.return_value = "agent result"
        with patch("orchestrator.work_plan.surface_checkpoint", return_value="A") as cp:
            plan = {
                "actions": [
                    _SPAWN("pm1", []),
                    {
                        "action": "checkpoint",
                        "name": "spec_approval",
                        "depends_on": ["pm1"],
                        "summary": "spec done",
                        "key_decisions": [],
                        "options": ["[A] Approve"],
                    },
                ]
            }
            result = asyncio.run(execute(plan))

    assert result["pm1"] == "agent result"
    assert result["spec_approval"] == "A"
    cp.assert_called_once()


def test_checkpoint_passes_completed_to_surface(ok_budget):
    captured = {}

    def fake_surface(action, completed):
        captured.update(completed)
        return "B"

    with patch("orchestrator.work_plan.agent_mod.call", new_callable=AsyncMock) as m:
        m.return_value = "worker done"
        with patch("orchestrator.work_plan.surface_checkpoint", side_effect=fake_surface):
            plan = {
                "actions": [
                    _SPAWN("w1", []),
                    {
                        "action": "checkpoint",
                        "name": "cp1",
                        "depends_on": ["w1"],
                        "summary": "",
                        "key_decisions": [],
                        "options": [],
                    },
                ]
            }
            asyncio.run(execute(plan))

    assert captured.get("w1") == "worker done"


# ---------------------------------------------------------------------------
# BudgetHaltedError
# ---------------------------------------------------------------------------

def test_budget_halted_raises(halted_budget):
    with patch("orchestrator.work_plan.agent_mod.call", new_callable=AsyncMock) as m:
        m.return_value = "ok"
        with pytest.raises(BudgetHaltedError):
            asyncio.run(execute({"actions": [_SPAWN("w1", [])]}))


def test_budget_warning_does_not_raise(monkeypatch, ok_budget):
    warn_state = BudgetState(total=100.0, spent=92.0, status="warning", calls=[])
    monkeypatch.setattr("orchestrator.work_plan.load_budget", lambda: warn_state)

    with patch("orchestrator.work_plan.agent_mod.call", new_callable=AsyncMock) as m:
        m.return_value = "ok"
        result = asyncio.run(execute({"actions": [_SPAWN("w1", [])]}))

    assert result["w1"] == "ok"


# ---------------------------------------------------------------------------
# Aggregate action — worker results forwarded
# ---------------------------------------------------------------------------

def test_aggregate_receives_worker_results(ok_budget):
    seen_worker_results = {}

    async def fake_call(**kwargs):
        if kwargs["instance_id"] == "agg1":
            seen_worker_results.update(kwargs.get("worker_results") or {})
            return "agg result"
        return "worker result"

    plan = {
        "actions": [
            _SPAWN("w1", []),
            {
                "action": "aggregate",
                "role": "dev_manager",
                "instance_id": "agg1",
                "depends_on": ["w1"],
                "context": {"worker_results": ["w1"]},
                "model": "claude-sonnet-4-6",
            },
        ]
    }

    with patch("orchestrator.work_plan.agent_mod.call", side_effect=fake_call):
        result = asyncio.run(execute(plan))

    assert seen_worker_results == {"w1": "worker result"}
    assert result["agg1"] == "agg result"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_plan_returns_empty(ok_budget):
    result = asyncio.run(execute({"actions": []}))
    assert result == {}


def test_unsatisfiable_deps_returns_partial(ok_budget):
    # w2 depends on "ghost" which doesn't exist — w2 never becomes ready
    with patch("orchestrator.work_plan.agent_mod.call", new_callable=AsyncMock) as m:
        m.return_value = "ok"
        plan = {"actions": [_SPAWN("w1", []), _SPAWN("w2", ["ghost"])]}
        result = asyncio.run(execute(plan))

    assert "w1" in result
    assert "w2" not in result


# ---------------------------------------------------------------------------
# _maybe_write_file — no-tool agent file output
# ---------------------------------------------------------------------------

def test_file_output_written_to_disk(ok_budget, tmp_path, monkeypatch):
    """pm/architect return {"file": "...", "content": "..."} — orchestrator writes it."""
    monkeypatch.chdir(tmp_path)

    file_result = '{"file": "decisions/spec.md", "content": "# Spec\\nHello"}'

    with patch("orchestrator.work_plan.agent_mod.call", new_callable=AsyncMock) as m:
        m.return_value = file_result
        asyncio.run(execute({"actions": [_SPAWN("pm_0", [])]}))

    written = (tmp_path / "decisions" / "spec.md").read_text()
    assert written == "# Spec\nHello"


def test_file_output_creates_parent_dirs(ok_budget, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nested = '{"file": "decisions/sub/arch.md", "content": "deep"}'

    with patch("orchestrator.work_plan.agent_mod.call", new_callable=AsyncMock) as m:
        m.return_value = nested
        asyncio.run(execute({"actions": [_SPAWN("arch_0", [])]}))

    assert (tmp_path / "decisions" / "sub" / "arch.md").read_text() == "deep"


def test_non_file_result_not_written(ok_budget, tmp_path, monkeypatch):
    """Plain text or status JSON doesn't trigger file writes."""
    monkeypatch.chdir(tmp_path)

    with patch("orchestrator.work_plan.agent_mod.call", new_callable=AsyncMock) as m:
        m.return_value = '{"status": "complete", "files_written": ["src/auth.ts"]}'
        asyncio.run(execute({"actions": [_SPAWN("dev_0", [])]}))

    # No file should have been created (decisions/ doesn't exist)
    assert not (tmp_path / "decisions").exists()


def test_plain_text_result_not_written(ok_budget, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with patch("orchestrator.work_plan.agent_mod.call", new_callable=AsyncMock) as m:
        m.return_value = "plain text response"
        asyncio.run(execute({"actions": [_SPAWN("w1", [])]}))

    assert list(tmp_path.iterdir()) == []
