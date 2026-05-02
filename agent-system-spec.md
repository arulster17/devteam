# Multi-Agent Software Development System — Full Specification

This document captures the complete design of an autonomous multi-agent software development system. It is intended to be the source of truth for implementation and can be condensed into a CLAUDE.md later. All design decisions, rationale, constraints, and open questions are preserved here.

---

## 1. What This System Is

The system takes a project idea or design document from a human and autonomously produces working, tested, version-controlled software using a hierarchy of AI agents. GitHub is the project backbone. Docker provides sandboxed test execution. The human interacts only at defined checkpoints.

The core interaction loop:

```
Human gives idea or file
  → Top-level agent asks clarifying questions
  → Human confirms understanding
  → Agents plan, design, build, test, and iterate
  → Human approves at key milestones
  → Working code lands in GitHub
```

---

## 2. Core Design Principles

### Agents reason. Tools execute.
Anything deterministic — diffing, linting, formatting, running tests, git operations, file search, token counting — is handled by tools, not agents. Agents are only invoked when judgment is required. Every role should be audited against this question: could a deterministic tool do this instead? If yes, use the tool.

### Structure owns the pipes. Agents own the water.
What is hardcoded in the system: message envelope schemas, escalation paths, loop caps, checkpoint moments, tool permissions, budget enforcement, spawn authority. What is left to agents: content of messages, decomposition decisions, strategies, quality judgments, context assembly.

### No agent spawns agents directly.
Agents produce work plans that *describe* what should be spawned. The orchestrator reads those plans and does the actual spawning. This keeps the orchestrator in control of the call log and budget at all times. A dev_manager produces a plan saying "spawn these three workers with these context packets." The orchestrator reads that plan and makes the API calls.

### GitHub is the backbone.
All code, history, issues, reviews, and releases live in GitHub. The project folder holds decisions and run state. Nothing important exists only in memory or the local filesystem.

### Every action is logged.
The orchestrator writes an append-only event to log.jsonl after every single action. No exceptions.

### Dynamic team, not static team.
The system never assumes a fixed number or type of agents. Structure defines what a worker *is* (inputs, outputs, tools, caps). Managers decide how many to spawn and with what specialization. A manager might decide "this feature needs a database specialist and a security-focused reviewer" rather than generic agents.

### Communication never skips tiers.
Information flows strictly through the hierarchy. Workers escalate to their manager. Managers escalate to the top-level agent. The top-level agent escalates to the human. No agent reaches across or up two tiers — not to get context, not to report results, not to ask questions. If a worker needs something it wasn't given, it flags the gap to its manager; the manager decides whether to re-spawn with a richer context packet or escalate further.

### Managers own context assembly for their workers.
Each manager assembles the context packet for every worker it spawns. It receives context from its own tier and filters down only what each worker needs. The top-level agent does not directly package context for tier-4 workers. This is the primary mechanism for aggressive context scoping — each tier strips out everything the next tier down doesn't need.

---

## 3. Architecture Overview

```
Human (Tier 0)
  └── Top-Level Agent (Tier 1) ←→ Orchestrator
        ├── Budget Manager (Tier 1)
        ├── PM (Tier 2)
        ├── Architect (Tier 2)
        ├── Dev Manager (Tier 3)
        │     └── [N × Dev Workers] (Tier 4)
        │     └── Integrator (Tier 4)
        ├── QA Manager (Tier 3)
        │     └── [N × Test Workers] (Tier 4)
        │     └── [N × Reviewers] (Tier 4)
        ├── Debug Manager (Tier 3)
        │     └── [N × Debug Workers] (Tier 4)
        ├── Release Summarizer (Tier 5)
        └── Docs Writer (Tier 5)

Orchestrator (not an agent) owns:
  GitHub API ↔ git CLI ↔ Docker ↔ budget.json ↔ log.jsonl
```

### The Hierarchical / Manager-Worker Pattern

Managers don't just route work — they *shape* workers. Each spawned worker gets:
- A custom context packet assembled by the manager (only what that worker needs)
- A specialized system prompt flavored for its specific assignment
- A clear deliverable definition
- Explicit interface contracts from adjacent modules

This means the team is fully dynamic. The dev manager might spawn 2 workers for a simple project or 7 for a complex one, each with different specializations, based on what the architecture actually requires.

This pattern applies across multiple tiers:
- QA manager spins up targeted testers per module
- Debug manager groups related failures and assigns diagnosis tasks
- A research tier (not in v1 but possible) could spawn agents to investigate specific technical unknowns before any code is written

The pattern is: goal arrives at tier → manager reasons about decomposition → workers execute in parallel → manager aggregates and quality checks → either done or loops back.

---

## 4. Human Interaction Model

### Interface
CLI. The top-level agent prints to stdout, the human types responses. No web UI in v1. Add one later if the CLI becomes limiting.

### The Six Checkpoints

The system pauses and waits for human response at exactly these six moments. Nothing else interrupts unless something goes wrong.

All checkpoints use this format:

```
CHECKPOINT: [name]
─────────────────────────────────────────
SUMMARY
  Brief description of what just happened.

KEY DECISIONS / FINDINGS
  The 3–5 things most worth your attention.

COST SO FAR
  $X.XX spent of $Y.YY budget  (or: budget not yet set)

YOUR OPTIONS
  [A] ...
  [B] ...

Waiting for your response.
```

The six checkpoints:

**1. Brief confirmation**
After the top-level agent has read the idea/file and asked clarifying questions. Human confirms the system understood correctly. Nothing is created before this.

