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
            for (aid, action), result in zip(agent_actions, results):
                completed[aid] = result
                _maybe_write_file(aid, result)
                _maybe_post_review(aid, action, result)
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


def _maybe_post_review(aid: str, action: dict, result_text: str) -> None:
    """
    Reviewers post to GitHub immediately on completion — they don't wait for the
    qa_manager aggregate. Silently skips if GITHUB_TOKEN is absent or the result
    isn't reviewer JSON.
    """
    if action.get("role") != "reviewer":
        return
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, ValueError):
        return
    verdict = data.get("verdict", "comment")
    issues = data.get("issues", [])
    body = "\n\n".join(
        f"**{i.get('severity', 'minor').upper()}** `{i.get('file', '')}` "
        f"(line {i.get('line', '?')}): {i.get('description', '')}"
        + (f"\n> Suggestion: {i['suggestion']}" if i.get("suggestion") else "")
        for i in issues
    ) or "No issues found."

    pr_number = _load_config().get("github", {}).get("current_pr_number")
    if not pr_number:
        log_event("reviewer_no_pr", {"aid": aid})
        return

    try:
        from orchestrator.github_client import GitHubClient
        gh = GitHubClient()
        gh.post_pr_review(
            pr_number=int(pr_number),
            verdict=verdict,
            body=body,
            reviewer_id=aid,
        )
    except Exception as exc:
        log_event("reviewer_post_error", {"aid": aid, "error": str(exc)})


def _maybe_write_file(aid: str, result_text: str) -> None:
    """
    No-tool agents (pm, architect, budget_manager, release_summarizer) return:
      {"file": "decisions/spec.md", "content": "..."}
    Write the file to disk so downstream agents can read it from documents[].
    Silently skips if the result isn't that shape.
    """
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(data, dict):
        return
    file_path = data.get("file")
    content = data.get("content")
    if not file_path or content is None:
        return
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    log_event("file_written", {"aid": aid, "path": file_path})


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
