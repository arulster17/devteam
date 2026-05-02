"""
Work plan executor.

Resolves depends_on ordering, runs independent spawn/aggregate actions in
parallel (up to max_parallel_workers), and executes checkpoint actions
sequentially as synchronous barriers.
"""
import asyncio
import json
from pathlib import Path

from orchestrator import agent as agent_mod
from orchestrator.budget import load as load_budget
from orchestrator.checkpoint import surface as surface_checkpoint
from orchestrator.logger import log_event


class BudgetHaltedError(Exception):
    pass


def _load_config() -> dict:
    path = Path("config.json")
    return json.loads(path.read_text()) if path.exists() else {}


def _action_id(action: dict) -> str:
    return action.get("instance_id") or action.get("name", "")


async def execute(work_plan: dict, spawned_by: str = "orchestrator") -> dict[str, str]:
    """
    Execute a work plan. Returns completed: {action_id -> result_text}.

    Execution model:
      Each iteration finds all actions whose depends_on are satisfied.
      Agent actions (spawn/aggregate) run in parallel under the semaphore.
      Checkpoint actions run sequentially after the parallel batch settles.
    """
    config = _load_config()
    max_workers = config.get("limits", {}).get("max_parallel_workers", 5)
    semaphore = asyncio.Semaphore(max_workers)

    actions = work_plan.get("actions", [])
    indexed: dict[str, dict] = {_action_id(a): a for a in actions}
    completed: dict[str, str] = {}

    while len(completed) < len(indexed):
        ready = [
            (aid, action)
            for aid, action in indexed.items()
            if aid not in completed
            and all(dep in completed for dep in action.get("depends_on", []))
        ]

        if not ready:
            break

        agent_actions = [(aid, a) for aid, a in ready if a["action"] in ("spawn", "aggregate")]
        checkpoints = [(aid, a) for aid, a in ready if a["action"] == "checkpoint"]

        if agent_actions:
            results = await asyncio.gather(*[
                _run_agent_action(aid, action, completed, spawned_by, semaphore)
                for aid, action in agent_actions
            ])
            for (aid, _), result in zip(agent_actions, results):
                completed[aid] = result
                _check_budget()

        for aid, action in checkpoints:
            log_event("checkpoint_start", {"name": action.get("name", aid)})
            human_response = surface_checkpoint(action, completed)
            completed[aid] = human_response
            log_event("checkpoint_response", {
                "name": action.get("name", aid),
                "response": human_response,
            })

    return completed


async def _run_agent_action(
    aid: str,
    action: dict,
    completed: dict[str, str],
    spawned_by: str,
    semaphore: asyncio.Semaphore,
) -> str:
    async with semaphore:
        role = action["role"]
        instance_id = action.get("instance_id", aid)
        context = action.get("context", {})
        model = action["model"]

        worker_results: dict[str, str] | None = None
        if action["action"] == "aggregate":
            result_ids = context.get("worker_results", [])
            worker_results = {rid: completed[rid] for rid in result_ids if rid in completed}

        log_event("agent_start", {"instance_id": instance_id, "role": role, "model": model})

        result = await agent_mod.call(
            role=role,
            instance_id=instance_id,
            spawned_by=spawned_by,
            context=context,
            model=model,
            worker_results=worker_results,
        )
        return result


def _check_budget() -> None:
    state = load_budget()
    if state.status == "warning":
        print(
            f"\nBUDGET WARNING: ${state.spent:.2f} of ${state.total:.2f} spent "
            f"({state.spent / state.total * 100:.0f}%). "
            "Agents are in conservative mode."
        )
    elif state.status == "halted":
        print(f"\nBUDGET HALTED: ${state.spent:.2f} of ${state.total:.2f} spent.")
        raise BudgetHaltedError()