**2. Spec approval**
After the PM agent produces the full spec. Last cheap moment to change scope. A wrong assumption here propagates everywhere downstream.

**3. Architecture approval**
After the architect produces the technical design. Last cheap moment to change technical decisions.

**4. Budget approval**
After the budget manager proposes a budget and model assignments. Human approves the number, adjusts it, or asks for descoping. Work only starts after explicit approval. The budget proposal includes a range (not a false precise figure), a recommended ceiling (top of range + 20% buffer), and model assignments per role with brief justification.

**5. Pass summary**
After each full pass completes. System reports what was built, test results, cost this pass, total cost, and a recommendation for what to do next. Human decides: continue, change direction, or stop.

**6. Escalation**
When the system cannot resolve something autonomously. Human makes the call. This should be rare.

### What "Rare" Means for Escalation

The system should handle the vast majority of problems itself. Escalation to human is only appropriate when:
- The spec is contradictory and cannot be resolved with a reasonable assumption
- Architecture requires a fundamental change mid-build
- Budget needs to increase
- The top-level agent has exhausted its retry attempts on something

Everything else — failed tests, quality issues, minor conflicts, ambiguous requirements — is handled within the agent hierarchy.

---

## 5. Top-Level Agent

This is the most important agent in the system. It is the thread of continuity across every run and every pass.

### Two Explicit Modes

**Conversation mode:** talking to the human. Taking ideas, design files, feedback. Asking clarifying questions. Presenting checkpoints. This is the interface the human actually interacts with.

**Orchestration mode:** running the system. Reading the project folder, deciding what needs to happen, producing structured work plans the orchestrator executes, watching results, deciding what to do next.

The switch between modes is explicit, not fuzzy. The agent should not blend them. When it is in orchestration mode it produces JSON work plans, not prose.

### Reading a New Project

When given an idea or design file, extract:
- What type of project this is and rough scope
- What the core goal is (not just the features — the actual purpose)
- What is ambiguous and will cause problems downstream
- What constraints exist (tech stack preferences, timeline, budget hints)
- What the MVP is — the smallest useful thing

Then ask 3–5 targeted clarifying questions that resolve the biggest ambiguities. No more. Confirm understanding before proceeding.

### Reading an Existing Project

On resumption, reconstruct where things stand by reading the project folder:
- What was the last completed pass?
- What was in progress when the system stopped?
- Were there any unresolved issues?
- What did the last run cost?
- Is there anything requiring attention before continuing?

Surface a clear summary to the human before asking what to do next. The human should not have to remember where things left off.

### Authority Model

**Can decide autonomously:**
- Which agents to spawn and in what order
- What context to give each spawned agent
- Whether output meets quality bar
- Whether to retry or escalate
- How to sequence work within a pass
- Minor scope clarifications that don't change acceptance criteria
- Model selection within approved budget constraints

**Must ask human:**
- Anything that changes the core spec or acceptance criteria
- Architecture decisions with significant tradeoffs
- Budget increases
- Whether to proceed when something is fundamentally broken
- Anything it is genuinely uncertain about

### What It Never Does
- Writes code directly
- Makes GitHub API calls directly
- Writes to log.jsonl or budget.json
- Spawns agents directly (it produces work plans, orchestrator spawns)

---

## 6. Role Registry

Every role is a type, not an instance. The orchestrator assigns instance IDs at spawn time (e.g. `dev_worker_3`). The number and specialization of instances is determined by manager agents at runtime — never hardcoded.

---

### Tier 0 — Human

Not an agent. Approves specs, architecture, budgets, pass summaries. Makes decisions on escalation. Unblocks the system at checkpoints.

---

### Tier 1 — Strategic

#### `top_level_agent`
- **Receives:** human input (idea, design file, feedback), full project folder contents
- **Produces:** clarifying questions (conversation mode), structured work plans for orchestrator (orchestration mode), checkpoint summaries, escalations
- **Tools:** read project folder, write project folder
- **Spawned by:** orchestrator on startup
- **Notes:** see Section 5 for full behavior spec. This agent decides the project folder structure at init, decides what context each spawned agent receives, and has the authority model described above.

#### `budget_manager`
- **Receives:** approved project brief, role registry, architecture doc
- **Produces:** proposed budget with model assignments per role (with justification), estimated token ranges per tier, recommended iteration caps, 20% buffer calculation
- **Tools:** none
- **Spawned by:** orchestrator after architecture approval, before any dev work starts
- **Notes:** produces a proposal only. Human approves at budget checkpoint. The proposal should include a range, not a false precise figure. Can also recommend rebalancing if early stages come in significantly under budget. Can run a pre-flight estimation before the full proposal to catch cases where the plan would clearly blow any reasonable budget.

---

### Tier 2 — Planning

#### `pm`
- **Receives:** approved project brief
- **Produces:** full spec — user stories, acceptance criteria, edge cases, explicit out-of-scope list. Output as structured document the architect can reference unambiguously.
- **Tools:** none
- **Spawned by:** orchestrator
- **Notes:** the spec is the last cheap moment to change scope. Downstream changes are expensive.

#### `architect`
- **Receives:** approved spec
- **Produces:** architecture doc — file structure, data models, API contracts, tech stack recommendation, module boundaries, dependency graph between modules, interface contracts between modules
- **Tools:** none
- **Spawned by:** orchestrator
- **Notes:** the architecture doc is the most important artifact in the system. All tier 3 and tier 4 agents read it. It is the reference point for all downstream decisions. Should be scoped to what is actually needed — over-engineering here multiplies cost at every downstream tier.

---

### Tier 3 — Management

