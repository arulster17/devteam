import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from orchestrator.budget import BudgetState
from orchestrator.main import _pass_loop
from orchestrator.work_plan import BudgetHaltedError

_EMPTY_PLAN = {"type": "work_plan", "actions": []}
_OK_STATE = BudgetState(total=100.0, spent=10.0, status="ok", calls=[])


@pytest.fixture(autouse=True)
def mock_infra(monkeypatch):
    """Patch all I/O and external calls so _pass_loop can run in-process."""
    monkeypatch.setattr("orchestrator.main.git_ops.current_commit_hash", lambda: "abc123")
    monkeypatch.setattr("orchestrator.main.git_ops.commit_all", lambda msg: "ok")
    monkeypatch.setattr("orchestrator.main.git_ops.tag", lambda name, msg: None)
    monkeypatch.setattr("orchestrator.main.log_event", lambda *a, **kw: None)
    monkeypatch.setattr("orchestrator.main._save_checkpoint", lambda wp, pn=1: None)
    monkeypatch.setattr("orchestrator.main._clear_checkpoint", lambda: None)
    monkeypatch.setattr("orchestrator.main._run_github_wiring", lambda *a, **kw: None)
    monkeypatch.setattr("orchestrator.main.budget_mod.load", lambda: _OK_STATE)


async def _fake_execute(work_plan, spawned_by="orchestrator"):
    return {"w1": "done"}


async def _fake_agent(**kwargs):
    if kwargs.get("role") == "release_summarizer":
        return "Release summary"
    return json.dumps(_EMPTY_PLAN)


# ---------------------------------------------------------------------------
# Pass count increments
# ---------------------------------------------------------------------------

def test_loop_increments_pass_count(monkeypatch):
    execute_count = 0

    async def counting_execute(work_plan, spawned_by="orchestrator"):
        nonlocal execute_count
        execute_count += 1
        return {}

    # First pass: continue (A); second pass: stop (C)
    responses = ["A", "C"]
    monkeypatch.setattr("orchestrator.main.work_plan_mod.execute", counting_execute)
    monkeypatch.setattr("orchestrator.main.agent_mod.call", AsyncMock(side_effect=_fake_agent))
    monkeypatch.setattr("orchestrator.main.checkpoint_mod.surface",
                        lambda action, completed: responses.pop(0))

    asyncio.run(_pass_loop(_EMPTY_PLAN, {}))

    assert execute_count == 2


def test_pass_count_reflected_in_checkpoint_summary(monkeypatch):
    summaries = []

    def capturing_surface(action, completed):
        summaries.append(action.get("summary", ""))
        return "C"

    monkeypatch.setattr("orchestrator.main.work_plan_mod.execute",
                        AsyncMock(return_value={"w1": "done"}))
    monkeypatch.setattr("orchestrator.main.agent_mod.call", AsyncMock(side_effect=_fake_agent))
    monkeypatch.setattr("orchestrator.main.checkpoint_mod.surface", capturing_surface)

    asyncio.run(_pass_loop(_EMPTY_PLAN, {}))

    assert len(summaries) == 1
    assert "Pass 1" in summaries[0]


# ---------------------------------------------------------------------------
# C response — release summarizer
# ---------------------------------------------------------------------------

def test_c_response_triggers_release_summarizer(monkeypatch):
    roles_called = []

    async def tracking_agent(**kwargs):
        roles_called.append(kwargs.get("role"))
        if kwargs.get("role") == "release_summarizer":
            return "Release summary"
        return json.dumps(_EMPTY_PLAN)

    monkeypatch.setattr("orchestrator.main.work_plan_mod.execute",
                        AsyncMock(return_value={"w1": "done"}))
    monkeypatch.setattr("orchestrator.main.agent_mod.call",
                        AsyncMock(side_effect=tracking_agent))
    monkeypatch.setattr("orchestrator.main.checkpoint_mod.surface",
                        lambda action, completed: "C")

    asyncio.run(_pass_loop(_EMPTY_PLAN, {}))

    assert "release_summarizer" in roles_called


def test_c_response_clears_checkpoint(monkeypatch):
    cleared = []

    monkeypatch.setattr("orchestrator.main._clear_checkpoint", lambda: cleared.append(True))
    monkeypatch.setattr("orchestrator.main.work_plan_mod.execute",
                        AsyncMock(return_value={}))
    monkeypatch.setattr("orchestrator.main.agent_mod.call", AsyncMock(side_effect=_fake_agent))
    monkeypatch.setattr("orchestrator.main.checkpoint_mod.surface",
                        lambda action, completed: "C")

    asyncio.run(_pass_loop(_EMPTY_PLAN, {}))

    assert len(cleared) == 1


