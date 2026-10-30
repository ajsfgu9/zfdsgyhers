"""Configuration management for Ouroboros Recursive Manager Agent."""

from __future__ import annotations

import dataclasses
import os
import pathlib
from typing import Dict, List


@dataclasses.dataclass
class ModelProviderConfig:
    """Configuration for primary and fallback LLM endpoints."""

    name: str = "openai_compatible"
    api_mode: str = "chat_completions"  # chat_completions | anthropic_messages | gemini_native
    base_url: str = dataclasses.field(
        default_factory=lambda: os.environ.get("OUROBOROS_API_BASE", "https://api.openai.com/v1")
    )
    api_key_env: str = "OUROBOROS_API_KEY"
    model: str = dataclasses.field(
        default_factory=lambda: os.environ.get("OUROBOROS_MODEL", "gpt-4.1")
    )
    max_context_tokens: int = 128_000
    timeout_seconds: int = 120


@dataclasses.dataclass
class AgentConfig:
    """Runtime and governance configuration for the recursive loop."""

    workspace_root: pathlib.Path = dataclasses.field(
        default_factory=lambda: pathlib.Path(
            os.environ.get("OUROBOROS_WORKSPACE", "./workspace_state")
        ).resolve()
    )
    skills_dir: pathlib.Path = dataclasses.field(
        default_factory=lambda: pathlib.Path(
            os.environ.get("OUROBOROS_SKILLS_DIR", "./skills")
        ).resolve()
    )
    max_turns: int = 100
    subagent_max_turns: int = 30
    max_recursion_depth: int = 3
    parallel_tool_workers: int = 8
    compression_trigger_ratio: float = 0.50
    compression_protect_last_n: int = 12
    skill_learning_min_tool_calls: int = 5
    auto_approve_tier_0: bool = True
    auto_approve_tier_1: bool = True
    require_human_on_tier_2: bool = True
    primary_provider: ModelProviderConfig = dataclasses.field(
        default_factory=ModelProviderConfig
    )
    fallback_providers: List[ModelProviderConfig] = dataclasses.field(default_factory=list)
    tier_2_sensitive_patterns: List[str] = dataclasses.field(
        default_factory=lambda: [
            "rm -rf /",
            "mkfs",
            "dd if=",
            "iptables -F",
            "/etc/sudoers",
            "/etc/ssh/sshd_config",
            "CODEOWNERS",
            "REVIEW.md",
            "AGENTS.md",
            ".github/workflows/",
        ]
    )

    def ensure_directories(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        (self.workspace_root / "planner").mkdir(parents=True, exist_ok=True)
        (self.workspace_root / "worktrees").mkdir(parents=True, exist_ok=True)
        (self.workspace_root / "logs").mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