#### `dev_manager`
- **Receives:** architecture doc, spec, current project state, open GitHub issues
- **Produces:** work plan — how many dev workers to spawn, what each one owns, dependency ordering between workers, context packet per worker (only what that worker needs), recommended model per worker
- **Tools:** read GitHub issues
- **Spawned by:** orchestrator
- **Notes:** reads open GitHub issues to build the work plan — it has real data to reason about rather than just the architecture doc. Expresses dependency ordering explicitly so the orchestrator can enforce sequencing. Does not assume a fixed team size.

#### `qa_manager`
- **Receives:** architecture doc, completed code (via PR diffs, not full files), spec
- **Produces:** test plan — how many test workers to spawn, coverage assignments, test types per module (unit / integration / edge case), context packet per worker
- **Tools:** read GitHub PR diffs
- **Spawned by:** orchestrator after dev workers and integrator complete
- **Notes:** reads PR diffs rather than raw files — cheaper and focused on what actually changed.

#### `debug_manager`
- **Receives:** Docker test run results (structured JSON), architecture doc, relevant code sections
- **Produces:** debug assignments — groups related failures, assigns each group to a debug worker with a scoped context packet containing only the failing tests and the code they cover
- **Tools:** read files
- **Spawned by:** orchestrator when Docker test run reports failures
- **Notes:** grouping related failures before assigning them prevents duplicate fix attempts and conflicting patches.

---

### Tier 4 — Execution

#### `dev_worker`
- **Receives:** architecture doc, assigned module spec from dev_manager context packet, interface contracts from adjacent modules, existing code if iterating
- **Produces:** code files for assigned module. Commits to assigned module branch. References GitHub issue number in commit messages.
- **Tools:** read files, write files, read GitHub issues
- **Spawned by:** orchestrator per dev_manager work plan
- **Notes:** multiple instances run in parallel up to configured cap (default 5). Each only sees its own module's context — not the full codebase. When it hits ambiguity, see Section 10 (Inter-Agent Ambiguity).

#### `reviewer`
- **Receives:** PR diff, relevant spec section, architecture doc
- **Produces:** structured issue list — each issue has severity (blocker / major / minor), file location, line reference, description, suggested fix. Posts this as a formal GitHub PR review.
- **Tools:** read files (PR diff)
- **Spawned by:** orchestrator after dev workers complete
- **Notes:** blockers become PR change requests. Majors and minors become PR comments. Produces structured data, not prose. Downstream agents parse the issue list.

#### `test_worker`
- **Receives:** module code, spec section for that module, interface contracts
- **Produces:** test files for assigned module (unit tests, integration tests, edge cases per qa_manager assignment)
- **Tools:** read files, write files
- **Spawned by:** orchestrator per qa_manager test plan

#### `debug_worker`
- **Receives:** specific failing tests, the code those tests cover, diagnosis assignment from debug_manager
- **Produces:** patched code files, root cause explanation. Creates fix branch, commits, opens PR, closes linked GitHub issue on merge.
- **Tools:** read files, write files
- **Spawned by:** orchestrator per debug_manager assignments

#### `integrator`
- **Receives:** all completed module outputs, architecture doc, interface contracts
- **Produces:** integration layer code, explicit flags for interface mismatches between modules
- **Tools:** read files, write files
- **Spawned by:** orchestrator after all dev workers in a pass complete
- **Notes:** checks for naming conflicts, inconsistent interfaces, duplicated logic between modules. This aggregation step is where cross-module bugs commonly live.

---

### Tier 5 — Bookkeeping

#### `release_summarizer`
- **Receives:** `git log --oneline` output for the milestone, closed GitHub issue titles, merged PR titles
- **Produces:** changelog entry, version bump recommendation (patch / minor / major)
- **Tools:** none
- **Spawned by:** orchestrator on human-requested release only — not triggered every pass
- **Notes:** reads git log output (plain text, tiny). Does NOT read code files. Versioning by reading code files would be prohibitively expensive. Git gives all the information needed for free.

#### `docs_writer`
- **Receives:** completed code, spec, architecture doc
- **Produces:** documentation appropriate to scope — inline comments, API reference, README
- **Tools:** read files, write files
- **Spawned by:** orchestrator, optional per project config

---

## 7. What the Orchestrator Owns

The following never lives inside any agent:

- Running the test suite (Docker subprocess)
- Budget tracking and enforcement
- Routing messages between tiers
- Human checkpoint surfacing and waiting
- Loop caps and halt logic
- Spawning agents (agents produce plans, orchestrator spawns)
- Git operations (commit, branch, tag, push)
- GitHub API calls (create repo, open PRs, post issues)
- Writing to log.jsonl and budget.json
- Interpreting Docker test results (pass/fail routing is deterministic)
- Polling GitHub Actions status

The orchestrator has no opinions. It executes what the top-level agent decides, enforces the hard constraints, and logs everything.

---

## 8. Orchestrator Main Loop

