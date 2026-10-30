---
name: code-review-governance
description: Audit pull requests, enforce REVIEW.md and AGENTS.md invariants, classify diff risk tiers (Tier 0/1/2), and run automated circuit breakers.
enabled: true
---

# Code Review Governance Skill

1. Classify every diff by modified paths (`Tier 0` = docs/tests, `Tier 1` = standard code/skills, `Tier 2` = workflows, auth, Dockerfiles, governance files).
2. Cite exact `file:line` ranges and concrete failure traces for every `[Important]` finding.
3. Cap `[Nit]` comments at 2 on Round 1 and 0 on Round 2+.
4. Run `python3 scripts/circuit_breaker.py` before allowing zero-human (`Tier 0`) auto-merges.
