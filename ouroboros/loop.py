"""Core Recursive Manager Loop combining Gemini Task, Hermes Agent, OpenClaw, and Grok Bot/Muse."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional

from ouroboros.config import AgentConfig, ModelProviderConfig
from ouroboros.memory import MemoryAndStateStore
from ouroboros.skills import SkillRegistry
from ouroboros.tools import ServerToolRegistry


LLMCallable = Callable[[List[Dict[str, Any]], List[Dict[str, Any]], ModelProviderConfig], Dict[str, Any]]


class OuroborosManagerAgent:
    """Recursive Autonomous Manager Agent with checklist planning, skill learning, and sub-agents."""

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        llm_callable: Optional[LLMCallable] = None,
        recursion_depth: int = 0,
        approval_callback: Optional[Callable[[str, Dict[str, Any], str], bool]] = None,
    ) -> None:
        self.config = config or AgentConfig()
        self.config.ensure_directories()
        self.recursion_depth = recursion_depth
        self.memory = MemoryAndStateStore(self.config.workspace_root)
        self.skills = SkillRegistry(self.config.skills_dir)
        self.llm_callable = llm_callable or self._default_openai_compatible_call
        self.tools = ServerToolRegistry(
            config=self.config,
            memory=self.memory,
            skills=self.skills,
            subagent_spawner=self._spawn_child_agent,
            approval_callback=approval_callback,
        )

    def build_system_prompt(self, role_override: Optional[str] = None) -> str:
        """Assembles the prompt from SOUL.md, MEMORY.md, TASK.md, and the live Skill Catalog."""
        soul = self.memory.read_soul().strip()
        if role_override:
            soul += f"\n\n## Specialist Sub-Agent Role: {role_override}\nFocus strictly on the delegated objective."
        memory_text = self.memory.read_memory().strip()
        task_text = self.memory.read_task_checklist().strip()
        skill_catalog = self.skills.list_catalog_prompt()

        return (
            f"{soul}\n\n"
            "## Phase 1 & Phase 2 Recursive Planning Protocol (Gemini Task Pattern)\n"
            "1. For any multi-step engineering or server management request, first update `TASK.md` "
            "via `update_task_checklist` with concrete verification milestones (`- [ ]`, `- [/]`, `- [x]`).\n"
            "2. Check the Available Skills below. If a skill matches your task, call `lookup_skills` "
            "before writing custom scripts.\n"
            "3. Emit independent tool calls in the same turn so the runtime executes them in parallel.\n"
            "4. When a complex workflow requires >=5 tool calls and reveals a reusable server pattern, "
            "crystallize it into a new skill via `save_skill` before finishing.\n\n"
            f"## Active Milestone Checklist (`TASK.md`)\n{task_text}\n\n"
            f"## Durable Server Memory (`MEMORY.md`)\n{memory_text}\n\n"
            f"## Available On-Demand Skills (`SKILL.md` Catalog)\n{skill_catalog}\n"
        )

    @staticmethod
    def validate_message_alternation(messages: List[Dict[str, Any]]) -> bool:
        """Enforces Hermes Agent strict role alternation invariant."""
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
        """Hermes/Gemini Task preflight context compaction (>50% window) preserving tool call/result pairs."""
        used = self.estimate_tokens(messages)
        if used <= int(max_tokens * self.config.compression_trigger_ratio):
            return messages
        protect_n = self.config.compression_protect_last_n
        if len(messages) <= protect_n + 2:
            return messages

        system_msg = messages[0]
        tail_start = max(1, len(messages) - protect_n)
        # Never split an assistant tool_call from its subsequent tool results
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
        # Ensure we don't put two 'user' messages in a row if tail[0] is also 'user'
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
        """Executes the recursive ReAct + Checklist loop until completion or iteration cap."""
        session_id = session_id or f"sess_{uuid.uuid4().hex[:10]}"
        max_turns = max_turns_override or self.config.max_turns

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
            # Refresh system prompt with latest TASK.md, MEMORY.md, and Skill Catalog
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
                # Hermes closed learning loop check: if >=5 tool calls were executed and no skill saved yet,
                # nudge the agent once to evaluate whether a skill should be persisted.
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
                return {
                    "session_id": session_id,
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
        return {
            "session_id": session_id,
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
        self, role: str, objective: str, max_turns: int
    ) -> Dict[str, Any]:
        """Spawns a bounded recursive sub-agent with isolated conversation history."""
        if self.recursion_depth >= self.config.max_recursion_depth:
            return {
                "status": "recursion_limit_reached",
                "max_recursion_depth": self.config.max_recursion_depth,
                "message": "Cannot spawn deeper sub-agent; execute remaining steps in current agent.",
            }
        child = OuroborosManagerAgent(
            config=self.config,
            llm_callable=self.llm_callable,
            recursion_depth=self.recursion_depth + 1,
        )
        result = child.run_task(
            user_prompt=objective,
            session_id=f"sub_{role.lower().replace(' ', '_')}_{uuid.uuid4().hex[:6]}",
            role_override=role,
            max_turns_override=min(max_turns, self.config.subagent_max_turns),
        )
        return {
            "subagent_role": role,
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
