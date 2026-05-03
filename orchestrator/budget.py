import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

BUDGET_PATH = Path("run/budget.json")

# Prices per 1M tokens. Keep in sync with current Anthropic pricing.
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {
        "input": 15.0, "output": 75.0,
        "cache_write": 18.75, "cache_read": 1.50,
    },
    "claude-sonnet-4-6": {
        "input": 3.0, "output": 15.0,
        "cache_write": 3.75, "cache_read": 0.30,
    },
    "claude-haiku-4-5-20251001": {
        "input": 0.80, "output": 4.0,
        "cache_write": 1.0, "cache_read": 0.08,
    },
}

# Fallback for unknown model IDs
_DEFAULT_PRICING = PRICING["claude-sonnet-4-6"]


@dataclass
class BudgetState:
    total: float = 0.0
    spent: float = 0.0
    status: str = "ok"  # "ok" | "warning" | "halted"
    calls: list = field(default_factory=list)


def load() -> BudgetState:
    if not BUDGET_PATH.exists():
        return BudgetState()
    data = json.loads(BUDGET_PATH.read_text())
    return BudgetState(**data)


def save(state: BudgetState) -> None:
    BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUDGET_PATH.write_text(json.dumps(asdict(state), indent=2))


def set_total(total: float) -> None:
    state = load()
    state.total = total
    state.status = "ok"
    save(state)


def record_call(
    agent_role: str,
    agent_instance: str,
    spawned_by: str,
    tier: int,
    model: str,
    usage,
) -> tuple[float, str]:
    """
    Record a completed API call. Returns (cost, new_status).
    `usage` is the Anthropic SDK usage object from the response.
    """
    prices = PRICING.get(model, _DEFAULT_PRICING)

    input_tokens = getattr(usage, "input_tokens", 0)
    output_tokens = getattr(usage, "output_tokens", 0)
    cache_write_tokens = getattr(usage, "cache_creation_input_tokens", 0)
    cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0)

    cost = (
        (input_tokens / 1_000_000) * prices["input"]
        + (output_tokens / 1_000_000) * prices["output"]
        + (cache_write_tokens / 1_000_000) * prices["cache_write"]
        + (cache_read_tokens / 1_000_000) * prices["cache_read"]
    )

    state = load()
    state.spent = round(state.spent + cost, 6)
    state.calls.append({
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "agent_role": agent_role,
        "agent_instance": agent_instance,
        "spawned_by": spawned_by,
        "tier": tier,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_write_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cost": round(cost, 6),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    if state.total > 0:
        pct = state.spent / state.total
        if pct >= 1.0:
            state.status = "halted"
        elif pct >= 0.90:
            state.status = "warning"
        else:
            state.status = "ok"

    save(state)
    return cost, state.status
