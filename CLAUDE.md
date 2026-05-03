# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

An autonomous multi-agent software development system. A human provides an idea or design file; a hierarchy of AI agents plans, builds, tests, and iterates until working code lands in GitHub. The human interacts only at six defined checkpoints.

The orchestrator is written in **Python**. Projects it generates should use whatever language best fits the project — TypeScript is the default stack.

## Commands

```bash
# Install (run once from devteam/)
pip install -e .

# Start a new project (run from any empty project directory)
python -m orchestrator        # or: devteam

# Run tests
pytest

# Run a single test
pytest tests/test_budget.py::test_record_call_calculates_cost_correctly

# Lint
ruff check .

# Type check
mypy orchestrator/
```

## Repository Layout

```
orchestrator/         # Python orchestrator (the non-agent core)
  __main__.py         # python -m orchestrator entry point
  main.py             # Bootstrap, conversation loop, pass loop
  agent.py            # Anthropic API caller with tool-use loop
  work_plan.py        # Parallel work plan executor
  context.py          # Prompt assembly and cache layout
  budget.py           # Budget tracking and enforcement
  github_client.py    # All GitHub API calls live here
  docker_runner.py    # Docker sandbox test execution
  git_ops.py          # All git subprocess calls live here
  logger.py           # Append-only log.jsonl writer
  checkpoint.py       # Checkpoint formatting and human I/O
prompts/              # Agent system prompts (one per role, loaded from package location)
docker/               # Dockerfile templates per stack (node.dockerfile, python.dockerfile)
tests/                # pytest test suite
pyproject.toml        # Package definition and tooling config
.env                  # Secrets — never committed, .gitignore written first
```

Each project the orchestrator works on gets its own directory with its own `config.json`, `run/`, and `decisions/`. The `devteam` repo is the tool, not the workspace — run `python -m orchestrator` from the project directory, not from here.

## Core Architecture

### Two Invariants That Shape Everything

**Communication never skips tiers.** Workers escalate to their manager. Managers escalate to the top-level agent. The top-level agent escalates to the human. No agent reaches across or up two tiers — not to get context, not to report results, not to ask questions. If a worker needs something it wasn't given, it flags the gap to its manager; the manager re-spawns with a richer context packet or escalates further.

**Managers own context assembly for their workers.** Each manager assembles the context packet for every worker it spawns, filtering down only what that worker needs. The top-level agent does not directly package context for tier-4 workers. This is the primary cost-control mechanism — each tier strips out everything the next tier down doesn't need.

### The Fundamental Split

**Orchestrator** (Python, not an agent) owns everything deterministic: spawning agents via API calls, budget tracking and enforcement, git/GitHub operations, Docker execution, logging, routing messages, loop caps, and surfacing checkpoints to the human.

**Agents** (Claude API calls) own judgment: decomposition decisions, quality assessments, content, strategy. Agents never directly spawn other agents — they produce JSON work plans; the orchestrator reads those plans and makes the API calls.

### Agent Hierarchy

```
Human (Tier 0)
  └── top_level_agent (Tier 1) ←→ Orchestrator
        ├── budget_manager (Tier 1)
        ├── pm (Tier 2)
        ├── architect (Tier 2)
        ├── dev_manager (Tier 3)
        │     └── [N × dev_worker] (Tier 4)
        │     └── integrator (Tier 4)
        ├── qa_manager (Tier 3)
        │     └── [N × test_worker] (Tier 4)
        │     └── [N × reviewer] (Tier 4)
        ├── debug_manager (Tier 3)
        │     └── [N × debug_worker] (Tier 4)
        ├── release_summarizer (Tier 5)
        └── docs_writer (Tier 5)
```

Team size is **dynamic**. Managers decide how many workers to spawn and with what specialization. Never hardcode team shape.

### The Main Loop

1. On startup: check for `run/checkpoint.json`. If found, resume; surface summary to human and wait for go-ahead.
2. `top_level_agent` reads the project folder and produces a structured JSON work plan.
3. Orchestrator executes each action in the plan: spawn agent → receive response → calculate cost from `response.usage` → append to `log.jsonl` → update `budget.json` → check budget.
4. After every agent call: if spend ≥ 90%, warn human; if ≥ 100%, halt (finish in-flight call, write completion summary, stop).
5. After a completed pass: `git add . && git commit && git tag`, write `run/checkpoint.json`, surface pass summary checkpoint, wait for human.

### Bootstrap Sequence (new projects only)