```
STARTUP
  check for run/checkpoint.json
  if exists:
    read it, reconstruct pass state
    surface resume summary to human: what was completed, what was in progress
    wait for human go-ahead
  if not:
    run bootstrap sequence (Section 9)

PASS LOOP
  top-level agent reads project folder, determines next action
  produces structured work plan (JSON envelope, agent owns content)
  
  orchestrator executes each action in work plan:
    spawn agent:
      call Anthropic API with context packet
      receive response
      calculate cost from usage object (input_tokens × rate + output_tokens × rate)
      append to log.jsonl
      update budget.json (spent, calls array)
      check budget status → if warning or halt, handle immediately
      return result to top-level agent
    
    run tool:
      shell out (git, docker, ripgrep, etc.)
      capture stdout/stderr/exit code
      append to log.jsonl
      return result
    
    checkpoint:
      format checkpoint message
      print to stdout
      wait for human input
      append response to log.jsonl
      return response
  
  after every agent call: check budget
  if budget warning (≥90%):
    surface to human with remaining work summary
    ask: increase budget / continue / stop
    wait for response
  if budget halt (≥100%):
    finish current in-flight call
    write completion summary (done / in-progress / not-started)
    surface to human with resume option
    stop
  
  if pass complete:
    shell out: git add . && git commit && git tag
    write run/checkpoint.json
    surface pass summary checkpoint
    wait for human go-ahead before next pass

SHUTDOWN
  write final state to project folder
  commit log.jsonl to git
  surface final summary
```

---

## 9. Bootstrap Sequence

Runs on first invocation of a new project only.

```
1. Human provides idea or design file
2. Top-level agent reads it
3. Top-level agent asks 3–5 clarifying questions (conversation mode)
4. Human answers
5. Top-level agent creates project folder structure (it decides the structure)
6. Orchestrator creates GitHub repo
7. Orchestrator sets up branches: main, integration
8. Orchestrator writes .gitignore (must include .env before anything else happens)
9. Top-level agent writes decisions/brief.md summarizing the project
10. CHECKPOINT: brief confirmation — human confirms understanding is correct
11. Normal pass loop begins
```

**Critical:** .gitignore must be committed before any other files. The bot token and any other credentials live in .env and must never be committed.

---

## 10. Budget Tracking

### Schema

Budget lives in run/budget.json. The orchestrator is the only writer.

```json
{
  "total": 20.00,
  "spent": 12.43,
  "status": "ok",
  "calls": [
    {
      "id": "call_a3f9",
      "agent_role": "dev_worker",
      "agent_instance": "dev_worker_3",
      "spawned_by": "dev_manager_1",
      "tier": 4,
      "model": "claude-haiku-4-5",
      "input_tokens": 18400,
      "output_tokens": 3200,
      "cost": 0.103,
      "timestamp": "2025-01-15T10:23:41Z"
    }
  ]
}
```

Budget status values: `"ok"` | `"warning"` | `"halted"`

**Why a call log instead of a per-agent map:** the team is dynamic. Agent names are not known in advance. The call log lets you derive any aggregation after the fact — cost by role, cost by tier, cost by model, cost by spawning manager — without assuming anything about team shape.

**Why `spawned_by`:** lets you reconstruct the full spawn tree. Essential for debugging runaway costs — which manager decision caused this?

### How Cost Is Measured

```javascript
// orchestrator does this immediately after every API call
const usage = response.usage;
const cost = (usage.input_tokens / 1_000_000) * INPUT_PRICE_PER_M
           + (usage.output_tokens / 1_000_000) * OUTPUT_PRICE_PER_M;

state.budget.spent += cost;
state.budget.calls.push({
  id: generateId(),
  agent_role: agentRole,
  agent_instance: agentInstance,
  spawned_by: spawnedBy,
  tier: tier,
  model: model,
  input_tokens: usage.input_tokens,
  output_tokens: usage.output_tokens,
  cost: cost,
  timestamp: new Date().toISOString()
});
```

Agents never self-report cost. The orchestrator measures them from the API response.

### Budget Rules

- Check budget status after every single agent call
- At 90%: status → "warning". Surface to human immediately with remaining work summary. Ask: increase budget / continue anyway / stop.
- At 100%: status → "halted". Finish current in-flight call. Write completion summary. Surface to human with resume option. Stop.
- Budget is set at budget approval checkpoint. Nothing starts before it is approved.
- Budget is approved by human, not set by agents.

### Budget Estimation

The budget manager proposes after architecture is approved. Proposal includes:
- Estimated token ranges per tier (ranges, not false precision)
- Model assignments per role with brief justification
- Recommended iteration caps (debug loops, review loops)
- 20% buffer on top of range ceiling
- Recommended total as a number the human can approve or adjust

The budget manager can also do a cheap pre-flight estimation before proposing — asking "given this work plan, roughly how many tokens will this require?" to catch obviously over-budget plans before committing to them.

---

## 11. Cost Model and Optimization

### Approximate Pricing (verify current rates)

| Model | Input per 1M tokens | Output per 1M tokens |
|---|---|---|
| Claude Opus 4 | ~$15 | ~$75 |
| Claude Sonnet 4 | ~$3 | ~$15 |
| Claude Haiku 4.5 | ~$0.80 | ~$4 |

### Rough Estimate for a Basic App (CRUD / simple API + frontend)

| Stage | Notes | Estimate |
|---|---|---|
| Planning tier | 2–3 calls, light context | $0.10–0.30 |
| Dev manager | 1 call, reads architecture | $0.05–0.15 |
| Dev agents (×3) | 30k input, 10k output each | ~$1.50 |
| Review + QA tier | Reads all code | $1.00–2.00 |
| Debug loop (2–3 iterations) | Re-reads code + test output | $1.00–3.00 |
| **Total (optimistic)** | | **$3–8 on Sonnet** |

Budget $20–50 for experimentation including system development iterations.

### Key Cost Drivers

**Context stuffing is the silent killer.** Every agent call includes system prompt + project state + relevant files + history. A mid-pipeline call might be 20k–50k tokens of input before producing a single output token. Aggressive context scoping is the single biggest optimization.

**Iteration loops multiply everything.** A debug loop that runs 5 times costs 5× what a single pass would.

**Model choice is the biggest lever.** Sonnet vs Haiku is roughly 4× cost difference. Assign models by whether judgment or mechanical execution is required.

