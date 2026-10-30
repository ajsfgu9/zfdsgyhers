---
name: telegram-bot-ops
description: Manage bidirectional Telegram Bot API command execution, proactive alerts via telegram_send, Tier-2 human approvals, and skill hot-reloading.
enabled: true
---

# Telegram Bot Operations (`telegram-bot-ops`)

Use this skill whenever the user asks to send outputs/alerts to Telegram, diagnose the Telegram Bot connection, or hot-reload skills on the Linux server.

## 1. Proactive Outbound Notifications (`telegram_send`)
- Call the built-in `telegram_send` tool (`Tier 0`) with:
  - `text`: The summary, command output, or alert message (`<= 4000` characters per call).
  - `chat_id` (optional): Defaults to the first chat ID in `OUROBOROS_TELEGRAM_ALLOWED_CHAT_IDS`.

## 2. Inbound Telegram Bot Commands
Users in `OUROBOROS_TELEGRAM_ALLOWED_CHAT_IDS` can send directly to the bot:
- `<any text>` or `/task <prompt>` — Runs `agent.run_task()` in persistent session `tg_<chat_id>` and returns the output.
- `/cmd <linux_command>` or `/bash <linux_command>` — Executes a shell command directly via `host_bash` and returns `exit_code`, `stdout`, and `stderr`.
- `/approve <id>` / `/deny <id>` — Approves or cancels a command blocked by the Sentinel Tier-2 guard.
- `/skills` or `/reload` — Hot-reloads all `skills/*/SKILL.md` files from disk immediately without restarting the service.
- `/status` — Displays `TASK.md` milestones and active skills.
- `/reset` — Rotates the Telegram chat's session ID to start fresh context.
- `/id` — Displays the sender's `chat_id` and authorization status.

## 3. Fixing Linux Skill Modification Permissions (`PermissionError`)
If `save_skill` returns `status: "permission_denied"`, the service user (`ouroboros`) does not own `/opt/ouroboros-agent/skills`. Fix on the host:
```bash
sudo chown -R ouroboros:ouroboros /opt/ouroboros-agent/skills /var/lib/ouroboros/workspace_state
sudo chmod -R u+rwX /opt/ouroboros-agent/skills /var/lib/ouroboros/workspace_state
```
