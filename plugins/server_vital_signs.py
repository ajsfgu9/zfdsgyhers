"""Example Hot-Pluggable Tool Plugin (`plugins/server_vital_signs.py`).

Drop any `.py` file into `plugins/` while Ouroboros is running — `HotReloadSupervisor`
detects it via file `mtime` and registers its tools live without restarting the daemon!
"""

from __future__ import annotations

import os
import platform
import time
from typing import Any, Dict

from ouroboros.tools import ServerToolRegistry, ToolSpec


def _get_vital_signs(args: Dict[str, Any]) -> Dict[str, Any]:
    load_avg = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    return {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "load_avg_1m_5m_15m": [round(x, 2) for x in load_avg],
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def register_plugins(registry: ServerToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="server_vital_signs",
            description="Return instant host vital signs (hostname, load average, platform) via a hot-loaded plugin.",
            parameters={"type": "object", "properties": {}},
            handler=_get_vital_signs,
            default_risk_tier="Tier 0",
        )
    )
