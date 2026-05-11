#!/bin/bash
# Resolve HOME if invoked from a context where it's unset (systemd cron-like).
if [ -z "$HOME" ]; then
    HOME=$(getent passwd "$(id -u)" | cut -d: -f6)
fi
PATH=$HOME/.local/bin/:$PATH

set -euo pipefail

# 1Password item names — adjust to match what you have in your vault.
NC_OP_ITEM="${NC_OP_ITEM:-nextcloud-olen-deck-app-password}"
IMAP_OP_ITEM="${IMAP_OP_ITEM:-apollo-imap-olen}"

NEXTCLOUD_PASSWORD="$(op --item="$NC_OP_ITEM" --field=password)"
IMAP_PASSWORD="$(op --item="$IMAP_OP_ITEM" --field=password)"

exec /home/olen/prog/nextcloud-deck-cli/nextcloud-deck-imap-sync.py \
    --url "${NEXTCLOUD_BASE_URL:-https://cloud.olen.net/}" \
    --username "${NEXTCLOUD_USERNAME:-olen}" \
    --password "$NEXTCLOUD_PASSWORD" \
    --board-id "${NEXTCLOUD_BOARD_ID:-4}" \
    --imap-host "${IMAP_HOST:-localhost}" \
    --imap-port "${IMAP_PORT:-993}" \
    --imap-user "${IMAP_USER:-olen}" \
    --imap-password "$IMAP_PASSWORD" \
    --imap-folder "${IMAP_FOLDER:-_Virtual/Important}" \
    "$@"
