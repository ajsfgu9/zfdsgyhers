"""Core Recursive Manager Loop with Prompt-Prefix Cache Alignment, Unified Lock, & Industrial Hot-Reload (v0.4.0)."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import threading
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional

from ouroboros.config import AgentConfig, ModelProviderConfig
from ouroboros.hot_reload import IndustrialReloadSupervisor
from ouroboros.logger import OuroborosLogger
from ouroboros.memory import MemoryAndStateStore
from ouroboros.multi_agent import (
    QuantDeskLayout,
    RoleSandboxPolicy,
    build_quant_desk_policies,
)
from ouroboros.skills import SkillRegistry
from ouroboros.telegram import TelegramBotService
from ouroboros.tools import ServerToolRegistry


LLMCallable = Callable[[List[Dict[str, Any]], List[Dict[str, Any]], ModelProviderConfig], Dict[str, Any]]


class OuroborosManagerAgent:
    """Recursive Autonomous Manager Agent with role-isolated sub-agents, multi-tier caching, and structured logging."""

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        llm_callable: Optional[LLMCallable] = None,
        recursion_depth: int = 0,
        approval_callback: Optional[Callable[[str, Dict[str, Any], str], bool]] = None,
        role_policy: Optional[RoleSandboxPolicy] = None,
        shared_root: Optional[pathlib.Path] = None,
        role_policies_registry: Optional[Dict[str, RoleSandboxPolicy]] = None,
        execution_lock: Optional[threading.RLock] = None,
    ) -> None:
        self.config = config or AgentConfig()
        self.config.ensure_directories()
        self.recursion_depth = recursion_depth
        self.role_policy = role_policy
        self.shared_root = (shared_root or self.config.workspace_root).resolve()
        self.execution_lock = execution_lock or threading.RLock()
        self.logger = OuroborosLogger(self.config.workspace_root / "logs")
        self.quant_layout = QuantDeskLayout(self.shared_root)
        self.quant_layout.ensure_structure()
        self.role_policies_registry = role_policies_registry or build_quant_desk_policies(
            frontier_model=os.environ.get("OUROBOROS_SKEPTIC_MODEL"),
            fast_model=os.environ.get("OUROBOROS_SWEEPER_MODEL"),
        )
        self.memory = MemoryAndStateStore(self.config.workspace_root)
        self.skills = SkillRegistry(self.config.skills_dir)
        self.llm_callable = llm_callable or self._default_openai_compatible_call
        self.tools = ServerToolRegistry(
            config=self.config,
            memory=self.memory,
            skills=self.skills,
            subagent_spawner=self._spawn_child_agent,
            approval_callback=approval_callback,
            role_policy=self.role_policy,
            shared_root=self.shared_root,
        )
        self.hot_reload = IndustrialReloadSupervisor(plugins_dir=self.config.plugins_dir)
        self.telegram = TelegramBotService(agent=self, agent_lock=self.execution_lock)
        self.tools.telegram_service = self.telegram
        self.tools.hot_reload_callback = lambda: self.hot_reload.sync_all(self, force=True)
        self.hot_reload.sync_all(self, force=True)

    def build_system_prompt(self, role_override: Optional[str] = None) -> str:
        """Builds a KV-Cache-Aligned System Prompt:
        - STATIC IMMUTABLE PREFIX FIRST (`SOUL.md` + `Planning Protocol` + `Skill Catalog`)
          so LLM providers (OpenAI Automatic Prefix Cache / Anthropic Ephemeral Cache) cache 100% of the prefix.
        - DYNAMIC VOLATILE SUFFIX LAST (`MEMORY.md` + `TASK.md`) so updating `TASK.md` on each turn
          never busts the static prefix cache!
        """
        soul = self.memory.read_soul().strip()
        active_role = role_override or (self.role_policy.role_name if self.role_policy else None)
        if active_role:
            soul += (
                f"\n\n## Isolated Specialist Sub-Agent Role: {active_role}\n"
                "You are running inside a physically isolated role workspace. "
                "Your `MEMORY.md`, `TASK.md`, and `state.db` are strictly separated from other agents "
                "to prevent data and narrative pollution."
            )
            if self.role_policy:
                soul += (
                    f"\n- Role Purpose: {self.role_policy.description}"
                    f"\n- Allowed Skills: {self.role_policy.allowed_skills}"
                    f"\n- Forbidden Paths (Data Wall): {self.role_policy.denied_path_prefixes}"
                    f"\n- Read-Only Paths: {self.role_policy.readonly_path_prefixes}"
                )

        skill_catalog = self.skills.list_catalog_prompt()
        memory_text = self.memory.read_memory().strip()
        task_text = self.memory.read_task_checklist().strip()

        return (
            f"{soul}\n\n"
            "## Phase 1 & Phase 2 Recursive Planning Protocol\n"
            "1. Update `TASK.md` via `update_task_checklist` with concrete verification milestones.\n"
            "2. Check Available Skills and call `lookup_skills` before writing custom code.\n"
            "3. Emit independent tool calls in the same turn for parallel execution.\n"
            "4. For multi-agent workflows (such as `@quant-research-lead`), delegate to isolated specialist roles "
            "(`librarian`, `scout`, `sweeper`, `skeptic`) via `delegate_subagent` with a structured `handoff_payload`.\n"
            "5. Zero-Downtime Evolution: New tools in `plugins/*.py`, skills in `skills/*/SKILL.md`, "
            "and `.env` updates are synchronized via `IndustrialReloadSupervisor` without dropping state.\n\n"
            f"## Available On-Demand Skills (`SKILL.md` Catalog)\n{skill_catalog}\n\n"
            f"## Private Role Memory (`MEMORY.md`)\n{memory_text}\n\n"
            f"## Active Milestone Checklist (`TASK.md`)\n{task_text}\n"
        )

    @staticmethod
    def validate_message_alternation(messages: List[Dict[str, Any]]) -> bool:
        if not messages or messages[0].get("role") != "system":
            return False
        prev_role = "system"
        for msg in messages[1:]:
            role = msg.get("role")
            if role == prev_role and role != "tool":
                return False
            prev_role = role
        return True

    def estimate_tokens(self, messages: List[Dict[str, Any]]) -> int:
        return sum(len(json.dumps(m, ensure_ascii=False)) // 4 for m in messages)

    def compress_context_if_needed(
        self, messages: List[Dict[str, Any]], max_tokens: int
    ) -> List[Dict[str, Any]]:
        used = self.estimate_tokens(messages)
        if used <= int(max_tokens * self.config.compression_trigger_ratio):
            return messages
        protect_n = self.config.compression_protect_last_n
        if len(messages) <= protect_n + 2:
            return messages

        system_msg = messages[0]
        tail_start = max(1, len(messages) - protect_n)
        while tail_start > 1 and messages[tail_start].get("role") == "tool":
            tail_start -= 1

        middle = messages[1:tail_start]
        tail = messages[tail_start:]

        summary_bullets = []
        for m in middle:
            role = m.get("role")
            if role == "user":
                summary_bullets.append(f"- User requested: {str(m.get('content', ''))[:160]}")
            elif role == "assistant" and m.get("tool_calls"):
                names = [tc.get("function", {}).get("name", "") for tc in m["tool_calls"]]
                summary_bullets.append(f"- Assistant invoked tools: {', '.join(names)}")
            elif role == "tool":
                summary_bullets.append(
                    f"- Tool `{m.get('name', 'tool')}` returned: {str(m.get('content', ''))[:140]}"
                )

        compaction_msg = {
            "role": "user",
            "content": (
                "# Resuming from Context Compaction\n"
                "Earlier turns were summarized to preserve context budget:\n"
                + "\n".join(summary_bullets[-20:])
                + "\n\nContinue executing the remaining items in `TASK.md` and emit a complete response."
            ),
        }
        if tail and tail[0].get("role") == "user":
            tail = [
                {
                    "role": "user",
                    "content": compaction_msg["content"] + "\n\n" + str(tail[0].get("content", "")),
                }
            ] + tail[1:]
            return [system_msg] + tail

        return [system_msg, compaction_msg] + tail

    def run_task(
        self,
        user_prompt: str,
        session_id: Optional[str] = None,
        role_override: Optional[str] = None,
        max_turns_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self.execution_lock:
            self.hot_reload.sync_all(self, force=False)

            session_id = session_id or f"sess_{uuid.uuid4().hex[:10]}"
            max_turns = max_turns_override or (
                self.role_policy.max_turns if self.role_policy else self.config.max_turns
            )
            self.logger.info(
                "task_started",
                session_id=session_id,
                role=role_override or (self.role_policy.role_name if self.role_policy else "root"),
                prompt_preview=user_prompt[:120],
            )

            history = self.memory.load_session(session_id)
            if not history:
                history = [{"role": "system", "content": self.build_system_prompt(role_override)}]
            else:
                history[0] = {"role": "system", "content": self.build_system_prompt(role_override)}

            if history[-1].get("role") == "user":
                history[-1]["content"] += f"\n\n{user_prompt}"
            else:
                history.append({"role": "user", "content": user_prompt})

            total_tool_calls = 0
            learning_nudge_injected = False
            turns_executed = 0

            for turn in range(1, max_turns + 1):
                turns_executed = turn
                self.hot_reload.sync_all(self, force=False)
                history[0]["content"] = self.build_system_prompt(role_override)
                history = self.compress_context_if_needed(
                    history, self.config.primary_provider.max_context_tokens
                )

                assistant_msg = self._call_model_with_failover(
                    history, self.tools.get_openai_tool_schemas()
                )
                history.append(assistant_msg)
                self.memory.save_session(session_id, history, status="running")

                tool_calls = assistant_msg.get("tool_calls") or []
                if not tool_calls:
                    if (
                        total_tool_calls >= self.config.skill_learning_min_tool_calls
                        and not learning_nudge_injected
                        and self.recursion_depth == 0
                    ):
                        learning_nudge_injected = True
                        history.append(
                            {
                                "role": "user",
                                "content": (
                                    "[System Learning Loop Check]: You executed "
                                    f"{total_tool_calls} tool calls to solve this task. "
                                    "If this workflow is reusable on this server, call `save_skill` now "
                                    "to crystallize it; otherwise provide your final concise answer."
                                ),
                            }
                        )
                        continue

                    self.memory.save_session(session_id, history, status="completed")
                    self.logger.info(
                        "task_completed",
                        session_id=session_id,
                        turns_executed=turns_executed,
                        total_tool_calls=total_tool_calls,
                    )
                    return {
                        "session_id": session_id,
                        "role": self.role_policy.role_name if self.role_policy else "root_manager",
                        "isolated_workspace": str(self.config.workspace_root),
                        "status": "completed",
                        "turns_executed": turns_executed,
                        "total_tool_calls": total_tool_calls,
                        "final_response": assistant_msg.get("content", ""),
                        "task_checklist": self.memory.read_task_checklist(),
                    }

                total_tool_calls += len(tool_calls)
                tool_results = self.tools.execute_tool_calls(session_id, tool_calls)
                history.extend(tool_results)
                self.memory.save_session(session_id, history, status="running")

            self.memory.save_session(session_id, history, status="budget_exhausted")
            self.logger.warning(
                "task_budget_exhausted",
                session_id=session_id,
                turns_executed=turns_executed,
                total_tool_calls=total_tool_calls,
            )
            return {
                "session_id": session_id,
                "role": self.role_policy.role_name if self.role_policy else "root_manager",
                "isolated_workspace": str(self.config.workspace_root),
                "status": "budget_exhausted",
                "turns_executed": turns_executed,
                "total_tool_calls": total_tool_calls,
                "final_response": (
                    f"Iteration budget ({max_turns} turns) reached. "
                    "Current progress has been checkpointed to SQLite and TASK.md."
                ),
                "task_checklist": self.memory.read_task_checklist(),
            }

    def _spawn_child_agent(
        self,
        role: str,
        objective: str,
        max_turns: int,
        handoff_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self.recursion_depth >= self.config.max_recursion_depth:
            return {
                "status": "recursion_limit_reached",
                "max_recursion_depth": self.config.max_recursion_depth,
                "message": "Cannot spawn deeper sub-agent; execute remaining steps in current agent.",
            }

        clean_role = role.lower().strip().replace(" ", "_")
        role_policy = self.role_policies_registry.get(clean_role)
        role_ws = self.quant_layout.role_workspace(clean_role)

        child_primary_provider = dataclasses.replace(self.config.primary_provider)
        if role_policy and role_policy.model_override:
            child_primary_provider.model = role_policy.model_override

        child_config = dataclasses.replace(
            self.config,
            workspace_root=role_ws,
            primary_provider=child_primary_provider,
        )

        handoff_path: Optional[pathlib.Path] = None
        if handoff_payload is not None:
            packet_id = f"handoff_to_{clean_role}_{uuid.uuid4().hex[:6]}"
            handoff_path = self.quant_layout.write_handoff_packet(packet_id, handoff_payload)

        child = OuroborosManagerAgent(
            config=child_config,
            llm_callable=self.llm_callable,
            recursion_depth=self.recursion_depth + 1,
            approval_callback=self.tools.approval_callback,
            role_policy=role_policy,
            shared_root=self.shared_root,
            role_policies_registry=self.role_policies_registry,
            execution_lock=self.execution_lock,
        )

        full_objective = objective
        if handoff_payload is not None and handoff_path is not None:
            full_objective += (
                f"\n\n## Structured Handoff Packet (`{handoff_path.name}`)\n"
                f"```json\n{json.dumps(handoff_payload, indent=2, sort_keys=True)}\n```"
            )

        effective_turns = min(
            max_turns,
            role_policy.max_turns if role_policy else self.config.subagent_max_turns,
        )
        result = child.run_task(
            user_prompt=full_objective,
            session_id=f"sub_{clean_role}_{uuid.uuid4().hex[:6]}",
            role_override=clean_role,
            max_turns_override=effective_turns,
        )
        return {
            "subagent_role": clean_role,
            "isolated_workspace": str(role_ws),
            "enforced_policy": dataclasses.asdict(role_policy) if role_policy else None,
            "handoff_packet_path": str(handoff_path) if handoff_path else None,
            "status": result["status"],
            "turns_executed": result["turns_executed"],
            "summary": result["final_response"],
        }

    def _call_model_with_failover(
        self, messages: List[Dict[str, Any]], tool_schemas: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        providers = [self.config.primary_provider] + list(self.config.fallback_providers)
        last_error: Optional[Exception] = None
        for provider in providers:
            try:
                return self.llm_callable(messages, tool_schemas, provider)
            except Exception as exc:
                last_error = exc
                self.logger.warning("provider_call_failed", provider=provider.name, error=str(exc))
        raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")

    @staticmethod
    def _default_openai_compatible_call(
        messages: List[Dict[str, Any]],
        tool_schemas: List[Dict[str, Any]],
        provider: ModelProviderConfig,
    ) -> Dict[str, Any]:
        api_key = os.environ.get(provider.api_key_env, "")
        if not api_key:
            raise RuntimeError(
                f"Missing API key in environment variable {provider.api_key_env}. "
                "Set it in .env or pass a custom llm_callable for offline testing."
            )
        url = provider.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": provider.model,
            "messages": messages,
            "tools": tool_schemas,
            "tool_choice": "auto",
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=provider.timeout_seconds) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        choice = body["choices"][0]["message"]
        msg: Dict[str, Any] = {
            "role": "assistant",
            "content": choice.get("content") or "",
        }
        if choice.get("tool_calls"):
            msg["tool_calls"] = choice["tool_calls"]
        return msg
