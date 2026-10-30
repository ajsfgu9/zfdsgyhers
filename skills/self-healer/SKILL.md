---
name: self-healer
description: Diagnose and remediate failing systemd services, Docker containers, disk pressure, and port conflicts on a dedicated Linux server.
enabled: true
---

# Dedicated Server Self-Healer

## Steps
1. **Inspect Service & Resource Health**:
   - Run `df -h`, `free -m`, and `systemctl --failed` (or `docker ps -a`) via `host_bash` in parallel.
2. **Isolate Root Cause**:
   - Inspect the last 100 lines of logs (`journalctl -u <service> -n 100 --no-pager` or `docker logs --tail 100 <container>`).
3. **Remediate Safely**:
   - For Tier-1 service restarts, restart the unit and verify `/healthz` or process state.
   - For Tier-2 firewall, SSH, or destructive disk cleanup outside `/tmp`, halt and request human sign-off.