### Optimization Strategies

**Tier your models.** Managers and agents requiring judgment get Sonnet. Mechanical execution agents (test writers, formatters, changelog agents) get Haiku. This alone cuts costs 60–70%.

**Aggressive context scoping.** Don't pass the whole codebase to every agent. Pass only what that agent needs. A frontend worker doesn't need the database schema details.

**Use prompt caching.** Anthropic's prompt caching discounts repeated system prompts and stable documents (architecture doc, spec) on re-reads. For agents that run in loops with the same base context, this adds up significantly.

**Set loop caps.** A debug agent looping 20 times is a $10+ surprise. Cap every loop at 3–5 iterations, escalate if unresolved.

**Prototype with Haiku everywhere first.** Get orchestration logic right cheaply, then upgrade specific agents to Sonnet/Opus once you know where quality matters.

**Pre-flight estimation.** Before spawning a batch of agents, the manager can do a cheap estimation call to check whether the planned work would blow the budget before it starts.

**Cost as a quality signal.** If the system is burning $50 on a todo app, that's telling you something architectural is off — agents re-reading unnecessary context, loops not converging, managers over-decomposing simple tasks.

---

## 12. GitHub Integration

GitHub is the project backbone. All code, history, reviews, and releases live there.

### How Work Maps to GitHub Primitives

| Event | GitHub Action |
|---|---|
| Project starts | Create repo, create main branch |
| Spec approved | Open milestone, create one issue per user story |
| Architecture approved | Create branch per module (`module/auth`, `module/api`, etc.) |
| Dev worker completes | Commits to module branch, references issue number in message |
| Review complete | Posts structured review as formal GitHub PR review |
| Tests pass | PR gets approved label |
| Integration complete | Module PRs merged to integration branch |
| Pass complete | Integration branch PR opened against main |
| Release requested | Tag created, release notes generated |
| Bug found | Issue opened, linked to failing test |
| Debug worker fixes | Commits to fix branch, closes linked issue on merge |

### Bot Account

Use a dedicated GitHub bot account, not your personal account. Reasons: clean personal commit history, separate rate limits, can revoke without affecting personal access, clear attribution when something goes wrong.

Setup:
1. Create new GitHub account (e.g. `yourname-dev-agent`)
2. Add as collaborator on repo with write access (not admin)
3. Generate a **fine-grained personal access token** scoped to specific repos and specific permissions only (issues, PRs, contents, actions — no org-level access)
4. Store token in `.env` file — must be in `.gitignore` before any agent touches files

### Rate Limits

GitHub REST API: 5,000 authenticated requests/hour. Well within limits for a single project. Avoid tight polling loops — minimum 15 second intervals. The orchestrator enforces a hard cap on GitHub API calls per run (in config.json). GraphQL API has separate point-based limits (5,000 points/hour) — relevant for complex queries.

### GitHub Actions

Actions trigger on PR open, not on every commit. This saves Actions minutes and keeps fast feedback in Docker. Actions serves as the final official gate before merge, not the primary test runner.

Free tier gives 2,000 Actions minutes/month. With Docker catching most failures before PRs are opened, this is comfortable.

---

## 13. Docker Test Execution

Agent-generated code is untrusted. It never runs directly on the host machine. Docker is the security boundary.

### Container Constraints

```javascript
{
  NetworkDisabled: true,           // no network access
  HostConfig: {
    Memory: 512 * 1024 * 1024,    // 512mb memory limit
    CpuPeriod: 100000,
    CpuQuota: 50000,               // 50% CPU max
    ReadonlyRootfs: true,          // no filesystem writes except mounted dirs
    Binds: [
      `${codePath}:/app:ro`,       // code is read-only
      `${resultsPath}:/results:rw` // results dir is write-only
    ],
    AutoRemove: true               // clean up container after exit
  }
}
```

The container can read its code and write to a results directory. Nothing else.

### Test Result Schema

Containers write structured JSON to `/results/output.json`. The orchestrator reads this directly — no agent needed to interpret pass/fail.

```json
{
  "lint": {
    "passed": false,
    "errors": [
      {
        "file": "src/auth.js",
        "line": 42,
        "rule": "no-unused-vars",
        "message": "x is defined but never used"
      }
    ]
  },
  "types": {
    "passed": true,
    "errors": []
  },
  "tests": {
    "passed": false,
    "summary": { "total": 24, "passed": 21, "failed": 3 },
    "failures": [
      {
        "test": "auth > should reject invalid token",
        "file": "src/auth.test.js",
        "error": "Expected 401 received 200",
        "stack": "..."
      }
    ],
    "coverage": { "lines": 84, "branches": 71 }
  }
}
```

Only the failure details (not the full results object) get passed to the debug_manager. Context scoping applies here too.

### Dockerfile Structure (Layer Caching)

Layer the Dockerfile so dependency installation is cached separately from code. This makes rebuilds after code changes take seconds, not minutes.

```dockerfile
# Layer 1: dependencies — only rebuilds when package.json changes
FROM node:20-slim
WORKDIR /app
COPY package*.json ./
RUN npm ci

# Layer 2: code — rebuilds on every code change
COPY . .

# Layer 3: test runner
RUN npm run lint && npm run typecheck

FROM base AS test
RUN npm test -- --coverage --json --outputFile=/results/output.json
```

### Execution Flow

```
dev_worker commits to branch
  → orchestrator builds Docker image (layer cache makes this fast)
    → lint container runs
    → type check container runs
    → test container runs
      → all pass → open PR → GitHub Actions confirms → merge
      → failures → parse results → debug_manager
                                    → debug_worker fixes
                                      → loop back to Docker
```

