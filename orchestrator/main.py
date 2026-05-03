"""
Orchestrator entry point.

Bootstrap sequence:
  1. Load .env / config
  2. Write .gitignore (first run only)
  3. Set budget via budget_manager
  4. top_level_agent conversation loop → work plan → pass loop

Resume: if run/checkpoint.json exists, skip straight to executing its work plan.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from orchestrator import agent as agent_mod
from orchestrator import budget as budget_mod
from orchestrator import checkpoint as checkpoint_mod
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


def _save_checkpoint(work_plan: dict, pass_num: int = 1) -> None:
    _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CHECKPOINT_PATH.write_text(json.dumps({"work_plan": work_plan, "pass_num": pass_num}, indent=2))


def _load_checkpoint() -> tuple[dict, int] | None:
    if not _CHECKPOINT_PATH.exists():
        return None
    data = json.loads(_CHECKPOINT_PATH.read_text())
    # Support old format (bare work plan dict) written before this change
    if "work_plan" in data:
        return data["work_plan"], data.get("pass_num", 1)
    return data, 1


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


# ── Text helpers for GitHub wiring ──────────────────────────────────────────

def _extract_user_stories(text: str) -> list[str]:
    """Parse ## User Stories section from a markdown spec. One item per bullet/heading."""
    in_section = False
    stories: list[str] = []
    for line in text.splitlines():
        if line.startswith("## User Stories"):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            stripped = line.strip()
            if stripped.startswith(("- ", "* ")):
                stories.append(stripped[2:].strip())
            elif stripped.startswith("### "):
                stories.append(stripped[4:].strip())
    return stories


def _extract_modules(text: str) -> list[str]:
    """
    Parse module names from a markdown architecture doc.
    Handles both '## Module Boundaries' (architect.md format) with '### name'
    subsections, and a plain '## Modules' list with '- name' bullets.
    """
    in_section = False
    modules: list[str] = []
    for line in text.splitlines():
        if line.startswith("## Module Boundaries") or line.startswith("## Modules"):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            stripped = line.strip()
            if stripped.startswith("### "):
                modules.append(stripped[4:].strip())
            elif stripped.startswith(("- ", "* ")):
                modules.append(stripped[2:].strip())
    return modules


# ── GitHub wiring ────────────────────────────────────────────────────────────

def _push_worker_result(gh: object, aid: str, result_text: str) -> None:
    """Attempt to push a dev_worker's committed files to its module branch."""
    try:
        result = json.loads(result_text)
    except (json.JSONDecodeError, ValueError):
        return
    if not result.get("files_written"):
        return
    # Extract module from instance_id using the documented convention:
    # dev_worker_<module>_<n>  →  module/<module>
    # e.g. dev_worker_auth_1  →  module/auth
    m = re.match(r"^dev_worker_(.+)_\d+$", aid)
    module = m.group(1) if m else "main"
    branch = f"module/{module}"
    commit_msg = result.get("commit_message", f"feat: {aid}")
    try:
        if not gh.branch_exists(branch):  # type: ignore[attr-defined]
            gh.create_branch(branch)  # type: ignore[attr-defined]
        git_ops.checkout(branch)
        git_ops.commit_all(commit_msg)
        git_ops.push(branch)
    except Exception as exc:
        log_event("github_push_error", {"aid": aid, "error": str(exc)})


def _run_github_wiring(work_plan: dict, completed: dict, pass_num: int) -> None:
    """
    Post-pass GitHub operations. Entirely optional — if GITHUB_TOKEN is absent
    or the repo is unreachable, logs a warning and continues without failing.
    """
    try:
        from orchestrator.github_client import GitHubClient
        gh = GitHubClient()
    except Exception as exc:
        log_event("github_skipped", {"reason": str(exc)})
        return

    actions_by_id = {
        (a.get("instance_id") or a.get("name", "")): a
        for a in work_plan.get("actions", [])
    }
    config = _load_config()

    # spec_approval → milestone + one issue per user story
    try:
        if "spec_approval" in completed:
            spec_path = Path("decisions/spec.md")
            if spec_path.exists():
                stories = _extract_user_stories(spec_path.read_text())
                if stories:
                    milestone_num = gh.create_milestone(f"Pass {pass_num}", "")
                    for story in stories:
                        gh.create_issue(title=story, body="", milestone_number=milestone_num)
    except Exception as exc:
        log_event("github_wiring_error", {"step": "spec_approval", "error": str(exc)})

    # architecture_approval → one branch per module
    try:
        if "architecture_approval" in completed:
            arch_path = Path("decisions/architecture.md")
            if arch_path.exists():
                for module in _extract_modules(arch_path.read_text()):
                    branch = f"module/{module.lower()}"
                    if not gh.branch_exists(branch):
                        gh.create_branch(branch)
    except Exception as exc:
        log_event("github_wiring_error", {"step": "architecture_approval", "error": str(exc)})

    # dev_worker completions → push to module branch
    try:
        for aid, action in actions_by_id.items():
            if action.get("role") == "dev_worker" and aid in completed:
                _push_worker_result(gh, aid, completed[aid])
    except Exception as exc:
        log_event("github_wiring_error", {"step": "dev_worker", "error": str(exc)})

    # integrator → open integration → main PR
    try:
        integration_branch = config.get("github", {}).get("integration_branch", "integration")
        main_branch = config.get("github", {}).get("main_branch", "main")
        for aid, action in actions_by_id.items():
            if action.get("role") == "integrator" and aid in completed:
                if gh.branch_exists(integration_branch):
                    pr_num = gh.create_pr(
                        title=f"Integration pass {pass_num}",
                        body=f"Automated integration PR for pass {pass_num}.",
                        head=integration_branch,
                        base=main_branch,
                    )
                    log_event("github_integration_pr", {"pr": pr_num, "pass": pass_num})
                break
    except Exception as exc:
        log_event("github_wiring_error", {"step": "integrator", "error": str(exc)})


