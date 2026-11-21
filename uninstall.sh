#!/usr/bin/env bash
set -Eeuo pipefail

systemctl --user disable --now ms10-rules.service 2>/dev/null || true
rm -f "$HOME/.local/bin/ms10-rules"
rm -f "$HOME/.local/share/applications/ms10-rules.desktop"
rm -f "$HOME/.config/systemd/user/ms10-rules.service"
systemctl --user daemon-reload

printf 'MS-10 Rules has been removed. Your profiles were preserved.\n'
printf 'System-wide udev and uinput configuration was left in place. Remove it manually if no longer needed.\n'