`.gitignore` must be committed **before any other files**. `.env` lives in `.gitignore` and must never be committed. Only after `.gitignore` is committed does the orchestrator proceed with anything else.

After clarifying questions and before the pass loop: `top_level_agent` decides the project folder structure (it owns everything outside `run/`) and writes `decisions/brief.md` — the source of truth for the PM and all downstream agents. The `brief_confirmation` checkpoint follows; nothing else begins until the human confirms.

### Work Plan Schema

Three action types: `spawn`, `aggregate`, `checkpoint`. Managers produce `spawn` and `aggregate`; only `top_level_agent` produces `checkpoint`.

```json
{
  "type": "work_plan",
  "produced_by": "dev_manager_1",
  "actions": [
    {
      "action": "spawn",
      "role": "dev_worker",
      "instance_id": "dev_worker_1",
      "depends_on": [],
      "context": {
        "system_prompt": "You are a TypeScript specialist focused on JWT auth...",
        "documents": ["decisions/architecture.md"],
        "inline": "Own the auth module. GitHub issue: #12."
      },
      "model": "claude-haiku-4-5"
    },
    {
      "action": "aggregate",
      "role": "dev_manager",
      "instance_id": "dev_manager_1",
      "depends_on": ["dev_worker_1", "dev_worker_2"],
      "context": {
        "documents": ["decisions/architecture.md"],
        "worker_results": ["dev_worker_1", "dev_worker_2"],
        "inline": "Review for interface consistency."
      },
      "model": "claude-sonnet-4-6"
    },
    {
      "action": "checkpoint",
      "name": "spec_approval",
      "depends_on": ["pm_1"],
      "summary": "...",
      "key_decisions": ["..."],
      "options": ["[A] Approve", "[B] Request changes", "[C] Stop"]
    }
  ]
}
```

`system_prompt` in context is used for tier-4 workers — the spawning manager writes it. Tier 1–3 and tier 5 agents load `prompts/<role>.md` instead. `depends_on` enforces sequencing. `aggregate` calls the manager again with worker outputs. `checkpoint` surfaces to the human and waits; orchestrator injects cost automatically.

### Context Assembly

The `context` object has two fields:
- **`documents`** — file paths the orchestrator reads and injects as stable cached content
- **`inline`** — manager's scoping notes and interface contracts (dynamic, never cached)

Orchestrator assembles each API call in fixed order with cache breakpoints: system prompt → resolved documents → inline/worker results. Managers never embed full document text — they reference file paths and write instructions in `inline`.

### Communication Model

All inter-agent information flows through the orchestrator. Two channels:
- **Large artifacts (code, tests):** workers write via git; next tier reads from there
- **Small outputs (analysis, flags, decisions):** flow as return values — worker results to manager via `aggregate`, manager output to top-level agent

The top-level agent never sees raw worker output — only the manager's aggregated result.

## Budget Tracking

Cost is calculated by the orchestrator immediately after every API call — agents never self-report cost.

```python
cost = (usage.input_tokens / 1_000_000) * INPUT_PRICE_PER_M \
     + (usage.output_tokens / 1_000_000) * OUTPUT_PRICE_PER_M
```

`run/budget.json` schema includes a `calls` array (not a per-agent map) because the team is dynamic — agent names aren't known in advance. The call log supports any aggregation after the fact. Each entry includes `spawned_by` so you can reconstruct the spawn tree when debugging runaway costs.

Budget status: `"ok"` → `"warning"` (≥90%) → `"halted"` (≥100%). Only the orchestrator writes this file.

## GitHub Integration

GitHub is the project backbone. The orchestrator handles all GitHub API calls — no agent calls GitHub directly.

Key mapping: spec approval → open milestone + one issue per user story; architecture approval → one branch per module (`module/auth`, etc.); dev worker completes → commits to module branch with issue reference; integration complete → module PRs merge to `integration`; pass complete → integration PR opened against `main`.

Use a dedicated bot account (not personal). Token must be fine-grained, scoped to specific repos, minimum permissions (issues, PRs, contents, actions). Rate limit: 5,000 requests/hour — minimum 15s poll intervals. Hard cap on GitHub calls per run is in `config.json`.

## Docker Test Execution

Agent-generated code **never runs on the host machine**. Always via Docker with: network disabled, 512MB memory limit, 50% CPU max, read-only rootfs, code mounted read-only, results dir write-only, `AutoRemove: true`.

