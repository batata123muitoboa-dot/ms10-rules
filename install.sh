#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP="MS-10 Rules"
readonly ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly BIN_DIR="$HOME/.local/bin"
readonly UNIT_DIR="$HOME/.config/systemd/user"
readonly DATA_DIR="$HOME/.config/ms10-rules"
readonly UDEV_RULE="/etc/udev/rules.d/72-ms10-rules.rules"
readonly MODULE_CONFIG="/etc/modules-load.d/ms10-rules.conf"
readonly PACKAGES=(python-evdev gtk4 python-gobject xdg-utils acl)
readonly COMMANDS=(pacman sudo systemctl udevadm modprobe install setfacl readlink python)

error() { printf 'Error: %s\n' "$*" >&2; exit 1; }

[[ "$EUID" -ne 0 ]] || error "Run this installer as your regular user, without sudo."

for command in "${COMMANDS[@]}"; do
    command -v "$command" >/dev/null 2>&1 || error "Required command not found: $command"
done

missing=()
while IFS= read -r package; do
    [[ -n "$package" ]] && missing+=("$package")
done < <(pacman -T "${PACKAGES[@]}" 2>/dev/null || true)

if ((${#missing[@]})); then
    printf 'Missing packages: %s\n' "${missing[*]}"
    printf 'Install them with: sudo pacman -S %s\n' "${missing[*]}"
    exit 2
fi

python -c 'import evdev, gi; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk' \
    >/dev/null 2>&1 || error "The installed Python packages could not be imported. Check python-evdev, gtk4 and python-gobject."

install -Dm755 "$ROOT/src/ms10_rules.py" "$BIN_DIR/ms10-rules"
install -Dm644 "$ROOT/share/applications/ms10-rules.desktop" "$HOME/.local/share/applications/ms10-rules.desktop"
install -Dm644 "$ROOT/systemd/ms10-rules.service" "$UNIT_DIR/ms10-rules.service"
install -d "$DATA_DIR"

sudo rm -f /etc/udev/rules.d/99-ms10-rules.rules
sudo install -Dm644 "$ROOT/udev/72-ms10-rules.rules" "$UDEV_RULE" 
module_config="$(mktemp)"
printf 'uinput\n' > "$module_config"
sudo install -Dm644 "$module_config" "$MODULE_CONFIG"
rm -f "$module_config"
sudo modprobe uinput
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input --action=change
sudo udevadm trigger --subsystem-match=misc --action=change
sudo udevadm settle

user_name="$(id -un)"
matched_nodes=0
for node in /dev/input/event*; do
    [[ -e "$node" ]] || continue
    sys_path="$(readlink -f "/sys/class/input/${node##*/}/device" 2>/dev/null || true)"
    while [[ -n "$sys_path" && "$sys_path" != / ]]; do
        if [[ -r "$sys_path/idVendor" && -r "$sys_path/idProduct" ]]; then
            vendor="$(<"$sys_path/idVendor")"
            product="$(<"$sys_path/idProduct")"
            if [[ "${vendor,,}" == 3151 && "${product,,}" == 3020 ]]; then
                sudo setfacl -m "u:${user_name}:rw" "$node"
                ((matched_nodes += 1))
            fi
            break
        fi
        sys_path="$(dirname -- "$sys_path")"
    done
done

[[ ! -e /dev/uinput ]] || sudo setfacl -m "u:${user_name}:rw" /dev/uinput

session_vars=(DISPLAY WAYLAND_DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS XDG_CURRENT_DESKTOP XDG_SESSION_TYPE GDK_BACKEND XAUTHORITY)
present_vars=()
for name in "${session_vars[@]}"; do
    [[ -n "${!name:-}" ]] && present_vars+=("$name")
done
if ((${#present_vars[@]})); then
    systemctl --user import-environment "${present_vars[@]}" || true
    if command -v dbus-update-activation-environment >/dev/null 2>&1; then
        dbus-update-activation-environment --systemd "${present_vars[@]}" || true
    fi
fi

systemctl --user daemon-reload
systemctl --user enable ms10-rules.service

# Remove the legacy file log; future daemon output goes to the systemd journal.
legacy_state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/ms10-rules"
rm -f "$legacy_state_dir/daemon.log"
rmdir "$legacy_state_dir" 2>/dev/null || true
"$BIN_DIR/ms10-rules" check
systemctl --user restart ms10-rules.service

printf '%s installed successfully.\n' "$APP"
printf 'Service: %s\n' "$(systemctl --user is-active ms10-rules.service || true)"
printf 'YICHIP event nodes granted access: %s\n' "$matched_nodes"
printf 'Launch from the application menu or run: ms10-rules\n'
