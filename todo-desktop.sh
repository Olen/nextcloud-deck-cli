#!/bin/bash

# Get the current session ID
SESSION_ID=$(loginctl -j | jq -r '
  [ .[] | select(.user == "olen") ]
  | sort_by(.since)
  | .[0].session')
# SESSION_ID=$(loginctl show-res --value user "$UID" | head -n 1)

# Query the LockedHint property for the session
if [ "$SESSION_ID" ]; then
    IS_LOCKED=$(loginctl show-session "$SESSION_ID" -p LockedHint --value)
    if [ "$IS_LOCKED" == "yes" ]; then
        echo "Screen is locked" >> /tmp/yubikey-gpg-refresh.log
        exit 1
    else
        echo "Screen is unlocked" >> /tmp/yubikey-gpg-refresh.log
    fi
else
    echo "Could not determine session ID" >> /tmp/yubikey-gpg-refresh.log
    exit 1
fi

# Wait for GNOME session to settle — bail out if YubiKey services haven't finished
if ! systemctl --user is-active --quiet yubikey.service 2>/dev/null; then
    echo "yubikey.service not ready yet, skipping" >> /tmp/yubikey-gpg-refresh.log
    exit 0
fi

# Ensure the 1Password token is available before calling op
RUNTIME_TOKEN="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/op-connect-token"
if [[ ! -f "$RUNTIME_TOKEN" ]]; then
    echo "1Password token not yet decrypted, skipping" >> /tmp/yubikey-gpg-refresh.log
    exit 0
fi

PASSWORD=$(op --item=nextcloud-olen-deck-app-password --field=password)

/home/olen/prog/nextcloud-deck-cli/nextcloud-deck-list.py --url https://cloud.olen.net/ -u olen -p "$PASSWORD" -b 4 --pango
