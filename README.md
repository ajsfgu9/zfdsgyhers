# Ouroboros Agent (`ouroboros-agent`)

**A Recursive Autonomous Manager Agent for Dedicated Linux Servers — Synthesizing Gemini Task Mode, Nous Hermes Agent, OpenClaw, Grok Bot, and Meta Muse under Automated Code Review Governance.**

## Architectural Lineage

Ouroboros combines five complementary agent paradigms into a single zero-dependency Python runtime designed to live on your own dedicated server:

| Reference System | Core Mechanism Adopted in Ouroboros | Where It Lives |
| :--- | :--- | :--- |
| **Gemini Task Mode** | Two-phase planning (`Phase 1` decomposition + `Phase 2` live `TASK.md` milestone state machine), parallel tool fan-out, and on-demand `lookup_skills` | `ouroboros/loop.py`, `ouroboros/memory.py` |
| **Nous Hermes Agent** | Strict message role alternation, preflight context compression (>50% window without splitting tool pairs), and **Closed Learning Loop** (`save_skill` after >=5 tool calls) | `ouroboros/loop.py`, `ouroboros/skills.py` |
| **OpenClaw** | Event-driven HTTP Gateway (`/v1/tasks`, `/v1/state`, `/v1/reload`, `/v1/telegram/webhook`, `/healthz`) and bidirectional Telegram Bot API bridge | `ouroboros/gateway.py`, `ouroboros/telegram_bot.py`, `main.py` |
| **Grok Bot & Meta Muse** | Chief-of-Staff recursive sub-agent delegation (`delegate_subagent` with isolated context and bounded `IterationBudget`) + Sentinel fail-closed `Tier 2` guards | `ouroboros/tools.py`, `ouroboros/loop.py` |
| **Code Review Governance** | Concise `AGENTS.md` (<150 lines), failure-mode `REVIEW.md`, diff-driven `CODEOWNERS` (`Tier 0/1/2`), and automated circuit breakers + `incident_evals.jsonl` | `AGENTS.md`, `REVIEW.md`, `scripts/circuit_breaker.py` |

## Telegram Bot API Integration

Send commands and receive outputs directly from Telegram:
1. Message `@BotFather` on Telegram -> `/newbot` -> copy your token into `.env` as `OUROBOROS_TELEGRAM_BOT_TOKEN=<token>`.
2. Start the agent (`python3 main.py --mode daemon` or `python3 main.py --mode telegram`) and send `/id` to your bot on Telegram.
3. Copy your `chat_id` into `.env` as `OUROBOROS_TELEGRAM_ALLOWED_CHAT_IDS=<your_chat_id>` and restart the service.
4. Use Telegram commands anytime:
   - `<any text>` or `/task <prompt>` — Run a full autonomous agent task with persistent chat memory.
   - `/cmd <linux_command>` — Execute a Linux shell command directly and receive `exit_code`, `stdout`, and `stderr`.
   - `/approve <id>` / `/deny <id>` — Approve or reject high-risk Sentinel Tier-2 commands directly from Telegram.
   - `/skills` or `/reload` — Hot-reload `skills/*/SKILL.md` files from disk without restarting the server.
   - `/status` — View active `TASK.md` checklist and installed skills.

## In-Place Upgrade on Existing Linux Servers

If `/opt/ouroboros-agent` already exists and `git clone` fails with `destination path already exists and is not an empty directory`, run:
```bash
bash scripts/upgrade_in_place.sh /opt/ouroboros-agent
```
This preserves `.env` and `workspace_state/`, repairs Linux ownership on `skills/`, runs the test suite, and restarts `ouroboros.service`.
