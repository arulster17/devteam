"""
Anthropic API caller with prompt caching and tool-use loop.

Two agent profiles:
  - No-tool agents (pm, architect, budget_manager, etc.): single call, parse JSON.
  - Write-tool agents (dev_worker, test_worker, debug_worker, integrator, docs_writer):
    provide write_file / read_file tools and loop until stop_reason == "end_turn".
"""
import anthropic

from orchestrator import budget as budget_mod
from orchestrator.context import assemble_api_call
from orchestrator.logger import log_event

# Roles that receive filesystem tools
WRITE_TOOL_ROLES: frozenset[str] = frozenset({
    "dev_worker",
    "test_worker",
    "debug_worker",
    "integrator",
    "docs_writer",
})

TIER_MAP: dict[str, int] = {
    "top_level_agent": 1, "budget_manager": 1,
    "pm": 2, "architect": 2,
    "dev_manager": 3, "qa_manager": 3, "debug_manager": 3,
    "dev_worker": 4, "test_worker": 4, "debug_worker": 4,
    "reviewer": 4, "integrator": 4,
    "release_summarizer": 5, "docs_writer": 5,
}

_WRITE_FILE_TOOL = {
    "name": "write_file",
    "description": "Write content to a file on disk.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to project root"},
            "content": {"type": "string", "description": "Full file content"},
        },
        "required": ["path", "content"],
    },
}

_READ_FILE_TOOL = {
    "name": "read_file",
    "description": "Read a file from disk.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to project root"},
        },
        "required": ["path"],
    },
}


def _execute_tool(name: str, inputs: dict) -> str:
    from pathlib import Path
    if name == "write_file":
        path = Path(inputs["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inputs["content"])
        return f"Written: {inputs['path']}"
    if name == "read_file":
        path = Path(inputs["path"])
        return path.read_text() if path.exists() else f"Error: {inputs['path']} not found"
    return f"Unknown tool: {name}"


async def call(
    role: str,
    instance_id: str,
    spawned_by: str,
    context: dict,
    model: str,
    worker_results: dict[str, str] | None = None,
    max_tool_iterations: int = 20,
) -> str:
    """
    Call an agent and return its text response.
    Handles tool-use loops for write-capable agents.
    Accumulates usage across all turns for accurate cost tracking.
    """
    client = anthropic.AsyncAnthropic()
    api_args = assemble_api_call(role, context, worker_results)

    use_tools = role in WRITE_TOOL_ROLES
    tools = [_WRITE_FILE_TOOL, _READ_FILE_TOOL] if use_tools else []
    messages = api_args["messages"]

    # Accumulate token counts across all turns in a tool-use loop
    total_input = 0
    total_output = 0
    total_cache_write = 0
    total_cache_read = 0

    for _ in range(max_tool_iterations):
        kwargs: dict = {
            "model": model,
            "max_tokens": 8096,
            "system": api_args["system"],
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        response = await client.messages.create(**kwargs)

        total_input += getattr(response.usage, "input_tokens", 0)
        total_output += getattr(response.usage, "output_tokens", 0)
        total_cache_write += getattr(response.usage, "cache_creation_input_tokens", 0)
        total_cache_read += getattr(response.usage, "cache_read_input_tokens", 0)

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = _execute_tool(block.name, block.input)
                    log_event("tool_call", {
                        "instance_id": instance_id,
                        "tool": block.name,
                        "path": block.input.get("path", ""),
                    })
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]
            continue

        # end_turn — record cost and return
        result_text = next(
            (block.text for block in response.content if hasattr(block, "text")), ""
        )

        class _Usage:
            input_tokens = total_input
            output_tokens = total_output
            cache_creation_input_tokens = total_cache_write
            cache_read_input_tokens = total_cache_read

        cost, status = budget_mod.record_call(
            agent_role=role,
            agent_instance=instance_id,
            spawned_by=spawned_by,
            tier=TIER_MAP.get(role, 4),
            model=model,
            usage=_Usage(),
        )

        log_event("agent_call", {
            "instance_id": instance_id,
            "role": role,
            "model": model,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cost": cost,
            "budget_status": status,
        })

        return result_text

    raise RuntimeError(f"{instance_id} exceeded max tool iterations ({max_tool_iterations})")