GitHub Actions at the PR level is the final official gate. Docker handles fast local feedback during iteration. Most failures are caught and fixed before a PR is ever opened.

---

## 14. Versioning

Git handles all versioning. No agent reads code files for the purpose of diffing or changelog generation.

| Task | Tool |
|---|---|
| Diffing | `git diff` |
| Changelogs | `git log --oneline` |
| File manifests | `git ls-files` |
| Reverting bad output | `git checkout` / `git revert` |
| Branching per agent | `git branch` |
| Tagging versions | `git tag` |
| History | `git log` |

The orchestrator shells out to git automatically after each tier completes. No agent is involved in routine versioning.

The `release_summarizer` agent runs only when the human explicitly requests a release. It reads `git log` output (plain text, tiny tokens) — not code files. Everything needed for a changelog is already in the git history.

---

## 15. Deterministic Tools

These tasks are always handled by tools, never agents. An agent doing any of these is a cost and quality problem.

| Task | Tool |
|---|---|
| Syntax checking | ESLint / Ruff |
| Type checking | TypeScript (`tsc`) / mypy |
| Code formatting | Prettier / Black |
| File search | ripgrep |
| Token counting | tiktoken (local) |
| Test execution | Jest / pytest (via Docker) |
| Git operations | git CLI |
| GitHub operations | GitHub MCP / REST API |
| File diffing | git diff |
| Dependency install | npm ci / pip install |
| Container execution | Docker CLI |

The rule: if it's mechanical, deterministic, or file-system-level, it's a tool. If it requires judgment, it's an agent.

---

## 16. Error Handling

Errors bubble up only when the current tier genuinely cannot resolve them.

### Retry Automatically (no judgment needed)
- GitHub API timeout → retry 3× with exponential backoff
- Docker container crash → rebuild and retry once
- Agent returns malformed JSON → retry the call once with a correction note appended to the prompt
- GitHub poll timeout → retry after backoff, surface to human if repeatedly failing

### Agent Handles It
- Worker hits ambiguity in spec → flags in output, manager decides how to proceed
- Test failure after fix attempt → debug worker gets another iteration within its cap
- Minor merge conflict → integrator resolves it
- Reviewer finds issues → dev worker iterates within review cap

### Top-Level Agent Handles It
- Worker fails quality check twice in a row
- Debug loop hits iteration cap without resolving
- Two workers produce conflicting interfaces
- GitHub Actions workflow fails in unexpected way
- Any pattern suggesting something systematic is wrong

### Escalate to Human
- Spec is contradictory and cannot be resolved with a reasonable assumption
- Architecture requires a fundamental change mid-build
- Budget needs to increase
- Top-level agent's retry attempts are exhausted
- Something is fundamentally broken that the system cannot diagnose

### Review Iteration Loop

When the qa_manager aggregate reports reviewer blockers, the pass loops:

1. Top-level agent spawns `dev_manager` again with the blocker list as primary context. Dev_manager may spawn only the affected workers rather than the full team.
2. Top-level agent spawns `qa_manager` again targeting the same modules to confirm blockers are resolved.
3. This loop is capped by `max_review_iterations` in `config.json` (default: 3). Blockers persisting after the cap → escalate to human.

Majors and minors from reviewers do not block the pass. They surface in the `pass_summary` checkpoint for the human to address in a future pass.

Workers signal inability to complete an assignment via a structured blocked output: `{"status": "blocked", "reason": "...", "what_I_need": "..."}`. Managers surface blocked workers in their aggregate output unchanged — blocked signals are never silently swallowed at any tier.

---

## 17. Inter-Agent Ambiguity

When a worker encounters something unclear mid-task:

**Minor ambiguity** (could reasonably go either way, wrong choice is reversible): make best judgment call, flag the decision explicitly in output so the reviewer can check it.

**Significant ambiguity** (wrong choice would break an interface or violate a spec requirement): halt the specific task, surface the question to the managing tier agent. Manager decides and relays back. Do not guess silently.

**Spec-level ambiguity** (the spec itself is unclear or contradictory): worker flags to manager. Manager escalates to top-level agent. Top-level agent decides whether it can resolve autonomously or must escalate to human.

Communication never skips tiers. A worker does not surface questions directly to the top-level agent. Escalation always goes up one step at a time.

Workers should flag rather than assume on anything significant. A flagged assumption is visible and correctable. A silent wrong guess compounds through the whole system.

---

## 18. Project Folder Structure

The top-level agent decides the structure at initialization. Two files are non-negotiable and orchestrator-managed:

```
project/
  run/
    log.jsonl          ← append-only event log (orchestrator only)
    budget.json        ← live spend tracking (orchestrator only)
    checkpoint.json    ← last completed pass snapshot
  .env                 ← credentials (in .gitignore, never committed)
  .gitignore           ← committed first, before anything else
  config.json          ← tunable parameters (see Section 20)
```

Everything else — decisions/, prompts/, how specs are organized — is decided by the top-level agent at init and kept consistent across passes.

---

## 19. Prompt Versioning

Agent prompts live as markdown files in the repository:

```
prompts/
  top_level_agent.md
  budget_manager.md
  pm.md
  architect.md
  dev_manager.md
  qa_manager.md
  debug_manager.md
  release_summarizer.md
  docs_writer.md
```

Tier-4 agents (dev_worker, test_worker, debug_worker, reviewer, integrator) do not have static prompt files. Their system prompts are generated by the manager that spawns them and passed in the `system_prompt` field of the context packet. This allows managers to specialise each worker's identity for its specific assignment.

