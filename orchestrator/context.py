from pathlib import Path

# Prompts live next to the orchestrator package, not in the project CWD.
# This lets `python -m orchestrator` run from any project directory.
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt_file(role: str) -> str:
    path = PROMPTS_DIR / f"{role}.md"
    if not path.exists():
        raise FileNotFoundError(f"No prompt file for role '{role}' at {path}")
    return path.read_text()


def resolve_documents(doc_paths: list[str]) -> str:
    parts = []
    for path_str in doc_paths:
        path = Path(path_str)
        content = path.read_text() if path.exists() else "[File not found]"
        parts.append(f"=== {path_str} ===\n{content}")
    return "\n\n".join(parts)


def format_worker_results(worker_results: dict[str, str]) -> str:
    parts = []
    for instance_id, result in worker_results.items():
        parts.append(f"=== Result from {instance_id} ===\n{result}")
    return "\n\n".join(parts)


def assemble_api_call(
    role: str,
    context: dict,
    worker_results: dict[str, str] | None = None,
) -> dict:
    """
    Build the system + messages payload for an Anthropic API call.

    Cache layout (two breakpoints):
      1. system prompt           — cache_control: ephemeral
      2. resolved documents      — cache_control: ephemeral
      3. inline + worker results — dynamic, no cache
    """
    system_prompt = context.get("system_prompt") or load_prompt_file(role)

    doc_paths = context.get("documents", [])
    documents_text = resolve_documents(doc_paths) if doc_paths else ""

    inline = context.get("inline", "")
    if worker_results:
        results_text = format_worker_results(worker_results)
        dynamic = f"{inline}\n\n{results_text}".strip()
    else:
        dynamic = inline

    user_content = []

    if documents_text:
        user_content.append({
            "type": "text",
            "text": documents_text,
            "cache_control": {"type": "ephemeral"},
        })

    user_content.append({
        "type": "text",
        "text": dynamic or "Begin.",
    })

    return {
        "system": [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {"role": "user", "content": user_content}
        ],
    }
