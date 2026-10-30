"""Configuration management for Ouroboros Recursive Manager Agent with live `.env` refresh."""

from __future__ import annotations

import dataclasses
import os
import pathlib
from typing import Any, List, Optional, Tuple, Union

from ouroboros.env_loader import LiveEnvLoader, resolve_telegram_credentials


@dataclasses.dataclass
class ModelProviderConfig:
    """Configuration for primary and fallback LLM endpoints."""

    name: str = "openai_compatible"
    api_mode: str = "chat_completions"
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
    """Runtime, Telegram, plugin, and governance configuration for the recursive loop."""

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
    plugins_dir: pathlib.Path = dataclasses.field(
        default_factory=lambda: pathlib.Path(
            os.environ.get("OUROBOROS_PLUGINS_DIR", "./plugins")
        ).resolve()
    )
    env_file: Optional[pathlib.Path] = None
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
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: List[Union[int, str]] = dataclasses.field(default_factory=list)
    telegram_webhook_secret: str = dataclasses.field(
        default_factory=lambda: os.environ.get("OUROBOROS_TELEGRAM_WEBHOOK_SECRET", "")
    )
    telegram_api_base: str = dataclasses.field(
        default_factory=lambda: os.environ.get(
            "OUROBOROS_TELEGRAM_API_BASE", "https://api.telegram.org"
        )
    )
    telegram_poll_timeout: int = 10
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

    def __post_init__(self) -> None:
        search = [self.env_file] if self.env_file else None
        self._env_loader = LiveEnvLoader(search_paths=search)
        self.refresh_from_env(force=True)

    def refresh_from_env(self, force: bool = False) -> Tuple[bool, List[str]]:
        """Hot-reloads `.env` if modified on disk and updates runtime fields in-place."""
        changed, updated_keys = self._env_loader.reload_if_changed(force=force)
        if os.environ.get("OUROBOROS_API_BASE"):
            self.primary_provider.base_url = os.environ["OUROBOROS_API_BASE"].strip()
        if os.environ.get("OUROBOROS_MODEL"):
            self.primary_provider.model = os.environ["OUROBOROS_MODEL"].strip()
        if not os.environ.get("OUROBOROS_API_KEY") and os.environ.get("OPENAI_API_KEY"):
            os.environ["OUROBOROS_API_KEY"] = os.environ["OPENAI_API_KEY"]
        if os.environ.get("OUROBOROS_TELEGRAM_WEBHOOK_SECRET"):
            self.telegram_webhook_secret = os.environ["OUROBOROS_TELEGRAM_WEBHOOK_SECRET"].strip()

        token, env_chat_ids = resolve_telegram_credentials()
        if token and (changed or not self.telegram_bot_token):
            self.telegram_bot_token = token
        if env_chat_ids and (changed or not self.telegram_allowed_chat_ids):
            normalized: List[Union[int, str]] = []
            for cid in env_chat_ids:
                try:
                    normalized.append(int(cid))
                except ValueError:
                    normalized.append(cid)
            self.telegram_allowed_chat_ids = normalized
        return changed, updated_keys

    def ensure_directories(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        (self.workspace_root / "planner").mkdir(parents=True, exist_ok=True)
        (self.workspace_root / "worktrees").mkdir(parents=True, exist_ok=True)
        (self.workspace_root / "logs").mkdir(parents=True, exist_ok=True)
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass
        try:
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass
