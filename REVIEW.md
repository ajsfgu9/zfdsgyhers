# REVIEW.md — Automated & Human Code Review Rules

## 1. Summary & Signal Rules
- Start every review with a 1-line count: `Review Summary: <N> Important, <M> Nit, <K> Pre-existing` (or state plainly: `0 findings — clean pass`).
- Every finding MUST cite an exact `file:line` range and provide a concrete failure trace or counterexample. Never guess from symbol or file names alone.
- Drop all formatting, import-order, naming-style, and linter-covered comments.
- Cap `[Nit]` findings at **2 maximum** on Round 1, and **0** on Round 2+ re-reviews.
- Tag legacy bugs discovered in touched files as `[Pre-existing]` so they do not block the PR author unfairly.

## 2. What Failure Looks Like in This Repository
- **Recursive Loop & Context Compression (`ouroboros/loop.py`)**: Splitting an `assistant` message containing `tool_calls` from its corresponding `tool` role outputs during context compression (causes immediate 400 API rejection), unbounded recursion when spawning child agents, or dropping `TASK.md` state across turns.
- **Tool Execution & Sentinel Boundaries (`ouroboros/tools.py`)**: Out-of-order tool results returned from `ThreadPoolExecutor`, shell command injection bypassing `classify_risk_tier`, leaked background zombie processes, or executing a `Tier 2` action when `approval_callback` is `None` or returns `False`.
- **Skill Registry & Path Traversal (`ouroboros/skills.py`)**: Allowing `get_skill_resources` or `save_skill` to read or write files outside `skills_dir` via `../` path traversal.
- **Persistence & Concurrency (`ouroboros/memory.py`, `ouroboros/gateway.py`)**: Unsynchronized concurrent writes to session state or SQLite locks under multi-threaded `/v1/tasks` requests.
- **Tests (`tests/`)**: Tautological mocks (mocking the unit under test), weakened or deleted assertions without justification, or missing negative-path coverage on bug fixes.
- **Documentation (`AGENTS.md` / `README.md`)**: Instructions, CLI flags, or architectural claims that contradict actual code behavior.

## 3. Paths to Skip
Do not post review findings on the following generated or runtime paths:
- `**/workspace_state/**`, `**/*.db`, `**/*.db-wal`, `**/*.db-shm`
- `**/__pycache__/**`, `**/*.pyc`, `**/dist/**`, `**/build/**`
- `**/*.lock`

## 4. Always-Human Sensitive Paths (Tier 2 / High Risk)
Changes touching these paths ALWAYS require 2 human reviews (`Tier 2`) regardless of diff size:
- `.github/workflows/**`, `Dockerfile*`, `docker-compose.yml`, `systemd/**`
- `ouroboros/tools.py` (specifically `classify_risk_tier` and `tier_2_sensitive_patterns`)
- `scripts/circuit_breaker.py`
- `CODEOWNERS`, `REVIEW.md`, `AGENTS.md`

## 5. Convergence & Re-Review Protocol
- **Round 1**: Full review of the PR diff against repository context.
- **Rounds 2–3**: Inspect ONLY the newly added commits plus the immediate callers/callees of functions modified in those commits. Do not raise new findings on untouched Round 1 lines.
- **Bug Class Consolidation**: Report repeated patterns once as a single bug-class finding listing all affected `file:line` locations.
- **Round 3 Cap**: After 3 rounds of review and fixes, stop posting line-level comments and escalate to the human lead for architectural redesign.