Prompts are versioned alongside code via git. The run log records the active git commit hash at the start of each pass — any prompt version can be traced to any run. Prompts may change between passes on the same project, but must not change during a pass.

### Prompt Structure (per role)

Every prompt should have these sections:
1. **Identity** — what this agent is and what project it's working on (loaded from project folder)
2. **Project context** — relevant documents (spec, architecture, etc.) assembled dynamically
3. **Authority / constraints** — what this agent can decide vs. what to flag or escalate
4. **Output format** — exact schema expected, with examples
5. **Current task** — what it has been asked to do right now

Manager prompts additionally include the spawn plan format — what a valid work plan looks like — since the orchestrator parses this.

---

## 20. Configuration

All tunable parameters live in `config.json` at the project root. Agents may read this file. The human may edit it between passes. It must not change during a pass.

```json
{
  "limits": {
    "max_parallel_workers": 5,
    "max_debug_iterations": 3,
    "max_review_iterations": 3,
    "max_total_passes": 10,
    "github_poll_interval_seconds": 15,
    "max_github_calls_per_run": 500
  },
  "checkpoints": {
    "require_approval_after_spec": true,
    "require_approval_after_architecture": true,
    "require_approval_after_budget": true,
    "warn_at_budget_percent": 90
  },
  "github": {
    "repo": "owner/repo",
    "integration_branch": "integration",
    "main_branch": "main",
    "protect_main": true
  },
  "stack": {
    "language": "typescript",
    "test_runner": "jest",
    "linter": "eslint",
    "formatter": "prettier",
    "type_checker": "tsc"
  }
}
```

**Model assignments are not in config.json.** They are proposed by the budget_manager and approved by the human at the budget checkpoint. Once approved they are written to `run/budget.json` and locked for that run. This allows per-run model flexibility while keeping config stable.

---

## 21. Resumability

The system supports between-pass resumption. Mid-pass resumption is not supported in v1 — if a run stops mid-pass, that pass restarts from the beginning. Passes are cheap enough that rerunning one is acceptable.

At the end of every completed pass, the orchestrator writes `run/checkpoint.json` containing the pass number and a summary of completed work. On startup, the orchestrator checks for this file. If found, it surfaces a resume summary to the human before continuing.

---

## 22. Observability

The orchestrator appends an event to `run/log.jsonl` after every single action. Log entries are never deleted or modified.

The git commit hash is recorded at the start of each pass — prompt versions and code state can always be traced back.

At the end of each pass, the human sees:
```
Pass 2 complete
  Cost this pass:     $3.42
  Total spent:        $6.11 of $12.00
  Tests passing:      21/24
```

No metrics dashboard in v1. The log contains enough detail to answer any retrospective question. Build a dashboard if you find yourself wanting it.

---

## 23. Multi-Pass Strategy

After pass 1 the human directs what subsequent passes focus on. The top-level agent reads the current project state and determines which roles need to run — it does not always run a full cycle.

Examples:
- "Fix the failing tests" → debug_manager + debug_workers + QA only
- "Add the export feature" → full cycle from pm through QA
- "Clean up the auth module" → dev_worker + reviewer + QA for that module only
- "Write the docs" → docs_writer only

The top-level agent presents its plan for the pass at the pass summary checkpoint. Human approves before work begins.

---

## 24. Pass Summary Format

```
CHECKPOINT: Pass [N] Complete
─────────────────────────────────────────
SUMMARY
  What was built or changed this pass.

TEST RESULTS
  Passing: 21/24
  Coverage: 84% lines, 71% branches
  Open issues: [links to any blocking GitHub issues]

COST
  This pass:   $3.42
  Total spent: $6.11 of $12.00

WHAT'S NEXT
  Top-level agent's recommendation for pass N+1.

YOUR OPTIONS
  [A] Continue with recommended next pass
  [B] Describe a different focus for next pass
  [C] Stop here

Waiting for your response.
```

---

## 25. Work Plan Schema

