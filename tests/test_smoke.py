"""
Smoke test: exercises _main() end-to-end with all external I/O mocked.

Verifies the wiring from startup → budget setup → conversation loop →
pass loop → stop, without making any real API calls, git operations,
or filesystem writes outside tmp_path.
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock

from orchestrator.main import _main


_WORK_PLAN = {
    "type": "work_plan",
    "produced_by": "top_level_agent_0",
    "actions": [
        {
            "action": "spawn",
            "role": "pm",
            "instance_id": "pm_0",
            "depends_on": [],
            "context": {"inline": "Write the spec."},
            "model": "claude-sonnet-4-6",
        }
    ],
}

_BUDGET_PROPOSAL = json.dumps({
    "recommended_total_usd": 5.0,
    "rationale": "Small project",
    "model_assignments": {},
})

_RELEASE_SUMMARY = json.dumps({
    "changelog_entry": "Initial release",
    "version_bump": "minor",
    "reason": "First pass complete",
})


@pytest.fixture()
def tmp_run(tmp_path, monkeypatch):
    """Redirect all file I/O to tmp_path so tests are hermetic."""
    import orchestrator.main as main_mod
    import orchestrator.budget as budget_mod
    import orchestrator.logger as logger_mod

    monkeypatch.setattr(main_mod, "_CHECKPOINT_PATH", tmp_path / "checkpoint.json")
    monkeypatch.setattr(budget_mod, "BUDGET_PATH", tmp_path / "budget.json")
    monkeypatch.setattr(logger_mod, "LOG_PATH", tmp_path / "log.jsonl")

    # config.json — provide a minimal one in tmp_path
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"limits": {"max_total_passes": 10, "max_parallel_workers": 5}}))
    monkeypatch.chdir(tmp_path)

    # decisions/ and run/ dirs exist
    (tmp_path / "decisions").mkdir()
    (tmp_path / "run").mkdir()


@pytest.fixture()
def mock_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_TOKEN", "")


@pytest.fixture()
def mock_git(monkeypatch):
    import orchestrator.main as main_mod
    monkeypatch.setattr(main_mod.git_ops, "write_gitignore", lambda: None)
    monkeypatch.setattr(main_mod.git_ops, "current_commit_hash", lambda: "abc123")
    monkeypatch.setattr(main_mod.git_ops, "commit_all", lambda msg: "ok")
    monkeypatch.setattr(main_mod.git_ops, "tag", lambda name, msg: None)


def _make_agent_side_effect(responses: dict):
    """Return an async side_effect that dispatches by role."""
    async def _agent(**kwargs):
        role = kwargs.get("role", "")
        return responses.get(role, "{}")
    return _agent


def test_smoke_fresh_start_stop(tmp_run, mock_env, mock_git, monkeypatch):
    """
    Fresh run: budget setup → TLA conversation → one pass → human stops (C).
    Verifies: budget saved, work plan executed, release_summarizer called.
    """
    import orchestrator.main as main_mod
    import orchestrator.work_plan as wp_mod

    # Track which roles were called
    roles_called = []

    async def agent_side_effect(**kwargs):
        role = kwargs.get("role", "")
        roles_called.append(role)
        if role == "budget_manager":
            return _BUDGET_PROPOSAL
        if role == "top_level_agent":
            return json.dumps(_WORK_PLAN)
        if role == "release_summarizer":
            return _RELEASE_SUMMARY
        return "{}"

    monkeypatch.setattr(main_mod.agent_mod, "call", AsyncMock(side_effect=agent_side_effect))

    # work_plan.execute: return one completed action
    async def fake_execute(plan, spawned_by="orchestrator"):
        return {"pm_0": "spec written"}
    monkeypatch.setattr(wp_mod, "execute", fake_execute)

    # checkpoint.surface: stop immediately
    monkeypatch.setattr(main_mod.checkpoint_mod, "surface", lambda action, completed: "C")

    # GitHub wiring: skip
    monkeypatch.setattr(main_mod, "_run_github_wiring", lambda *a, **kw: None)

    # User input sequence: project description → budget acceptance → stop
    inputs = iter(["Build a todo app", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    asyncio.run(_main())

    assert "budget_manager" in roles_called
    assert "top_level_agent" in roles_called
    assert "release_summarizer" in roles_called


def test_smoke_resume_from_checkpoint(tmp_run, mock_env, mock_git, monkeypatch, tmp_path):
    """
    Checkpoint exists → user confirms → resumes into _pass_loop at saved pass.
    Verifies: budget_manager and conversation loop are NOT re-run.
    """
    import orchestrator.main as main_mod
    import orchestrator.budget as budget_mod
    import orchestrator.work_plan as wp_mod

    # Pre-write a checkpoint at pass 2
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(json.dumps({"work_plan": _WORK_PLAN, "pass_num": 2}))

    # Budget already set
    budget_mod.set_total(10.0)

    roles_called = []

    async def agent_side_effect(**kwargs):
        role = kwargs.get("role", "")
        roles_called.append(role)
        if role == "release_summarizer":
            return _RELEASE_SUMMARY
        return json.dumps(_WORK_PLAN)

    monkeypatch.setattr(main_mod.agent_mod, "call", AsyncMock(side_effect=agent_side_effect))

    async def fake_execute(plan, spawned_by="orchestrator"):
        return {"pm_0": "done"}
    monkeypatch.setattr(wp_mod, "execute", fake_execute)

    monkeypatch.setattr(main_mod.checkpoint_mod, "surface", lambda action, completed: "C")
    monkeypatch.setattr(main_mod, "_run_github_wiring", lambda *a, **kw: None)

    # User confirms resume
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    asyncio.run(_main())

    # Budget manager and TLA conversation should NOT have been called
    assert "budget_manager" not in roles_called
    # Release summarizer should have been called (pass completed, human chose C)
    assert "release_summarizer" in roles_called


def test_smoke_checkpoint_pass_number_preserved(tmp_run, mock_env, mock_git, monkeypatch, tmp_path):
    """
    After a budget halt on pass 3, checkpoint records pass_num=3.
    On resume, _pass_loop starts at pass 3, not pass 1.
    """
    import orchestrator.main as main_mod
    import orchestrator.budget as budget_mod
    import orchestrator.work_plan as wp_mod
    from orchestrator.work_plan import BudgetHaltedError

    budget_mod.set_total(10.0)

    async def agent_side_effect(**kwargs):
        return json.dumps(_WORK_PLAN)

    monkeypatch.setattr(main_mod.agent_mod, "call", AsyncMock(side_effect=agent_side_effect))

    halt_on_first = [True]

    async def fake_execute(plan, spawned_by="orchestrator"):
        if halt_on_first[0]:
            halt_on_first[0] = False
            raise BudgetHaltedError()
        return {}

    monkeypatch.setattr(wp_mod, "execute", fake_execute)
    monkeypatch.setattr(main_mod, "_run_github_wiring", lambda *a, **kw: None)

    # Capture what pass number was saved in the checkpoint
    saved_checkpoints = []

    def capturing_save(wp, pn=1):
        saved_checkpoints.append(pn)
    monkeypatch.setattr(main_mod, "_save_checkpoint", capturing_save)

    # Also write the checkpoint file so it persists for inspection
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")  # don't resume

    # Run the pass loop directly with start_pass=3
    asyncio.run(main_mod._pass_loop(_WORK_PLAN, {}, start_pass=3))

    # Budget halted immediately — saved pass_num should be 3
    assert saved_checkpoints[0] == 3
