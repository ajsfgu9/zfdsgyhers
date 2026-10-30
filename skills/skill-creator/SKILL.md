---
name: skill-creator
description: Author, package, and validate new reusable SKILL.md capabilities from multi-step server workflows.
enabled: true
---

# Skill Creator (Closed Learning Loop)

When a task requires 5 or more tool calls and establishes a repeatable operational procedure (such as deploying a container, rotating logs, or running database migrations):

1. Distill the exact commands, environment prerequisites, and failure checks into a clean Markdown runbook.
2. Call `save_skill` with:
   - `name`: kebab-case identifier (e.g., `nginx-zero-downtime-reload`)
   - `description`: 1-sentence trigger condition explaining *when* future sessions should load this skill
   - `instructions_markdown`: step-by-step instructions and verification commands
   - `scripts`: optional helper scripts under `scripts/`
