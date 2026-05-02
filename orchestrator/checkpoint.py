from orchestrator.budget import load as load_budget


def surface(action: dict, completed: dict) -> str:
    """Format and print a checkpoint, wait for human response, return it."""
    budget = load_budget()
    if budget.total > 0:
        cost_line = f"  ${budget.spent:.2f} spent of ${budget.total:.2f} budget"
    else:
        cost_line = "  budget not yet set"

    name = action["name"].upper().replace("_", " ")
    summary = action.get("summary", "")
    key_decisions = action.get("key_decisions", [])
    options = action.get("options", ["[A] Continue", "[B] Stop"])

    kd_lines = "\n".join(f"  - {kd}" for kd in key_decisions)
    opt_lines = "\n".join(f"  {opt}" for opt in options)

    print(f"""
CHECKPOINT: {name}
{'─' * 41}
SUMMARY
  {summary}

KEY DECISIONS / FINDINGS
{kd_lines}

COST SO FAR
{cost_line}

YOUR OPTIONS
{opt_lines}

Waiting for your response.""")

    return input("> ").strip()
