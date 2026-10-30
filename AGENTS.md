# AGENTS.md — Ouroboros Recursive Autonomous Manager Agent

## 1. Stack & Repository Layout
- **Runtime**: Python 3.10+ (standard library core; zero mandatory third-party runtime dependencies for air-gapped/dedicated server reliability).
- **Core Modules**:
  - `ouroboros/loop.py` — Recursive ReAct + Gemini-Task Phase-1/Phase-2 checklist engine, preflight context compression, and child sub-agent delegation.
  - `ouroboros/tools.py` — `ThreadPoolExecutor` parallel tool fan-out, background daemon process manager, and Sentinel Tier-0/1/2 risk classifier.
  - `ouroboros/skills.py` — OpenClaw/Hermes-compatible on-demand `SKILL.md` registry and closed learning loop (`save_skill`).
  - `ouroboros/memory.py` — Three-tier state (`SOUL.md`, `MEMORY.md`, `planner/TASK.md`) + SQLite WAL session and action-receipt ledger.
  - `ouroboros/gateway.py` — Event-driven HTTP control plane (`/v1/tasks`, `/v1/state`, `/healthz`) and scheduled routine runner.
  - `scripts/circuit_breaker.py` — Automated governance circuit breaker & Golden Incident Evaluator (`evals/incident_evals.jsonl`).

## 2. Build, Test, & Governance Commands
- **Run full unit & integration test suite**: `python3 -m unittest discover -s tests -v`
- **Run governance circuit breaker & incident evals**: `python3 scripts/circuit_breaker.py --evals evals/incident_evals.jsonl --telemetry evals/telemetry_window.json`
- **Run interactive CLI task**: `python3 main.py --mode task --prompt "Inspect server health and update TASK.md"`
- **Start HTTP & Cron Gateway Daemon**: `python3 main.py --mode daemon --host 0.0.0.0 --port 8787`

## 3. Core Architectural Invariants (MUST NEVER BREAK)
1. **Strict Message Role Alternation**: Conversation history in `ouroboros/loop.py` must strictly follow `system -> user -> assistant -> [tool...] -> assistant`. Never emit consecutive `user` or consecutive `assistant` messages, and never split `assistant` `tool_calls` from their matching `tool` result messages during context compression.
2. **Deterministic Parallel Tool Order**: `ServerToolRegistry.execute_tool_calls` executes independent tool calls concurrently via `ThreadPoolExecutor`, but MUST always return tool result messages in the exact index order of the assistant's `tool_calls` array.
3. **Bounded Recursion & Iteration Budgets**: Every parent agent (`max_turns=100`) and child sub-agent (`subagent_max_turns=30`, `max_recursion_depth=3`) must enforce hard iteration caps so a faulty prompt or tool loop can never spin endlessly on the dedicated server.
4. **Fail-Closed Sentinel Tier-2 Guard**: Any tool invocation matching `tier_2_sensitive_patterns` (e.g., `rm -rf /`, `/etc/sudoers`, `CODEOWNERS`, `REVIEW.md`, `AGENTS.md`, `.github/workflows/`) MUST classify as `Tier 2` and block execution unless `approval_callback` explicitly returns `True`.

## 4. Explicit Non-Goals
- Do not bundle heavy browser-GUI frameworks inside the core loop; external browser or MCP integrations belong in modular `skills/*/SKILL.md` packages.
- Do not poll external APIs in tight `while True: sleep(1)` loops; all recurring work must route through event-driven triggers in `ouroboros/gateway.py`.
