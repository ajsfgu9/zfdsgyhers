"""Strict Role-Separated Multi-Agent Isolation Engine & 5-Role Quant Desk Orchestrator (v0.4.0 Hardened)."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from typing import Any, Dict, List, Optional


@dataclasses.dataclass
class RoleSandboxPolicy:
    """Enforces physical context, skill, model, and filesystem isolation for a specialist agent role."""

    role_name: str
    description: str
    allowed_skills: List[str]
    denied_path_prefixes: List[str] = dataclasses.field(default_factory=list)
    readonly_path_prefixes: List[str] = dataclasses.field(default_factory=list)
    model_override: Optional[str] = None
    max_turns: int = 25

    def check_path_access(self, target_path: pathlib.Path, mode: str, shared_root: pathlib.Path) -> Optional[str]:
        resolved = target_path.resolve()
        try:
            rel = resolved.relative_to(shared_root.resolve()).as_posix()
        except ValueError:
            return None

        for denied in self.denied_path_prefixes:
            clean_denied = denied.strip("/")
            if rel == clean_denied or rel.startswith(clean_denied + "/"):
                return (
                    f"[DATA_POLLUTION_FIREWALL] Role '{self.role_name}' is strictly forbidden from "
                    f"accessing '{rel}' (protected prefix: '{clean_denied}')."
                )

        if mode in ("write", "append"):
            for ro in self.readonly_path_prefixes:
                clean_ro = ro.strip("/")
                if rel == clean_ro or rel.startswith(clean_ro + "/"):
                    return (
                        f"[DATA_POLLUTION_FIREWALL] Role '{self.role_name}' has READ-ONLY access to "
                        f"'{rel}' and cannot mutate it."
                    )
        return None

    def check_bash_command(self, command: str) -> Optional[str]:
        """Blocks shell commands that attempt to read or touch denied data slices (including glob/symlink tricks)."""
        normalized = re.sub(r"/+", "/", command)
        for denied in self.denied_path_prefixes:
            clean_denied = denied.strip("/")
            if not clean_denied:
                continue
            leaf = clean_denied.rsplit("/", 1)[-1]
            wildcard_prefix = leaf[:4] + "*" if len(leaf) >= 4 else leaf
            if (
                clean_denied in normalized
                or re.search(rf"\b{re.escape(leaf)}\b", normalized)
                or (len(leaf) >= 4 and wildcard_prefix in normalized)
            ):
                return (
                    f"[DATA_POLLUTION_FIREWALL] Role '{self.role_name}' bash command blocked: "
                    f"references forbidden path or pattern '{clean_denied}'."
                )
        for ro in self.readonly_path_prefixes:
            clean_ro = ro.strip("/")
            if clean_ro and clean_ro in normalized:
                if any(op in normalized for op in (">", "tee ", "rm ", "mv ", "cp ", "sed -i", "append", "dd ")):
                    return (
                        f"[DATA_POLLUTION_FIREWALL] Role '{self.role_name}' bash command blocked: "
                        f"attempts to mutate read-only path '{clean_ro}'."
                    )
        return None


def build_quant_desk_policies(
    frontier_model: Optional[str] = None,
    fast_model: Optional[str] = None,
) -> Dict[str, RoleSandboxPolicy]:
    return {
        "lead": RoleSandboxPolicy(
            role_name="lead",
            description="Orchestrates the 8-gate pipeline across Librarian, Scout, Sweeper, and Skeptic without grading its own homework.",
            allowed_skills=["quant-research-lead", "ai-quant-research-desk"],
            denied_path_prefixes=["data/holdout"],
            readonly_path_prefixes=["ledger", "data/develop", "data/select"],
            model_override=frontier_model,
            max_turns=40,
        ),
        "librarian": RoleSandboxPolicy(
            role_name="librarian",
            description="Sole custodian of the append-only failure ledger (`ledger/research_ledger.jsonl`) and cumulative trial counter `K`.",
            allowed_skills=["quant-librarian-ledger"],
            denied_path_prefixes=["data/holdout"],
            readonly_path_prefixes=["data/develop", "data/select"],
            model_override=fast_model,
            max_turns=15,
        ),
        "scout": RoleSandboxPolicy(
            role_name="scout",
            description="Formulates falsifiable, point-in-time hypotheses and coarse parameter grids. Physically barred from Holdout data.",
            allowed_skills=["quant-scout-sweeper"],
            denied_path_prefixes=["data/holdout"],
            readonly_path_prefixes=["ledger", "data/develop", "data/select"],
            model_override=fast_model,
            max_turns=20,
        ),
        "sweeper": RoleSandboxPolicy(
            role_name="sweeper",
            description="Executes parameter grid sweeps on Develop+Select slices (first 85%) and computes net-of-cost matrices (`0/1/5/20 bp`). Physically barred from Holdout data.",
            allowed_skills=["quant-scout-sweeper"],
            denied_path_prefixes=["data/holdout"],
            readonly_path_prefixes=["ledger", "data/develop", "data/select"],
            model_override=fast_model,
            max_turns=25,
        ),
        "skeptic": RoleSandboxPolicy(
            role_name="skeptic",
            description="Adversarial falsifier running on the strongest reasoning model. Only role permitted to read `data/holdout` for single-shot OOS replay and DSR audit.",
            allowed_skills=["quant-skeptic-adversary"],
            denied_path_prefixes=[],
            readonly_path_prefixes=["ledger", "data/develop", "data/select", "data/holdout", "handoffs"],
            model_override=frontier_model,
            max_turns=25,
        ),
    }


class QuantDeskLayout:
    def __init__(self, shared_root: pathlib.Path) -> None:
        self.shared_root = shared_root.resolve()
        self.data_develop = self.shared_root / "data" / "develop"
        self.data_select = self.shared_root / "data" / "select"
        self.data_holdout = self.shared_root / "data" / "holdout"
        self.ledger_dir = self.shared_root / "ledger"
        self.handoffs_dir = self.shared_root / "handoffs"
        self.role_workspaces_dir = self.shared_root / "workspaces"

    def ensure_structure(self) -> None:
        for d in (
            self.data_develop,
            self.data_select,
            self.data_holdout,
            self.ledger_dir,
            self.handoffs_dir,
            self.role_workspaces_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def role_workspace(self, role_name: str) -> pathlib.Path:
        clean = role_name.lower().strip().replace(" ", "_")
        ws = self.role_workspaces_dir / clean
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def write_handoff_packet(self, packet_name: str, payload: Dict[str, Any]) -> pathlib.Path:
        self.handoffs_dir.mkdir(parents=True, exist_ok=True)
        target = self.handoffs_dir / f"{packet_name}.json"
        canonical = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        target.write_text(canonical + "\n", encoding="utf-8")
        return target

    def read_handoff_packet(self, packet_name: str) -> Dict[str, Any]:
        target = self.handoffs_dir / f"{packet_name}.json"
        return json.loads(target.read_text(encoding="utf-8"))
