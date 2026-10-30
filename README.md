# Ouroboros Agent (`ouroboros-agent`)

**A Recursive Autonomous Manager Agent for Dedicated Linux Servers — Synthesizing Gemini Task Mode, Nous Hermes Agent, OpenClaw, Grok Bot, and Meta Muse under Automated Code Review Governance.**

## Architectural Lineage

Ouroboros combines five complementary agent paradigms into a single zero-dependency Python runtime designed to live on your own dedicated server:

| Reference System | Core Mechanism Adopted in Ouroboros | Where It Lives |
| :--- | :--- | :--- |
| **Gemini Task Mode** | Two-phase planning (`Phase 1` decomposition + `Phase 2` live `TASK.md` milestone state machine), parallel tool fan-out, and on-demand `lookup_skills` | `ouroboros/loop.py`, `ouroboros/memory.py` |
| **Nous Hermes Agent** | Strict message role alternation, preflight context compression (>50% window without splitting tool pairs), and **Closed Learning Loop** (`save_skill` after >=5 tool calls) | `ouroboros/loop.py`, `ouroboros/skills.py` |
| **OpenClaw** | Event-driven HTTP Gateway (`/v1/tasks`, `/v1/state`, `/healthz`) and scheduled cron wakeups without CPU busy-polling | `ouroboros/gateway.py`, `main.py` |
| **Grok Bot & Meta Muse** | Chief-of-Staff recursive sub-agent delegation (`delegate_subagent` with isolated context and bounded `IterationBudget`) + Sentinel fail-closed `Tier 2` guards | `ouroboros/tools.py`, `ouroboros/loop.py` |
| **Code Review Governance** | Concise `AGENTS.md` (<150 lines), failure-mode `REVIEW.md`, diff-driven `CODEOWNERS` (`Tier 0/1/2`), and automated circuit breakers + `incident_evals.jsonl` | `AGENTS.md`, `REVIEW.md`, `scripts/circuit_breaker.py` |

## Repository Structure

```text
ouroboros-agent/
├── AGENTS.md                              # <150-line architectural map & invariants for AI/human reviewers
├── REVIEW.md                              # Subsystem failure definitions, file:line evidence rules, 3-round cap
├── CODEOWNERS                             # Risk-tiered ownership (Tier 0 = 0 humans, Tier 1 = 1, Tier 2 = 2)
├── Dockerfile                             # Production container image for dedicated servers
├── docker-compose.yml                     # Single-command server deployment with persistent volumes
├── main.py                                # CLI & HTTP/Cron Gateway Daemon entrypoint
├── pyproject.toml                         # Standard Python package manifest
├── .env.example                           # Environment configuration template
├── .github/
│   ├── pull_request_template.md           # Blast-radius & <5 min rollback attestation
│   └── workflows/governance-gate.yml      # Automated test + circuit-breaker CI gate
├── ouroboros/
│   ├── config.py                          # Dataclass runtime & Sentinel Tier-2 pattern config
│   ├── loop.py                            # Recursive ReAct + TASK.md checklist + closed learning loop
│   ├── memory.py                          # Three-tier memory (SOUL.md, MEMORY.md, TASK.md) + SQLite WAL store
│   ├── skills.py                          # Dynamic SKILL.md loader, resource reader, and skill creator
│   ├── tools.py                           # ThreadPoolExecutor parallel tool runner & Sentinel risk classifier
│   └── gateway.py                         # Event-driven REST control plane & scheduled routine dispatcher
├── skills/
│   ├── skill-creator/SKILL.md             # Closed learning loop skill-authoring runbook
│   ├── self-healer/SKILL.md               # Dedicated server systemd/Docker/disk auto-remediation skill
│   └── code-review-governance/SKILL.md    # Governance audit skill
├── scripts/
│   └── circuit_breaker.py                 # Revert-rate, Sev-1/2, convergence, & churn circuit breaker CLI
├── evals/
│   ├── incident_evals.jsonl               # Golden incident regression dataset
│   └── telemetry_window.json              # Rolling 7-day merge & revert telemetry
├── systemd/
│   └── ouroboros.service                  # Native Linux systemd daemon unit
└── tests/
    └── test_ouroboros.py                  # Full unit & integration test suite
```

## Quick Start (Dedicated Linux Server)

1. **Clone & Configure Environment**:
   ```bash
   git clone https://github.com/<your-org>/ouroboros-agent.git
   cd ouroboros-agent
   cp .env.example .env
   # Edit .env with your OpenAI / Anthropic / OpenRouter / local vLLM API key
   ```

2. **Run Verification & Governance Suite**:
   ```bash
   python3 -m unittest discover -s tests -v
   python3 scripts/circuit_breaker.py --changed-files ouroboros/loop.py
   ```

3. **Run Interactive CLI or Daemon Mode**:
   ```bash
   # Single autonomous task
   python3 main.py --mode task --prompt "Audit disk space, check failed systemd units, and record findings"

   # Persistent HTTP & Scheduled Routine Daemon
   python3 main.py --mode daemon --host 127.0.0.1 --port 8787
   ```
