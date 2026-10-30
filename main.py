#!/usr/bin/env python3
"""CLI & Server Daemon Entrypoint for Ouroboros Recursive Autonomous Manager Agent."""

from __future__ import annotations

import argparse
import json
import sys

from ouroboros.config import AgentConfig
from ouroboros.gateway import OuroborosGatewayServer, ScheduledRoutine
from ouroboros.loop import OuroborosManagerAgent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ouroboros: Recursive Autonomous Manager Agent for Dedicated Servers"
    )
    parser.add_argument(
        "--mode",
        choices=["task", "chat", "daemon"],
        default="task",
        help="Execution mode: single autonomous task, interactive REPL chat, or background gateway daemon.",
    )
    parser.add_argument("--prompt", help="Task prompt when running in --mode task.")
    parser.add_argument("--session-id", default=None, help="Optional session ID to resume.")
    parser.add_argument("--host", default="127.0.0.1", help="Gateway bind address.")
    parser.add_argument("--port", type=int, default=8787, help="Gateway bind port.")
    args = parser.parse_args()

    config = AgentConfig()
    agent = OuroborosManagerAgent(config=config)

    if args.mode == "task":
        if not args.prompt:
            parser.error("--prompt is required when --mode is 'task'")
        result = agent.run_task(user_prompt=args.prompt, session_id=args.session_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.mode == "chat":
        session_id = args.session_id or "interactive_cli"
        print(f"Ouroboros Interactive Manager Shell (session={session_id}). Type 'exit' to quit.")
        while True:
            try:
                user_in = input("ouroboros> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if user_in.lower() in ("exit", "quit"):
                break
            if not user_in:
                continue
            res = agent.run_task(user_prompt=user_in, session_id=session_id)
            print("\n" + str(res["final_response"]) + "\n")
        return 0

    routines = [
        ScheduledRoutine(
            name="hourly_self_heal_audit",
            interval_seconds=3600,
            prompt="Load the self-healer skill and verify disk usage and failed services.",
        )
    ]
    gateway = OuroborosGatewayServer(
        agent=agent, host=args.host, port=args.port, routines=routines
    )
    print(f"Ouroboros Gateway listening on http://{args.host}:{args.port}")
    gateway.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
