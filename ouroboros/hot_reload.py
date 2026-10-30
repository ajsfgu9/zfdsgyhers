"""Zero-Downtime Hot-Reload Supervisor (`ouroboros/hot_reload.py`).

Allows a long-lived Ouroboros daemon to evolve without ever restarting the Python process
or losing SQLite sessions, background processes, or in-flight state:
1. `.env` hot-reloading (`AgentConfig.refresh_from_env`)
2. `skills/*/SKILL.md` hot-reloading (`SkillRegistry.reload`)
3. `plugins/*.py` dynamic tool hot-loading (drop a `.py` file in `plugins/` to add/update tools live!)
4. Core `ouroboros/*.py` live module reloading via `importlib.reload` with fault-tolerant rollback
   if a syntax error is present in an edited file.
"""

from __future__ import annotations

import importlib
import importlib.util
import pathlib
import sys
import time
import traceback
from typing import Any, Dict, List


class HotReloadSupervisor:
    """Monitors `.env`, `skills/`, `plugins/*.py`, and core `ouroboros/*.py` for zero-restart updates."""

    CORE_MODULE_NAMES = (
        "ouroboros.env_loader",
        "ouroboros.config",
        "ouroboros.skills",
        "ouroboros.multi_agent",
        "ouroboros.tools",
    )

    def __init__(self, plugins_dir: pathlib.Path) -> None:
        self.plugins_dir = plugins_dir.resolve()
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self._plugin_mtimes: Dict[pathlib.Path, int] = {}
        self._core_mtimes: Dict[str, int] = {}
        self.loaded_plugins: Dict[str, List[str]] = {}
        self.last_reload_timestamp: float = time.time()
        self.last_errors: List[str] = []
        self._snapshot_core_mtimes()

    def _snapshot_core_mtimes(self) -> None:
        for mod_name in self.CORE_MODULE_NAMES:
            mod = sys.modules.get(mod_name)
            if mod and getattr(mod, "__file__", None):
                p = pathlib.Path(mod.__file__).resolve()
                if p.is_file():
                    self._core_mtimes[mod_name] = p.stat().st_mtime_ns

    def sync_all(self, agent: Any, force: bool = False) -> Dict[str, Any]:
        """Runs a non-destructive hot-reload pass across .env, skills, plugins, and core modules."""
        self.last_errors.clear()

        # 1. Hot-reload .env
        env_changed, updated_env_keys = agent.config.refresh_from_env(force=force)

        # 2. Hot-reload skills/*/SKILL.md
        agent.skills.reload()

        # 3. Hot-reload plugins/*.py into agent.tools
        reloaded_plugins = self._reload_plugins(agent.tools, force=force)

        # 4. Hot-reload core modules if modified on disk (fault-tolerant)
        reloaded_core = self._reload_core_modules_if_changed(agent, force=force)

        if env_changed or reloaded_plugins or reloaded_core or force:
            self.last_reload_timestamp = time.time()

        return {
            "env_changed": env_changed,
            "updated_env_keys": updated_env_keys,
            "active_skills_count": len(agent.skills._skills),
            "reloaded_plugins": reloaded_plugins,
            "active_plugins": dict(self.loaded_plugins),
            "reloaded_core_modules": reloaded_core,
            "errors": list(self.last_errors),
        }

    def _reload_plugins(self, tool_registry: Any, force: bool = False) -> List[str]:
        reloaded: List[str] = []
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        current_files = sorted(self.plugins_dir.glob("*.py"))

        for py_file in current_files:
            if py_file.name.startswith("_"):
                continue
            try:
                mtime_ns = py_file.stat().st_mtime_ns
            except OSError:
                continue

            if not force and self._plugin_mtimes.get(py_file) == mtime_ns:
                continue

            try:
                spec = importlib.util.spec_from_file_location(
                    f"ouroboros_plugin_{py_file.stem}", py_file
                )
                if not spec or not spec.loader:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                before_tools = set(tool_registry._tools.keys())
                if hasattr(module, "register_plugins") and callable(module.register_plugins):
                    module.register_plugins(tool_registry)
                elif hasattr(module, "TOOLS") and isinstance(module.TOOLS, list):
                    for tspec in module.TOOLS:
                        tool_registry.register(tspec)
                after_tools = set(tool_registry._tools.keys())

                self._plugin_mtimes[py_file] = mtime_ns
                self.loaded_plugins[py_file.name] = sorted(after_tools - before_tools) or ["registered"]
                reloaded.append(py_file.name)
            except Exception as exc:
                err_msg = f"Plugin {py_file.name} failed to load (kept existing tools alive): {type(exc).__name__}: {exc}"
                self.last_errors.append(err_msg)

        return reloaded

    def _reload_core_modules_if_changed(self, agent: Any, force: bool = False) -> List[str]:
        reloaded_mods: List[str] = []
        for mod_name in self.CORE_MODULE_NAMES:
            mod = sys.modules.get(mod_name)
            if not mod or not getattr(mod, "__file__", None):
                continue
            p = pathlib.Path(mod.__file__).resolve()
            if not p.is_file():
                continue
            try:
                mtime_ns = p.stat().st_mtime_ns
            except OSError:
                continue
            if not force and self._core_mtimes.get(mod_name) == mtime_ns:
                continue

            try:
                # Syntax pre-check before reloading into sys.modules
                source = p.read_text(encoding="utf-8")
                compile(source, str(p), "exec")
                importlib.reload(mod)
                self._core_mtimes[mod_name] = mtime_ns
                reloaded_mods.append(mod_name)
            except Exception as exc:
                err_msg = (
                    f"Core module {mod_name} had syntax/reload error (staying on last known-good version): "
                    f"{type(exc).__name__}: {exc}"
                )
                self.last_errors.append(err_msg)

        # If ouroboros.multi_agent was reloaded, refresh role policies on the live agent
        if "ouroboros.multi_agent" in reloaded_mods:
            ma_mod = sys.modules["ouroboros.multi_agent"]
            agent.role_policies_registry = ma_mod.build_quant_desk_policies()

        return reloaded_mods
