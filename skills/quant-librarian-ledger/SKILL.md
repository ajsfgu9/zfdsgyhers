---
name: quant-librarian-ledger
description: Maintain an append-only quantitative research failure ledger (Librarian), enforce hypothesis pre-registration, track cumulative multiple-testing trial counts (K), block retesting of falsified ideas, and govern the paper-only risk switch.
enabled: true
---
# Quant Librarian Ledger

- Manage `ledger/research_ledger.jsonl` via `python3 skills/quant-librarian-ledger/scripts/ledger_manager.py`.
- Block duplicate `NO_EDGE` retests by checking `mechanism_hash` before pre-registration.
- Increment `cumulative_k` on every `PRE_REGISTERED` experiment and keep `paper_only = true`.
