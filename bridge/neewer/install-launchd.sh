#!/usr/bin/env bash
# Install the Neewer bridge as a launchd User Agent so it starts on login.
#
# Run from anywhere — the script derives the repo's absolute path from its
# own location, substitutes it into the plist template, and loads the agent.
#
# Usage:
#     ./install-launchd.sh           # install + load
#     ./install-launchd.sh uninstall # unload + remove

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERTFORD_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PLIST_NAME="com.hertford.neewer-bridge.plist"
PLIST_SRC="${SCRIPT_DIR}/${PLIST_NAME}"
PLIST_DST="${HOME}/Library/LaunchAgents/${PLIST_NAME}"

case "${1:-install}" in
    install)
        if [ ! -x "${SCRIPT_DIR}/.venv/bin/python" ]; then
            echo "✗ ${SCRIPT_DIR}/.venv/bin/python missing — set up the venv first:"
            echo "    cd ${SCRIPT_DIR}"
            echo "    python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
            exit 1
        fi

        mkdir -p "${HOME}/Library/LaunchAgents"

        # If already loaded, unload first so re-runs pick up the new path/plist.
        if launchctl list | grep -q "com.hertford.neewer-bridge"; then
            launchctl unload "${PLIST_DST}" 2>/dev/null || true
        fi

        # Substitute the absolute path into the template.
        sed "s|@@HERTFORD_DIR@@|${HERTFORD_DIR}|g" "${PLIST_SRC}" > "${PLIST_DST}"

        launchctl load "${PLIST_DST}"

        echo "✓ installed and loaded ${PLIST_NAME}"
        echo "  logs:    ${SCRIPT_DIR}/launchd.{out,err}.log"
        echo "  status:  launchctl list | grep neewer"
        echo "  stop:    launchctl unload ${PLIST_DST}"
        ;;
    uninstall)
        if [ -f "${PLIST_DST}" ]; then
            launchctl unload "${PLIST_DST}" 2>/dev/null || true
            rm "${PLIST_DST}"
            echo "✓ uninstalled ${PLIST_NAME}"
        else
            echo "(nothing to uninstall — ${PLIST_DST} not present)"
        fi
        ;;
    *)
        echo "usage: $0 [install|uninstall]" >&2
        exit 2
        ;;
esac
