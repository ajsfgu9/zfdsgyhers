#!/usr/bin/env bash
# Safe In-Place Upgrade & Permission Repair Script for Ouroboros Agent on Linux
# Solves `fatal: destination path already exists and is not an empty directory` when upgrading
# while preserving `.env`, `workspace_state/`, and custom `skills/`.

set -euo pipefail

TARGET_DIR="${1:-/opt/ouroboros-agent}"
WORKSPACE_DIR="${OUROBOROS_WORKSPACE:-/var/lib/ouroboros/workspace_state}"
SERVICE_NAME="${OUROBOROS_SERVICE:-ouroboros}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== Ouroboros In-Place Linux Server Upgrade ==="
echo "Source package : ${SOURCE_DIR}"
echo "Target install : ${TARGET_DIR}"

mkdir -p "${TARGET_DIR}"
mkdir -p "${WORKSPACE_DIR}"

# 1. Backup existing .env if present
TMP_BACKUP="$(mktemp -d)"
trap 'rm -rf "${TMP_BACKUP}"' EXIT

if [ -f "${TARGET_DIR}/.env" ]; then
    echo "[1/5] Backing up existing ${TARGET_DIR}/.env ..."
    cp -a "${TARGET_DIR}/.env" "${TMP_BACKUP}/.env.bak"
fi

# 2. Sync updated code into TARGET_DIR without wiping .env, workspace_state, or custom skills
if [ "${SOURCE_DIR}" != "$(realpath "${TARGET_DIR}")" ]; then
    echo "[2/5] Syncing updated code into ${TARGET_DIR} (preserving .env and workspace_state)..."
    if command -v rsync >/dev/null 2>&1; then
        rsync -av \
            --exclude='.env' \
            --exclude='workspace_state/' \
            --exclude='*.db' \
            --exclude='*.db-wal' \
            --exclude='*.db-shm' \
            --exclude='__pycache__/' \
            "${SOURCE_DIR}/" "${TARGET_DIR}/"
    else
        cp -rf "${SOURCE_DIR}/." "${TARGET_DIR}/"
    fi
else
    echo "[2/5] Already running inside ${TARGET_DIR}; checking git remote if configured..."
    if [ -d "${TARGET_DIR}/.git" ] && git -C "${TARGET_DIR}" remote get-url origin >/dev/null 2>&1; then
        git -C "${TARGET_DIR}" fetch --all --prune
        git -C "${TARGET_DIR}" stash push -m "pre-upgrade-auto-stash" || true
        git -C "${TARGET_DIR}" pull --rebase || true
    fi
fi

# Restore .env if backed up and ensure new Telegram variables exist
if [ -f "${TMP_BACKUP}/.env.bak" ]; then
    cp -a "${TMP_BACKUP}/.env.bak" "${TARGET_DIR}/.env"
elif [ ! -f "${TARGET_DIR}/.env" ] && [ -f "${TARGET_DIR}/.env.example" ]; then
    cp "${TARGET_DIR}/.env.example" "${TARGET_DIR}/.env"
fi

for var_name in OUROBOROS_TELEGRAM_BOT_TOKEN OUROBOROS_TELEGRAM_ALLOWED_CHAT_IDS OUROBOROS_TELEGRAM_WEBHOOK_SECRET OUROBOROS_TELEGRAM_POLL_TIMEOUT; do
    if [ -f "${TARGET_DIR}/.env" ] && ! grep -q "^${var_name}=" "${TARGET_DIR}/.env"; then
        echo "${var_name}=" >> "${TARGET_DIR}/.env"
        echo "  + Added ${var_name}= placeholder to ${TARGET_DIR}/.env"
    fi
done

# 3. Fix Linux user permissions on skills/ and workspace_state/ so agent can save/modify skills
echo "[3/5] Repairing directory ownership and write permissions for skills/ and workspace_state/..."
RUN_USER="$(whoami)"
if systemctl cat "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    SVC_USER="$(systemctl show -p User --value "${SERVICE_NAME}.service" || true)"
    if [ -n "${SVC_USER}" ] && id "${SVC_USER}" >/dev/null 2>&1; then
        RUN_USER="${SVC_USER}"
    fi
elif id "ouroboros" >/dev/null 2>&1; then
    RUN_USER="ouroboros"
fi

RUN_GROUP="$(id -gn "${RUN_USER}" 2>/dev/null || echo "${RUN_USER}")"
if [ "$(id -u)" -eq 0 ]; then
    chown -R "${RUN_USER}:${RUN_GROUP}" "${TARGET_DIR}/skills" "${WORKSPACE_DIR}"
    chmod -R u+rwX "${TARGET_DIR}/skills" "${WORKSPACE_DIR}"
    if [ -d "${TARGET_DIR}/workspace_state" ]; then
        chown -R "${RUN_USER}:${RUN_GROUP}" "${TARGET_DIR}/workspace_state"
        chmod -R u+rwX "${TARGET_DIR}/workspace_state"
    fi
else
    chmod -R u+rwX "${TARGET_DIR}/skills" 2>/dev/null || \
        echo "  ! Note: Run 'sudo chown -R ${RUN_USER}:${RUN_GROUP} ${TARGET_DIR}/skills ${WORKSPACE_DIR}' if owned by root."
fi

# 4. Run verification test suite
echo "[4/5] Running unit and governance verification suite..."
(cd "${TARGET_DIR}" && python3 -m unittest discover -s tests -v)

# 5. Restart running service if active
echo "[5/5] Checking running daemon status..."
if systemctl is-active --quiet "${SERVICE_NAME}.service" 2>/dev/null; then
    echo "Restarting systemd unit ${SERVICE_NAME}.service..."
    if [ "$(id -u)" -eq 0 ]; then
        systemctl daemon-reload
        systemctl restart "${SERVICE_NAME}.service"
        systemctl status "${SERVICE_NAME}.service" --no-pager -l
    else
        echo "Run: sudo systemctl daemon-reload && sudo systemctl restart ${SERVICE_NAME}.service"
    fi
elif command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ouroboros-agent$'; then
    echo "Rebuilding and restarting Docker container ouroboros-agent..."
    (cd "${TARGET_DIR}" && docker compose up -d --build)
else
    echo "Upgrade complete. Start daemon with:"
    echo "  cd ${TARGET_DIR} && python3 main.py --mode daemon --host 127.0.0.1 --port 8787"
fi