Manager agents produce work plans the orchestrator executes. Two action types: `spawn` and `aggregate`.

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
        "documents": ["decisions/architecture.md", "decisions/spec.md"],
        "inline": "Own the auth module. Interface contract with api module: POST /auth/token returns {token, expires_at}."
      },
      "model": "claude-haiku-4-5"
    },
    {
      "action": "spawn",
      "role": "dev_worker",
      "instance_id": "dev_worker_2",
      "depends_on": [],
      "context": {
        "documents": ["decisions/architecture.md"],
        "inline": "Own the api module. Do not implement auth logic — consume the auth interface."
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
        "inline": "Review worker outputs for interface consistency and flag any mismatches."
      },
      "model": "claude-sonnet-4-6"
    }
  ]
}
```

### Action types

**`spawn`** — creates a new agent instance. `depends_on` is how managers express sequencing; the orchestrator enforces it.

**`aggregate`** — calls the manager again with worker results. `worker_results` lists instance IDs whose outputs the orchestrator injects into the context. Use this for quality checks, aggregation, and conflict detection after workers complete.

**`checkpoint`** — produced by the top-level agent only. The orchestrator formats and surfaces the checkpoint to the human and waits for a response before continuing. Fields: `name` (one of the six checkpoint names), `summary`, `key_decisions`, `options`. The orchestrator injects cost automatically.

### Context `system_prompt` field

Tier-4 workers (dev_worker, test_worker, debug_worker, reviewer, integrator) receive a `system_prompt` field in their context packet instead of loading a static prompt file. The spawning manager writes this field — it contains the worker's specialised identity, module ownership, interface contracts, and output expectations. The orchestrator uses it as the system prompt for that API call.

```json
{
  "action": "spawn",
  "role": "dev_worker",
  "instance_id": "dev_worker_1",
  "context": {
    "system_prompt": "You are a TypeScript specialist focused on JWT authentication...",
    "documents": ["decisions/architecture.md"],
    "inline": "GitHub issue: #12. Own the auth module only."
  },
  "model": "claude-haiku-4-5"
}
```

If `system_prompt` is absent, the orchestrator falls back to `prompts/<role>.md`.

### Context assembly

The `context` object uses two fields:
- **`documents`** — file paths the orchestrator reads and injects as stable cached content
- **`inline`** — manager's specific framing, scoping notes, and interface contracts for this agent (dynamic, never cached)

The orchestrator assembles the final API call in a fixed order, applying prompt cache breakpoints:
1. System prompt (from `prompts/<role>.md`) — cache breakpoint after
2. Resolved document contents — cache breakpoint after
3. Inline context + worker results (if aggregate) — dynamic, no cache

Managers never embed full document text directly. They reference file paths and write scoping instructions in `inline`. This keeps work plans readable and lets the orchestrator control cache placement.

### Communication model

All inter-agent information flows through the orchestrator — no agent calls another directly.

Two channels:
- **Large artifacts (code, test files):** workers write to the project folder via git. Next tier reads from there. No message passing needed.
- **Small outputs (analysis, flags, decisions):** flow as return values through the orchestrator. Worker results return to the manager via `aggregate`. Manager output returns to the top-level agent.

The top-level agent never sees raw worker output directly — it receives the manager's aggregated result. Tier skipping is not possible in this model.

---

## 26. Security and Safety

### Agent-Generated Code
- Never runs on the host machine
- Always executes in Docker with the constraints in Section 13
- No network access during execution
- Read-only access to its own code

### Credentials
- All secrets in `.env`
- `.gitignore` committed before any other files
- Bot token scoped to minimum required permissions (fine-grained, repo-specific)
- No agent has access to `.env`

### GitHub Bot Permissions
- Write access to specific repos
- No admin access
- No org-level access
- Fine-grained token — only the permissions actually needed

### Runaway Agent Protection
- Hard loop caps at every tier (in config.json)
- Hard GitHub API call cap per run (in config.json)
- Budget halt is a hard stop, not a suggestion
- Test with private repos during development

---

## 27. What Agents Must Never Do

- Write to `run/log.jsonl` or `run/budget.json` (orchestrator only)
- Spawn other agents directly (produce a work plan, orchestrator spawns)
- Make decisions about budget or whether to continue a run
- Run code outside of Docker
- Read or write `.env`
- Commit credentials or secrets to any file
- Access files outside their defined tool permissions
- Make GitHub API calls directly (go through the orchestrator)
- Change `config.json` during a pass
- Write code directly (top-level agent only — its job is coordination)

---

## 28. Assumptions and Defaults

These were agreed during design. Can be revisited.

| Decision | Default | Reason |
|---|---|---|
| Language/stack | Whatever fits the project; TypeScript + Jest + ESLint + Prettier + tsc is the default | Flexible — architect recommends, human approves |
| Human interface | CLI | Zero extra work, focus on the system |
| Budget default | $20 per run | Calibrated for a basic app with buffer |
| Max parallel workers | 5 | Balances speed vs debuggability |
| Debug iteration cap | 3 | Prevents expensive stuck loops |
| Review iteration cap | 3 | Same |
| Where it runs | Local machine | Docker handles sandboxing |
| Multi-project | Not in v1 | Adds state complexity |
| Learning across projects | Not in v1 | Adds complexity, revisit later |
| Mid-pass resumption | Not in v1 | Restart the pass, acceptable cost |
| Metrics dashboard | Not in v1 | Log is sufficient, build later |

---

## 29. Still To Be Built (Not Designed Yet)

These are known gaps that need work before or during implementation:

- **Docker image per stack** — base images for TypeScript, Python, etc. need to be defined
- **The orchestrator itself** — the actual code that implements the loop in Section 8

---

## 30. Design Decisions That Were Explicitly Rejected

Keeping these here so the reasoning isn't lost:

**Agents self-reporting cost:** rejected because agents can't reliably measure themselves and it creates incentive misalignment. Orchestrator measures from API response.

**Hardcoded team size:** rejected because it over-resources simple tasks and under-resources complex ones. Structure defines roles, managers decide counts.

**Version manager agent reading code files:** rejected as prohibitively expensive. Git does this for free.

**Running tests via GitHub Actions as primary loop:** rejected because Actions minutes cost money and the feedback loop is slow. Docker runs locally first; Actions is the final gate.

**Mid-pass resumption in v1:** rejected as too complex for the benefit. Restart the pass.

**Per-agent map in budget.json:** rejected because it assumes a fixed team. Call log supports dynamic team.

**Single agent doing both PM and architect work:** possible for simple projects but rejected because the spec and architecture are both important enough to deserve focused agents. Combining them risks one dominating the other.

**Streaming API calls:** rejected. The orchestrator waits for each agent's full response before deciding the next action — there is no case where it acts on a partial stream. Progress visibility is provided by the agent response hierarchy (managers read worker outputs) and orchestrator-level CLI status lines. Non-streaming keeps cost calculation simple: read directly from `response.usage`.

**GitHub MCP for orchestrator GitHub operations:** rejected. MCP is designed for agents to use as tools. The orchestrator is Python code and owns all GitHub operations directly — it calls the GitHub REST API via PyGithub or httpx. Agents must never call GitHub directly.