# ---------------------------------------------------------------------------
# BudgetHaltedError — saves checkpoint and exits
# ---------------------------------------------------------------------------

def test_budget_halted_saves_checkpoint_and_exits(monkeypatch):
    saved = []

    async def halting_execute(work_plan, spawned_by="orchestrator"):
        raise BudgetHaltedError()

    monkeypatch.setattr("orchestrator.main.work_plan_mod.execute", halting_execute)
    monkeypatch.setattr("orchestrator.main._save_checkpoint", lambda wp, pn=1: saved.append(wp))

    asyncio.run(_pass_loop(_EMPTY_PLAN, {}))

    assert len(saved) >= 1


def test_budget_halted_does_not_call_checkpoint(monkeypatch):
    surface_called = []

    async def halting_execute(work_plan, spawned_by="orchestrator"):
        raise BudgetHaltedError()

    monkeypatch.setattr("orchestrator.main.work_plan_mod.execute", halting_execute)
    monkeypatch.setattr("orchestrator.main.checkpoint_mod.surface",
                        lambda *a, **kw: surface_called.append(True) or "A")

    asyncio.run(_pass_loop(_EMPTY_PLAN, {}))

    assert surface_called == []


# ---------------------------------------------------------------------------
# Pass cap
# ---------------------------------------------------------------------------

def test_pass_cap_respected(monkeypatch):
    execute_count = 0

    async def counting_execute(work_plan, spawned_by="orchestrator"):
        nonlocal execute_count
        execute_count += 1
        return {}

    monkeypatch.setattr("orchestrator.main.work_plan_mod.execute", counting_execute)
    monkeypatch.setattr("orchestrator.main.agent_mod.call", AsyncMock(side_effect=_fake_agent))
    monkeypatch.setattr("orchestrator.main.checkpoint_mod.surface",
                        lambda action, completed: "A")

    config = {"limits": {"max_total_passes": 3}}
    asyncio.run(_pass_loop(_EMPTY_PLAN, config))

    assert execute_count == 3


def test_pass_cap_default_is_ten(monkeypatch):
    execute_count = 0

    async def counting_execute(work_plan, spawned_by="orchestrator"):
        nonlocal execute_count
        execute_count += 1
        return {}

    monkeypatch.setattr("orchestrator.main.work_plan_mod.execute", counting_execute)
    monkeypatch.setattr("orchestrator.main.agent_mod.call", AsyncMock(side_effect=_fake_agent))
    monkeypatch.setattr("orchestrator.main.checkpoint_mod.surface",
                        lambda action, completed: "A")

    asyncio.run(_pass_loop(_EMPTY_PLAN, {}))  # no config → default 10

    assert execute_count == 10


# ---------------------------------------------------------------------------
# A response — top_level_agent called for next plan
# ---------------------------------------------------------------------------

def test_a_response_calls_top_level_agent(monkeypatch):
    tla_calls = []

    async def tracking_agent(**kwargs):
        if kwargs.get("role") == "top_level_agent":
            tla_calls.append(kwargs.get("instance_id"))
        if kwargs.get("role") == "release_summarizer":
            return "done"
        return json.dumps(_EMPTY_PLAN)

    responses = ["A", "C"]
    monkeypatch.setattr("orchestrator.main.work_plan_mod.execute",
                        AsyncMock(return_value={}))
    monkeypatch.setattr("orchestrator.main.agent_mod.call",
                        AsyncMock(side_effect=tracking_agent))
    monkeypatch.setattr("orchestrator.main.checkpoint_mod.surface",
                        lambda action, completed: responses.pop(0))

    asyncio.run(_pass_loop(_EMPTY_PLAN, {}))

    assert len(tla_calls) == 1
    assert tla_calls[0] == "top_level_agent_2"


# ---------------------------------------------------------------------------
# git operations called per pass
# ---------------------------------------------------------------------------

def test_git_commit_and_tag_called_each_pass(monkeypatch):
    commits = []
    tags = []

    monkeypatch.setattr("orchestrator.main.git_ops.commit_all", lambda msg: commits.append(msg))
    monkeypatch.setattr("orchestrator.main.git_ops.tag",
                        lambda name, msg: tags.append(name))

    responses = ["A", "C"]
    monkeypatch.setattr("orchestrator.main.work_plan_mod.execute",
                        AsyncMock(return_value={}))
    monkeypatch.setattr("orchestrator.main.agent_mod.call", AsyncMock(side_effect=_fake_agent))
    monkeypatch.setattr("orchestrator.main.checkpoint_mod.surface",
                        lambda action, completed: responses.pop(0))

    asyncio.run(_pass_loop(_EMPTY_PLAN, {}))

    assert len(commits) == 2
    assert "pass-1" in tags
    assert "pass-2" in tags