Test containers write structured JSON to `/results/output.json`. The orchestrator parses pass/fail routing deterministically — no agent needed to interpret results. Only the failure details (not the full results object) are passed to `debug_manager`.

Layer the Dockerfile so dependency installation is cached separately from code — rebuilds after code changes should take seconds.

GitHub Actions triggers on PR open as the final official gate, not as the primary test runner.

## Prompt Files

Static prompt files exist for tier 1–3 and tier 5 agents only (9 files). Tier-4 workers (dev_worker, test_worker, debug_worker, reviewer, integrator) receive manager-generated system prompts via the `system_prompt` context field — their identity is specialised per assignment.

Prompts are versioned alongside code via git. The orchestrator records the active git commit hash at the start of each pass. Prompts may change between passes but **must not change during a pass**.

Every static prompt has five sections: Identity, What you receive, Authority/constraints, Output format (with schema), Current task. Manager prompts additionally document how to generate worker system prompts and the work plan format.

Model assignments are proposed by `budget_manager` and approved by the human at the budget checkpoint. They live in `run/budget.json` for that run — not in `config.json`.

## Six Human Checkpoints

The system pauses at exactly these moments — nothing else interrupts unless something goes wrong:

1. **Brief confirmation** — after clarifying questions, before anything is created
2. **Spec approval** — last cheap moment to change scope
3. **Architecture approval** — last cheap moment to change technical decisions
4. **Budget approval** — work only starts after explicit human approval
5. **Pass summary** — after each full pass; human decides continue/redirect/stop
6. **Escalation** — only when the system cannot resolve something autonomously

## What Agents Must Never Do

- Write to `run/log.jsonl` or `run/budget.json`
- Spawn other agents directly (produce a work plan; orchestrator spawns)
- Make GitHub API calls directly
- Run code outside Docker
- Read or write `.env`
- Change `config.json` during a pass
- Write code directly (the top-level agent coordinates, it does not write code)

## Deterministic Tools — Never Use Agents For These

Linting (ruff/ESLint), type checking (mypy/tsc), formatting (Black/Prettier), file search (ripgrep), token counting (tiktoken), test execution (pytest/Jest via Docker), git operations, GitHub API calls, file diffing. If it's mechanical and deterministic, it's a tool.

## Anthropic API Integration

**Non-streaming.** The orchestrator waits for each agent's full response before acting. Cost is read directly from `response.usage` after each call.

**Prompt caching — two breakpoints per call:**
1. After the system prompt (role identity, authority model)
2. After stable project documents (spec, architecture doc — don't change mid-pass)

Dynamic content (current task, context packet) is always last and never cached.

**GitHub REST via `PyGithub` or `httpx`.** The orchestrator calls GitHub directly as Python code. MCP is for agents; the orchestrator is not an agent.

## Agent Output Conventions

Agents never run git. The orchestrator commits after each tier. Agents that produce files indicate this in their output:

- **No-tool agents** (pm, architect, budget_manager, release_summarizer): return `{"file": "path", "content": "..."}` — orchestrator writes to disk
- **Write-tool agents** (dev_worker, test_worker, debug_worker, integrator, docs_writer): write files directly, then return `{"status": "complete", "files_written": [...], "commit_message": "..."}` — orchestrator commits using the suggested message
- **Reviewer**: returns `{"verdict": "approve|request_changes|comment", "issues": [...]}` — orchestrator posts as GitHub PR review
- **Manager agents**: return work plan JSON
- **top_level_agent**: returns work plan JSON (orchestration mode) or plain text (conversation mode)

## Explicitly Rejected Decisions

- **Agents self-reporting cost** — orchestrator measures from API response; agents can't reliably self-measure
- **Hardcoded team size** — over-resources simple tasks, under-resources complex ones
- **Version manager reading code files** — git log is free; reading code for changelogs is expensive
- **GitHub Actions as primary test loop** — slow feedback, costs Actions minutes; Docker runs locally first
- **Mid-pass resumption (v1)** — restart the pass; acceptable cost, too complex to implement correctly
- **Per-agent map in budget.json** — assumes fixed team; call log supports dynamic teams
- **Single agent for PM + architect** — spec and architecture both deserve focused agents
- **Streaming API calls** — orchestrator waits for full responses; progress comes from the agent response hierarchy; cost reads cleanly from `response.usage`
- **GitHub MCP for orchestrator** — MCP is for agents; orchestrator calls GitHub REST directly as Python code