# ── Pass loop ────────────────────────────────────────────────────────────────

async def _pass_loop(work_plan: dict, config: dict, start_pass: int = 1) -> None:
    """
    Multi-pass execution loop. Each pass: execute work plan → commit/tag →
    surface pass summary checkpoint → human decides continue/redirect/stop.
    """
    max_passes = config.get("limits", {}).get("max_total_passes", 10)

    for pass_num in range(start_pass, max_passes + 1):
        commit_hash = git_ops.current_commit_hash()
        log_event("pass_start", {"pass": pass_num, "commit": commit_hash})
        _save_checkpoint(work_plan, pass_num)

        try:
            completed = await work_plan_mod.execute(work_plan)
        except BudgetHaltedError:
            _save_checkpoint(work_plan, pass_num)
            print(f"\nBudget halted on pass {pass_num}. Checkpoint saved.")
            log_event("budget_halted", {"pass": pass_num})
            return
        except KeyboardInterrupt:
            _save_checkpoint(work_plan, pass_num)
            print("\nInterrupted. Checkpoint saved.")
            return

        log_event("pass_complete", {"pass": pass_num, "completed": len(completed)})

        git_ops.commit_all(f"pass {pass_num}: {len(completed)} actions")
        git_ops.tag(f"pass-{pass_num}", f"pass {pass_num}")

        _run_github_wiring(work_plan, completed, pass_num)

        summary_action = {
            "action": "checkpoint",
            "name": "pass_summary",
            "summary": f"Pass {pass_num} complete. {len(completed)} actions executed.",
            "key_decisions": list(completed.keys()),
            "options": ["[A] Continue", "[B] Redirect", "[C] Stop"],
        }
        human_response = checkpoint_mod.surface(summary_action, completed)
        log_event("pass_summary_response", {"pass": pass_num, "response": human_response})

        choice = human_response.strip().upper()

        if choice.startswith("C"):
            summary = await agent_mod.call(
                role="release_summarizer",
                instance_id="release_summarizer_0",
                spawned_by="orchestrator",
                context={"inline": f"Completed actions: {', '.join(completed.keys())}"},
                model="claude-haiku-4-5-20251001",
            )
            print(f"\n{summary}")
            _clear_checkpoint()
            log_event("run_stopped", {"pass": pass_num})
            return

        elif choice.startswith("B"):
            print("\nWhat would you like to redirect to?")
            work_plan_text = await _conversation_loop(config)
            work_plan = json.loads(work_plan_text)

        else:  # A — continue
            inline = (
                f"Pass {pass_num} complete. "
                f"Completed action IDs: {', '.join(completed.keys())}. "
                "Continue building — produce the next work plan."
            )
            result_text = await agent_mod.call(
                role="top_level_agent",
                instance_id=f"top_level_agent_{pass_num + 1}",
                spawned_by="orchestrator",
                context={"inline": inline},
                model="claude-opus-4-7",
            )
            try:
                work_plan = json.loads(result_text.strip())
            except json.JSONDecodeError:
                print(f"\nExpected work plan from top_level_agent but got:\n{result_text}")
                work_plan_text = await _conversation_loop(config)
                work_plan = json.loads(work_plan_text)

        _save_checkpoint(work_plan, pass_num + 1)

    print(f"\nReached maximum of {max_passes} passes.")
    log_event("max_passes_reached", {"max_passes": max_passes})


# ── Entry point ──────────────────────────────────────────────────────────────

async def _main() -> None:
    load_dotenv()
    _require_env("ANTHROPIC_API_KEY")

    config = _load_config()

    # ── Resume from checkpoint if present ────────────────────────────────
    repo = config.get("github", {}).get("repo", "")
    if repo:
        git_ops.configure_remote_auth(repo)

    saved = _load_checkpoint()
    if saved:
        saved_plan, saved_pass = saved
        print(f"\nResuming from checkpoint: {_CHECKPOINT_PATH}")
        print(f"  Pass {saved_pass}, {len(saved_plan.get('actions', []))} actions in work plan")
        raw = input("Continue? [y/N] ").strip().lower()
        if raw != "y":
            _clear_checkpoint()
            print("Checkpoint cleared. Starting fresh.")
        else:
            await _pass_loop(saved_plan, config, start_pass=saved_pass)
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

    # ── Conversation → first work plan ───────────────────────────────────
    work_plan_text = await _conversation_loop(config)

    try:
        work_plan = json.loads(work_plan_text)
    except json.JSONDecodeError as exc:
        print(f"Failed to parse work plan: {exc}", file=sys.stderr)
        sys.exit(1)

    _save_checkpoint(work_plan)

    # ── Pass loop ─────────────────────────────────────────────────────────
    await _pass_loop(work_plan, config, start_pass=1)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
