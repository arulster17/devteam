"""
Orchestrator entry point.

Bootstrap sequence:
  1. Load .env / config
  2. Write .gitignore (first run only)
  3. Set budget via budget_manager
  4. top_level_agent conversation loop → work plan → execute

Resume: if run/checkpoint.json exists, skip straight to executing its work plan.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from orchestrator import agent as agent_mod
from orchestrator import budget as budget_mod
from orchestrator import git_ops
from orchestrator import work_plan as work_plan_mod
from orchestrator.logger import log_event
from orchestrator.work_plan import BudgetHaltedError

_CHECKPOINT_PATH = Path("run/checkpoint.json")
_BRIEF_PATH = Path("decisions/brief.md")


def _load_config() -> dict:
    path = Path("config.json")
    return json.loads(path.read_text()) if path.exists() else {}


def _require_env(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        print(f"Error: {key} environment variable not set.", file=sys.stderr)
        sys.exit(1)
    return val


def _save_checkpoint(work_plan: dict) -> None:
    _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CHECKPOINT_PATH.write_text(json.dumps(work_plan, indent=2))


def _load_checkpoint() -> dict | None:
    if _CHECKPOINT_PATH.exists():
        return json.loads(_CHECKPOINT_PATH.read_text())
    return None


def _clear_checkpoint() -> None:
    if _CHECKPOINT_PATH.exists():
        _CHECKPOINT_PATH.unlink()


async def _run_budget_manager(idea: str, config: dict) -> float:
    """Ask budget_manager for a total budget; prompt human to confirm."""
    model = "claude-sonnet-4-6"
    context = {
        "inline": f"Project idea:\n{idea}\n\nConfig:\n{json.dumps(config, indent=2)}"
    }
    result_text = await agent_mod.call(
        role="budget_manager",
        instance_id="budget_manager_0",
        spawned_by="orchestrator",
        context=context,
        model=model,
    )

    try:
        result = json.loads(result_text)
    except json.JSONDecodeError:
        print(f"\nBudget manager response:\n{result_text}")
        total = float(input("Enter approved budget ($): ").strip())
        return total

    print(f"\n{'─' * 50}")
    print("BUDGET PROPOSAL")
    print(f"{'─' * 50}")
    print(result_text)
    print(f"\n{'─' * 50}")
    raw = input("Approve this budget? Enter amount or press Enter to accept: ").strip()
    total = float(raw) if raw else float(result.get("recommended_total_usd", 0))
    return total


async def _conversation_loop(config: dict) -> str:
    """
    Drive a conversation with top_level_agent until it returns a work plan.
    Returns the raw JSON work plan string.
    """
    model = "claude-opus-4-7"
    messages: list[dict] = []
    budget_state = budget_mod.load()

    print("\nDescribe your project (or type 'quit' to exit):")
    user_input = input("> ").strip()
    if user_input.lower() == "quit":
        sys.exit(0)

    inline = (
        f"Budget status: {budget_state.status} "
        f"(${budget_state.spent:.2f} of ${budget_state.total:.2f})\n\n"
        f"User: {user_input}"
    )
    context = {"inline": inline}

    while True:
        result_text = await agent_mod.call(
            role="top_level_agent",
            instance_id="top_level_agent_0",
            spawned_by="orchestrator",
            context=context,
            model=model,
        )

        # Check if the agent returned a work plan (JSON object with "actions" key)
        stripped = result_text.strip()
        if stripped.startswith("{"):
            try:
                candidate = json.loads(stripped)
                if "actions" in candidate:
                    log_event("work_plan_received", {"action_count": len(candidate["actions"])})
                    return stripped
            except json.JSONDecodeError:
                pass

        # Conversational response — print and ask for next input
        print(f"\nAssistant: {result_text}\n")
        user_input = input("> ").strip()
        if user_input.lower() == "quit":
            sys.exit(0)

        budget_state = budget_mod.load()
        inline = (
            f"Budget status: {budget_state.status} "
            f"(${budget_state.spent:.2f} of ${budget_state.total:.2f})\n\n"
            f"User: {user_input}"
        )
        context = {"inline": inline}


async def _main() -> None:
    load_dotenv()
    _require_env("ANTHROPIC_API_KEY")

    config = _load_config()

    # ── Resume from checkpoint if present ────────────────────────────────
    saved = _load_checkpoint()
    if saved:
        print(f"\nResuming from checkpoint: {_CHECKPOINT_PATH}")
        print(f"  {len(saved.get('actions', []))} actions in work plan")
        raw = input("Continue? [y/N] ").strip().lower()
        if raw != "y":
            _clear_checkpoint()
            print("Checkpoint cleared. Starting fresh.")
        else:
            work_plan = saved
            try:
                completed = await work_plan_mod.execute(work_plan)
                log_event("run_complete", {"completed_actions": len(completed)})
                _clear_checkpoint()
                print("\nRun complete.")
            except BudgetHaltedError:
                _save_checkpoint(work_plan)
                print("\nRun halted by budget. Checkpoint saved.")
            return

    # ── First-run setup ──────────────────────────────────────────────────
    if not Path(".gitignore").exists():
        git_ops.write_gitignore()

    Path("decisions").mkdir(exist_ok=True)
    Path("run").mkdir(exist_ok=True)

    # ── Budget setup ─────────────────────────────────────────────────────
    if budget_mod.load().total == 0:
        print("\nNo budget set. Let's configure one.")
        idea_preview = input("Briefly describe your project (for budget estimation): ").strip()
        total = await _run_budget_manager(idea_preview, config)
        budget_mod.set_total(total)
        print(f"Budget set to ${total:.2f}")
        log_event("budget_set", {"total": total})

    # ── Conversation → work plan ─────────────────────────────────────────
    work_plan_text = await _conversation_loop(config)

    try:
        work_plan = json.loads(work_plan_text)
    except json.JSONDecodeError as exc:
        print(f"Failed to parse work plan: {exc}", file=sys.stderr)
        sys.exit(1)

    _save_checkpoint(work_plan)

    # ── Execute ──────────────────────────────────────────────────────────
    try:
        completed = await work_plan_mod.execute(work_plan)
        log_event("run_complete", {"completed_actions": len(completed)})
        _clear_checkpoint()
        print(f"\nRun complete. {len(completed)} actions executed.")
    except BudgetHaltedError:
        print("\nRun halted by budget. Checkpoint saved — rerun to continue.")
    except KeyboardInterrupt:
        print("\nInterrupted. Checkpoint saved.")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
