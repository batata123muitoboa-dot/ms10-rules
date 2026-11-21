#!/usr/bin/env python3
"""MS-10 Rules: rule-based remapper for the HMaston MS-10 2.4 GHz receiver."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import signal
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

APP_NAME = "MS-10 Rules"
APP_VERSION = "1.0.1"
VENDOR_ID = "3151"
PRODUCT_ID = "3020"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ms10-rules"
CONFIG_FILE = CONFIG_DIR / "profiles.json"

BUTTONS = {
    "BTN_LEFT": "Left",
    "BTN_RIGHT": "Right",
    "BTN_MIDDLE": "Middle click",
    "BTN_SIDE": "Side button 1",
    "BTN_EXTRA": "Side button 2",
    "BTN_FORWARD": "Forward",
    "BTN_BACK": "Back",
    "BTN_TASK": "Task",
}
for _i in range(16):
    BUTTONS[f"BTN_{_i}"] = f"Extra button {_i}"

TRIGGERS = {
    "press": "On press",
    "release": "On release",
    "while_holding": "While holding",
    "toggle": "Toggle",
}
ACTIONS = {
    "keyboard": "Keyboard shortcut",
    "mouse_button": "Mouse button",
    "scroll": "Scroll",
    "command": "Run command",
    "open": "Open application or URL",
    "disable": "Disable button",
    "sniper": "Sniper mode",
}
KEY_ALIASES = {
    "CTRL": "KEY_LEFTCTRL", "CONTROL": "KEY_LEFTCTRL", "ALT": "KEY_LEFTALT",
    "SHIFT": "KEY_LEFTSHIFT", "SUPER": "KEY_LEFTMETA", "META": "KEY_LEFTMETA",
    "ENTER": "KEY_ENTER", "ESC": "KEY_ESC", "SPACE": "KEY_SPACE",
    "TAB": "KEY_TAB", "BACKSPACE": "KEY_BACKSPACE", "DELETE": "KEY_DELETE",
    "UP": "KEY_UP", "DOWN": "KEY_DOWN", "LEFT": "KEY_LEFT", "RIGHT": "KEY_RIGHT",
}


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "active_profile": "Default",
        "device_phys": "",
        "profiles": {"Default": {"rules": []}},
    }


def load_config() -> dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        return default_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("profiles"), dict):
            raise ValueError("Invalid configuration format")
        if not data["profiles"]:
            data["profiles"] = {"Default": {"rules": []}}
        if data.get("active_profile") not in data["profiles"]:
            data["active_profile"] = next(iter(data["profiles"]))
        data.setdefault("device_phys", "")
        if "Padrão" in data["profiles"] and "Default" not in data["profiles"]:
            data["profiles"]["Default"] = data["profiles"].pop("Padrão")
            if data.get("active_profile") == "Padrão":
                data["active_profile"] = "Default"
        for profile in data["profiles"].values():
            if isinstance(profile, dict):
                profile["rules"] = [normalize_rule(rule) for rule in profile.get("rules", []) if isinstance(rule, dict)]
        return data
    except Exception as exc:
        raise RuntimeError(f"Could not read {CONFIG_FILE}: {exc}") from exc


def save_config(data: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


def normalize_hold_delay(value: Any) -> int:
    """Blank means activate immediately; malformed text falls back to 1 ms."""
    raw = str(value if value is not None else "").strip()
    if not raw:
        return 0
    try:
        delay = int(raw, 10)
    except (TypeError, ValueError):
        return 1
    if delay < 0:
        return 1
    return min(delay, 2_147_483_647)


def sniper_multiplier(value: Any) -> float:
    try:
        multiplier = float(str(value).strip())
    except (TypeError, ValueError):
        return 0.25
    if not 0.01 <= multiplier <= 1.0:
        return 0.25
    return multiplier


def get_rule_actions(rule: dict[str, Any]) -> list[dict[str, str]]:
    """Read the current multi-action format and migrate legacy single-action rules."""
    actions = rule.get("actions")
    if isinstance(actions, list):
        return [
            {"action": str(item.get("action", "")), "value": str(item.get("value", ""))}
            for item in actions if isinstance(item, dict)
        ]
    if rule.get("action"):
        return [{"action": str(rule.get("action", "")), "value": str(rule.get("value", ""))}]
    return []


def normalize_rule(rule: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(rule)
    normalized["actions"] = get_rule_actions(rule)
    normalized.pop("action", None)
    normalized.pop("value", None)
    # Older configs used "hold" for the toggle behavior exposed by the UI.
    if normalized.get("trigger") == "hold":
        normalized["trigger"] = "toggle"
    if normalized.get("trigger") == "toggle":
        raw_delay = str(normalized.get("hold_ms", "")).strip()
        normalized["hold_ms"] = str(normalize_hold_delay(raw_delay)) if raw_delay else ""
    return normalized


def validate_rule(rule: dict[str, Any]) -> None:
    if rule.get("button") not in BUTTONS:
        raise ValueError("Invalid source button")
    if rule.get("trigger") not in TRIGGERS:
        raise ValueError("Invalid trigger")
    actions = get_rule_actions(rule)
    if not actions:
        raise ValueError("Add at least one action")
    sniper_count = 0
    for item in actions:
        action = item.get("action")
        value = str(item.get("value", "")).strip()
        if action not in ACTIONS:
            raise ValueError("Invalid action")
        if action in ("keyboard", "mouse_button", "command", "open", "scroll") and not value:
            raise ValueError(f"{ACTIONS[action]} requires a value")
        if action == "mouse_button" and value not in BUTTONS:
            raise ValueError("Invalid destination button")
        if action == "scroll" and value not in ("up", "down", "left", "right"):
            raise ValueError("Invalid scroll direction")
        if action == "sniper":
            sniper_count += 1
            if rule.get("trigger") not in ("toggle", "while_holding", "hold"):
                raise ValueError("Sniper mode requires Toggle or While holding")
            sniper_multiplier(value)
        if rule.get("trigger") == "while_holding" and action in ("command", "open", "disable"):
            raise ValueError(f"{ACTIONS[action]} cannot be held; choose a reversible action")
    if sniper_count > 1:
        raise ValueError("A rule can contain only one Sniper mode action")


def sysfs_usb_ids(event_path: str) -> tuple[str | None, str | None, str]:
    """Walk the event node's sysfs ancestry; never infer device from hidraw index."""
    event_name = Path(event_path).name
    node = Path("/sys/class/input") / event_name / "device"
    try:
        resolved = node.resolve(strict=True)
    except OSError:
        return None, None, ""
    phys = ""
    try:
        # InputDevice.phys is more useful when present; sysfs path remains a stable fallback.
        phys = str(resolved)
    except Exception:
        pass
    for parent in (resolved, *resolved.parents):
        vendor = parent / "idVendor"
        product = parent / "idProduct"
        if vendor.exists() and product.exists():
            try:
                return vendor.read_text().strip().lower(), product.read_text().strip().lower(), phys
            except OSError:
                break
    return None, None, phys


def input_device_usb_ids(dev: Any, event_path: str) -> tuple[str | None, str | None, str]:
    """Prefer EVIOCGID (InputDevice.info); use sysfs only as a fallback."""
    info = getattr(dev, "info", None)
    vendor = getattr(info, "vendor", 0) if info is not None else 0
    product = getattr(info, "product", 0) if info is not None else 0
    phys = getattr(dev, "phys", "") or ""
    if vendor or product:
        return f"{int(vendor):04x}", f"{int(product):04x}", phys
    vid, pid, sys_path = sysfs_usb_ids(event_path)
    return vid, pid, phys or sys_path


def enumerate_event_nodes(input_dir: str = "/dev/input", list_devices_fn: Any = None) -> list[str]:
    """Enumerate event nodes even if python-evdev's convenience scanner returns nothing."""
    paths: set[str] = set()
    if list_devices_fn is not None:
        try:
            paths.update(str(path) for path in list_devices_fn())
        except Exception:
            # The direct filesystem scan below gives us a second independent path.
            pass
    root = Path(input_dir)
    try:
        paths.update(str(path) for path in root.glob("event*"))
    except OSError:
        pass
    # Some udev setups expose stable aliases even when the event directory scan is odd.
    for alias_dir in (root / "by-id", root / "by-path"):
        try:
            for alias in alias_dir.glob("*event*"):
                try:
                    target = str(alias.resolve(strict=True))
                    if Path(target).name.startswith("event"):
                        paths.add(target)
                except OSError:
                    continue
        except OSError:
            continue
    return sorted(paths)


def discover_mouse_devices(diagnostics: list[str] | None = None) -> list[dict[str, str]]:
    """Find the actual YICHIP pointer event node, not the keyboard HID interface."""
    try:
        from evdev import InputDevice, list_devices, ecodes
    except ImportError as exc:
        raise RuntimeError("Missing dependency: install python-evdev") from exc
    found: list[dict[str, str]] = []
    paths = enumerate_event_nodes(list_devices_fn=list_devices)
    if diagnostics is not None:
        input_dir = Path("/dev/input")
        try:
            entries = sorted(item.name for item in input_dir.iterdir()) if input_dir.exists() else []
        except OSError as exc:
            entries = [f"directory listing failed: {exc}"]
        try:
            sysfs_events = sorted(item.name for item in Path("/sys/class/input").glob("event*"))
        except OSError as exc:
            sysfs_events = [f"sysfs listing failed: {exc}"]
        diagnostics.append(
            f"UID={os.getuid()} EUID={os.geteuid()} grupos={os.getgroups()}; "
            f"/dev/input={'accessible' if os.access(input_dir, os.R_OK | os.X_OK) else 'permission denied'}, "
            f"entries={entries or 'none'}, sysfs={sysfs_events or 'no event* nodes'}, "
            f"evdev.list_devices()={len(list_devices())} node(s)"
        )
        if not paths and entries:
            diagnostics.append("Entries exist in /dev/input, but no event node was found by evdev or the filesystem")
    for path in paths:
        dev = None
        try:
            dev = InputDevice(path)
            vid, pid, sys_path = input_device_usb_ids(dev, path)
            caps = dev.capabilities(absinfo=False)
            keys = set(caps.get(ecodes.EV_KEY, []))
            rels = set(caps.get(ecodes.EV_REL, []))
            is_pointer = ecodes.BTN_LEFT in keys and ecodes.REL_X in rels and ecodes.REL_Y in rels
            if diagnostics is not None:
                diagnostics.append(
                    f"{path}: {dev.name!r}, ID={vid or '????'}:{pid or '????'}, "
                    f"pointer={'yes' if is_pointer else 'no'}, phys={dev.phys or sys_path}"
                )
            if vid == VENDOR_ID and pid == PRODUCT_ID and is_pointer:
                found.append({"path": path, "name": dev.name or "YICHIP mouse", "phys": dev.phys or sys_path})
        except OSError as exc:
            if diagnostics is not None:
                diagnostics.append(f"{path}: could not open: {exc} (readable={'yes' if os.access(path, os.R_OK) else 'no'})")
        finally:
            if dev is not None:
                try:
                    dev.close()
                except OSError:
                    pass
    return found


def filter_selected_receiver(devices: list[dict[str, str]], selected_phys: str) -> list[dict[str, str]]:
    """Apply the user's receiver selection; an empty value means automatic."""
    if not selected_phys:
        return list(devices)
    return [device for device in devices if device.get("phys", "") == selected_phys]


def receiver_options(devices: list[dict[str, str]], selected_phys: str = "") -> list[dict[str, str]]:
    """Build stable UI options and retain a saved receiver even while disconnected."""
    options = [{"phys": "", "label": "Automatic"}] if devices else []
    known = set()
    name_counts: dict[str, int] = {}
    for device in devices:
        phys = device.get("phys", "")
        if not phys or phys in known:
            continue
        known.add(phys)
        name = str(device.get("name") or "MS-10 receiver").strip()
        name_counts[name] = name_counts.get(name, 0) + 1
        options.append({"phys": phys, "label": name})
    duplicate_indices: dict[str, int] = {}
    for option in options[1:]:
        label = option["label"]
        if name_counts.get(label, 0) > 1:
            duplicate_indices[label] = duplicate_indices.get(label, 0) + 1
            option["label"] = f"{label} ({duplicate_indices[label]})"
    if selected_phys and selected_phys not in known:
        options.append({"phys": selected_phys, "label": "Saved receiver (offline)"})
    if not options:
        options.append({"phys": "", "label": "No receiver found"})
    return options


def scroll_event_values(direction: str, ecodes: Any) -> list[tuple[int, int, int]]:
    """Return legacy and high-resolution wheel events for one scroll step."""
    mapping = {
        "up": ("REL_WHEEL", "REL_WHEEL_HI_RES", 1),
        "down": ("REL_WHEEL", "REL_WHEEL_HI_RES", -1),
        "left": ("REL_HWHEEL", "REL_HWHEEL_HI_RES", -1),
        "right": ("REL_HWHEEL", "REL_HWHEEL_HI_RES", 1),
    }
    if direction not in mapping:
        raise ValueError("Invalid scroll direction")
    legacy_name, hires_name, amount = mapping[direction]
    events: list[tuple[int, int, int]] = []
    hires_code = getattr(ecodes, hires_name, None)
    legacy_code = getattr(ecodes, legacy_name, None)
    if isinstance(hires_code, int):
        events.append((ecodes.EV_REL, hires_code, amount * 120))
    if isinstance(legacy_code, int):
        events.append((ecodes.EV_REL, legacy_code, amount))
    if not events:
        raise RuntimeError("The virtual mouse does not expose scroll axes")
    return events


def build_virtual_mouse_caps(caps: dict[int, list[int]], ecodes: Any) -> dict[int, list[int]]:
    """Build a conservative uinput mouse; never copy vendor/system/MSC capabilities."""
    source_keys = set(caps.get(ecodes.EV_KEY, []))
    button_codes = {getattr(ecodes, name) for name in BUTTONS
                    if isinstance(getattr(ecodes, name, None), int)}
    # Keep only actual mouse buttons from the physical device, then add supported
    # remap destinations so a rule can emit a button absent from the hardware.
    mouse_keys = (source_keys & button_codes) | button_codes
    mouse_keys = {code for code in mouse_keys if isinstance(code, int) and code > 0}
    rel_codes = {code for code in caps.get(ecodes.EV_REL, [])
                 if isinstance(code, int) and code >= 0}
    # Scroll actions must be available even when the physical interface does not
    # advertise wheel axes (some receiver firmware exposes them inconsistently).
    for name in ("REL_WHEEL", "REL_HWHEEL", "REL_WHEEL_HI_RES", "REL_HWHEEL_HI_RES"):
        code = getattr(ecodes, name, None)
        if isinstance(code, int) and code >= 0:
            rel_codes.add(code)
    result: dict[int, list[int]] = {}
    if mouse_keys:
        result[ecodes.EV_KEY] = sorted(mouse_keys)
    if rel_codes:
        result[ecodes.EV_REL] = sorted(rel_codes)
    if not result.get(ecodes.EV_KEY) or not result.get(ecodes.EV_REL):
        raise RuntimeError("Insufficient capabilities to create the virtual mouse")
    return result


def build_virtual_keyboard_caps(ecodes: Any) -> dict[int, list[int]]:
    """Expose real KEY_* codes within Linux's EV_KEY range, excluding sentinels."""
    key_max = getattr(ecodes, "KEY_MAX", 0x2FF)
    if not isinstance(key_max, int) or key_max <= 1:
        key_max = 0x2FF
    codes: set[int] = set()

    def add_code(name: Any, code: Any) -> None:
        # KEY_MAX and KEY_CNT are table bounds, not actual keys. KEY_CNT is
        # KEY_MAX + 1 and makes UI_SET_KEYBIT fail with EINVAL on Linux.
        if (isinstance(name, str) and name.startswith("KEY_")
                and name not in {"KEY_MAX", "KEY_CNT"}
                and isinstance(code, int) and 0 < code < key_max):
            codes.add(code)

    key_map = getattr(ecodes, "KEY", {})
    if isinstance(key_map, dict):
        for key, value in key_map.items():
            # python-evdev versions expose KEY as either code->name or name->code.
            if isinstance(key, int):
                add_code(value, key)
            else:
                add_code(key, value)

    # The canonical ecodes table is name->code; it can include KEY_CNT/KEY_MAX.
    all_codes = getattr(ecodes, "ecodes", {})
    if isinstance(all_codes, dict):
        for name, value in all_codes.items():
            add_code(name, value)

    if not codes:
        raise RuntimeError("Could not determine valid keyboard key codes")
    return {ecodes.EV_KEY: sorted(codes)}


def supported_mouse_buttons() -> list[str]:
    """Return only buttons actually exposed by the detected MS-10 pointer node."""
    try:
        from evdev import InputDevice, ecodes
        devices = discover_mouse_devices()
        if len(devices) != 1:
            return list(BUTTONS)
        dev = InputDevice(devices[0]["path"])
        try:
            keys = set(dev.capabilities(absinfo=False).get(ecodes.EV_KEY, []))
            return [name for name in BUTTONS if getattr(ecodes, name, None) in keys]
        finally:
            dev.close()
    except Exception:
        return list(BUTTONS)


def compile_rule_map(config: dict[str, Any], ecodes: Any, log: Any) -> dict[int, dict[str, Any]]:
    profile = config["profiles"].get(config["active_profile"], {"rules": []})
    result: dict[int, dict[str, Any]] = {}
    for rule in profile.get("rules", []):
        try:
            validate_rule(rule)
            if not rule.get("enabled", True):
                continue
            code = getattr(ecodes, rule["button"], None)
            if not isinstance(code, int):
                raise ValueError(f"HID code is unavailable: {rule['button']}")
            result[code] = normalize_rule(rule)
        except Exception as exc:
            log.warning("Rule skipped: %s (%s)", rule, exc)
    return result


def keycode_from_name(name: str, ecodes: Any) -> int:
    normalized = name.strip().upper()
    if normalized in KEY_ALIASES:
        normalized = KEY_ALIASES[normalized]
    if not normalized.startswith("KEY_"):
        normalized = "KEY_" + normalized
    code = getattr(ecodes, normalized, None)
    if not isinstance(code, int):
        raise ValueError(f"Unknown key: {name}")
    return code


def parse_shortcut(value: str, ecodes: Any) -> list[int]:
    parts = [part.strip() for part in value.split("+") if part.strip()]
    if not parts:
        raise ValueError("Atalho vazio")
    return [keycode_from_name(part, ecodes) for part in parts]


class MouseDaemon:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.log = logging.getLogger("ms10-rules")
        self._toggle_lock = threading.RLock()
        self._held: dict[int, dict[str, Any]] = {}
        self._while_held: dict[int, dict[str, Any]] = {}
        self._pressed_buttons: set[int] = set()
        self._sniper_active: dict[int, float] = {}
        self._motion_remainder = {"x": 0.0, "y": 0.0}

    def stop(self, *_: Any) -> None:
        self.stop_event.set()

    def run(self) -> int:
        # Logs go to the systemd journal; this application does not create .log files.
        legacy_log = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "ms10-rules" / "daemon.log"
        try:
            legacy_log.unlink(missing_ok=True)
            legacy_log.parent.rmdir()
        except (OSError, TypeError):
            pass
        logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                            format="%(levelname)s %(message)s")
        try:
            from evdev import InputDevice, UInput, ecodes
        except ImportError:
            self.log.exception("python-evdev is not installed")
            return 2
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.log.info("Daemon started; searching for YICHIP receiver %s:%s", VENDOR_ID, PRODUCT_ID)
        while not self.stop_event.is_set():
            try:
                config = load_config()
                discovery_details: list[str] = []
                candidates = discover_mouse_devices(discovery_details)
                wanted = config.get("device_phys", "")
                candidates = filter_selected_receiver(candidates, wanted)
                if not candidates:
                    self.log.error("Compatible mouse not found. Event-node diagnostics: %s",
                                   " | ".join(discovery_details) or "no diagnostics available")
                    self.stop_event.wait(3)
                    continue
                if len(candidates) > 1:
                    self.log.error("Multiple compatible mice found; select one in the interface: %s", candidates)
                    self.stop_event.wait(5)
                    continue
                self._run_device(candidates[0], config, InputDevice, UInput, ecodes)
            except Exception:
                self.log.exception("Daemon error; retrying in 2 seconds")
                self.stop_event.wait(2)
        self.log.info("Daemon stopped")
        return 0

    def _begin_toggle(self, code: int, rule: dict[str, Any], mouse_ui: Any,
                     keyboard_ui: Any, ecodes: Any) -> None:
        state = {
            "code": code, "rule": rule, "mouse": mouse_ui, "keyboard": keyboard_ui,
            "ecodes": ecodes, "timer": None, "active": False,
            "pending": False, "pending_off": False,
            "keys": [], "mouse_buttons": [],
        }
        with self._toggle_lock:
            self._held[code] = state
            delay_ms = normalize_hold_delay(rule.get("hold_ms", ""))
            if delay_ms == 0:
                self._activate_toggle_state(state)
            else:
                state["pending"] = True
                timer = threading.Timer(delay_ms / 1000.0, self._activate_toggle_state, args=(state,))
                timer.daemon = True
                state["timer"] = timer
                timer.start()

    def _toggle_button(self, code: int, rule: dict[str, Any], mouse_ui: Any,
                       keyboard_ui: Any, ecodes: Any) -> None:
        """Start a hold-to-toggle cycle; a configured delay must elapse while held."""
        with self._toggle_lock:
            state = self._held.get(code)
            if state is None:
                self._begin_toggle(code, rule, mouse_ui, keyboard_ui, ecodes)
                return

            if not state["active"] or state.get("pending_off"):
                return

            delay_ms = normalize_hold_delay(rule.get("hold_ms", ""))
            if delay_ms == 0:
                self._end_toggle(code)
                return

            state["pending_off"] = True
            timer = threading.Timer(delay_ms / 1000.0, self._finish_toggle_off, args=(state,))
            timer.daemon = True
            state["timer"] = timer
            timer.start()

    def _release_toggle_press(self, code: int) -> None:
        """Cancel an unfinished hold when the physical button is released."""
        with self._toggle_lock:
            state = self._held.get(code)
            if state is None:
                return
            if state.get("pending") or state.get("pending_off"):
                timer = state.get("timer")
                if timer is not None:
                    timer.cancel()
                state["timer"] = None
                if state.get("pending"):
                    self._held.pop(code, None)
                    state["pending"] = False
                else:
                    state["pending_off"] = False

    def _finish_toggle_off(self, state: dict[str, Any]) -> None:
        with self._toggle_lock:
            code = state["code"]
            if self._held.get(code) is state and state.get("pending_off"):
                state["pending_off"] = False
                self._end_toggle(code)

    def _is_new_button_press(self, code: int, value: int) -> bool:
        """Ignore duplicate key-down/repeat packets until a real key-up arrives."""
        with self._toggle_lock:
            if value == 1:
                if code in self._pressed_buttons:
                    return False
                self._pressed_buttons.add(code)
                return True
            if value == 0:
                self._pressed_buttons.discard(code)
            return False

    def _activate_toggle_state(self, state: dict[str, Any]) -> None:
        with self._toggle_lock:
            code = state["code"]
            if self._held.get(code) is not state or state["active"] or self.stop_event.is_set():
                return
            state["pending"] = False
            state["active"] = True
            mouse_ui, keyboard_ui, ecodes = state["mouse"], state["keyboard"], state["ecodes"]
            try:
                for item in get_rule_actions(state["rule"]):
                    action = item["action"]
                    value = item.get("value", "").strip()
                    if action == "keyboard":
                        keys = parse_shortcut(value, ecodes)
                        for key in keys:
                            keyboard_ui.write(ecodes.EV_KEY, key, 1)
                            state["keys"].append(key)
                        keyboard_ui.syn()
                    elif action == "mouse_button":
                        button = getattr(ecodes, value)
                        mouse_ui.write(ecodes.EV_KEY, button, 1)
                        mouse_ui.syn()
                        state["mouse_buttons"].append(button)
                    elif action == "sniper":
                        self._sniper_active[code] = sniper_multiplier(value)
                        self._motion_remainder = {"x": 0.0, "y": 0.0}
                        self.log.info("Sniper mode enabled for %s at %.2fx", state["rule"].get("button"), self._sniper_active[code])
                    else:
                        self._execute_action(item, mouse_ui, keyboard_ui, ecodes, state["rule"])
            except Exception:
                self.log.exception("Could not activate toggle rule for %s", state["rule"].get("button"))
                self._held.pop(code, None)
                self._release_toggle_state(state)

    def _release_toggle_state(self, state: dict[str, Any]) -> None:
        timer = state.get("timer")
        if timer is not None:
            timer.cancel()
        mouse_ui, keyboard_ui, ecodes = state["mouse"], state["keyboard"], state["ecodes"]
        try:
            for key in reversed(state.get("keys", [])):
                keyboard_ui.write(ecodes.EV_KEY, key, 0)
            if state.get("keys"):
                keyboard_ui.syn()
            for button in state.get("mouse_buttons", []):
                mouse_ui.write(ecodes.EV_KEY, button, 0)
            if state.get("mouse_buttons"):
                mouse_ui.syn()
        except Exception:
            self.log.exception("Could not release held virtual inputs")
        if state.get("code") in self._sniper_active:
            self._sniper_active.pop(state["code"], None)
            self._motion_remainder = {"x": 0.0, "y": 0.0}
            self.log.info("Sniper mode disabled")

    def _end_toggle(self, code: int) -> None:
        with self._toggle_lock:
            state = self._held.pop(code, None)
            if state:
                self._release_toggle_state(state)

    def _start_while_holding(self, code: int, rule: dict[str, Any], mouse_ui: Any,
                             keyboard_ui: Any, ecodes: Any) -> None:
        """Activate reversible outputs for exactly the physical down interval."""
        with self._toggle_lock:
            if code in self._while_held:
                return
            state = {
                "code": code, "rule": rule, "mouse": mouse_ui, "keyboard": keyboard_ui,
                "ecodes": ecodes, "keys": [], "mouse_buttons": [], "scroll_timers": {},
            }
            self._while_held[code] = state
            try:
                for action_index, item in enumerate(get_rule_actions(rule)):
                    action, value = item["action"], str(item.get("value", "")).strip()
                    if action == "keyboard":
                        keys = parse_shortcut(value, ecodes)
                        for key in keys:
                            keyboard_ui.write(ecodes.EV_KEY, key, 1)
                            state["keys"].append(key)
                        keyboard_ui.syn()
                    elif action == "mouse_button":
                        button = getattr(ecodes, value)
                        mouse_ui.write(ecodes.EV_KEY, button, 1)
                        mouse_ui.syn()
                        state["mouse_buttons"].append(button)
                    elif action == "sniper":
                        self._sniper_active[code] = sniper_multiplier(value)
                        self._motion_remainder = {"x": 0.0, "y": 0.0}
                    elif action == "scroll":
                        self._execute_action(item, mouse_ui, keyboard_ui, ecodes, rule)
                        self._schedule_held_scroll(state, item, action_index)
            except Exception:
                self.log.exception("Could not activate While holding rule for %s", rule.get("button"))
                self._stop_while_holding(code)

    def _schedule_held_scroll(self, state: dict[str, Any], item: dict[str, Any], action_index: int) -> None:
        timer = threading.Timer(0.12, self._repeat_held_scroll, args=(state, item, action_index))
        timer.daemon = True
        state["scroll_timers"][action_index] = timer
        timer.start()

    def _repeat_held_scroll(self, state: dict[str, Any], item: dict[str, Any], action_index: int) -> None:
        with self._toggle_lock:
            code = state["code"]
            if self._while_held.get(code) is not state or self.stop_event.is_set():
                return
            state["scroll_timers"].pop(action_index, None)
            try:
                self._execute_action(item, state["mouse"], state["keyboard"], state["ecodes"], state["rule"])
                self._schedule_held_scroll(state, item, action_index)
            except Exception:
                self.log.exception("Could not repeat held scroll action")
                self._stop_while_holding(code)

    def _stop_while_holding(self, code: int) -> None:
        with self._toggle_lock:
            state = self._while_held.pop(code, None)
            if state is None:
                return
            for timer in state.get("scroll_timers", {}).values():
                timer.cancel()
            mouse_ui, keyboard_ui, ecodes = state["mouse"], state["keyboard"], state["ecodes"]
            try:
                for key in reversed(state.get("keys", [])):
                    keyboard_ui.write(ecodes.EV_KEY, key, 0)
                if state.get("keys"):
                    keyboard_ui.syn()
                for button in state.get("mouse_buttons", []):
                    mouse_ui.write(ecodes.EV_KEY, button, 0)
                if state.get("mouse_buttons"):
                    mouse_ui.syn()
            except Exception:
                self.log.exception("Could not release While holding outputs")
            if code in self._sniper_active:
                self._sniper_active.pop(code, None)
                self._motion_remainder = {"x": 0.0, "y": 0.0}

    def _release_all_toggles(self) -> None:
        with self._toggle_lock:
            while_states = list(self._while_held.keys())
            for code in while_states:
                self._stop_while_holding(code)
            states = list(self._held.values())
            self._held.clear()
            for state in states:
                self._release_toggle_state(state)

    def _execute_action(self, item: dict[str, Any], mouse_ui: Any,
                        keyboard_ui: Any, ecodes: Any,
                        rule: dict[str, Any] | None = None) -> None:
        action, value = item["action"], str(item.get("value", "")).strip()
        if action in ("disable", "sniper"):
            return
        if action == "mouse_button":
            code = getattr(ecodes, value)
            mouse_ui.write(ecodes.EV_KEY, code, 1); mouse_ui.syn()
            mouse_ui.write(ecodes.EV_KEY, code, 0); mouse_ui.syn()
        elif action == "scroll":
            for event_type, axis, amount in scroll_event_values(value, ecodes):
                mouse_ui.write(event_type, axis, amount)
            mouse_ui.syn()
        elif action == "keyboard":
            codes = parse_shortcut(value, ecodes)
            for code in codes:
                keyboard_ui.write(ecodes.EV_KEY, code, 1)
            keyboard_ui.syn()
            for code in reversed(codes):
                keyboard_ui.write(ecodes.EV_KEY, code, 0)
            keyboard_ui.syn()
        elif action == "command":
            argv = shlex.split(value)
            if argv:
                subprocess.Popen(argv, start_new_session=True, close_fds=True)
        elif action == "open":
            if value.startswith(("https://", "http://", "file://")) or os.path.exists(os.path.expanduser(value)):
                opener = shutil.which("xdg-open") or shutil.which("gio")
                if not opener:
                    raise RuntimeError("Neither xdg-open nor gio is available. Install xdg-utils.")
                command = [opener, value] if Path(opener).name == "xdg-open" else [opener, "open", value]
                subprocess.Popen(command, start_new_session=True, close_fds=True, env=os.environ.copy())
            else:
                argv = shlex.split(value)
                if argv:
                    subprocess.Popen(argv, start_new_session=True, close_fds=True, env=os.environ.copy())

    def _scale_relative_motion(self, code: int, value: int, ecodes: Any) -> int:
        if not self._sniper_active or code not in (ecodes.REL_X, ecodes.REL_Y):
            return value
        axis = "x" if code == ecodes.REL_X else "y"
        scale = min(self._sniper_active.values())
        scaled = value * scale + self._motion_remainder[axis]
        output = int(scaled)
        self._motion_remainder[axis] = scaled - output
        return output

    def _run_device(self, selected: dict[str, str], config: dict[str, Any], InputDevice: Any, UInput: Any, ecodes: Any) -> None:
        dev = InputDevice(selected["path"])
        mouse_ui = keyboard_ui = None
        grabbed = False
        try:
            caps = dev.capabilities(absinfo=False)
            keys = set(caps.get(ecodes.EV_KEY, []))
            rels = set(caps.get(ecodes.EV_REL, []))
            if not (ecodes.BTN_LEFT in keys and ecodes.REL_X in rels and ecodes.REL_Y in rels):
                raise RuntimeError("Selected device is not the mouse pointer interface")
            rules = compile_rule_map(config, ecodes, self.log)
            config_mtime = CONFIG_FILE.stat().st_mtime_ns if CONFIG_FILE.exists() else 0
            mouse_caps = build_virtual_mouse_caps(caps, ecodes)
            keyboard_caps = build_virtual_keyboard_caps(ecodes)
            mouse_key_codes = set(mouse_caps[ecodes.EV_KEY])
            keyboard_key_codes = set(keyboard_caps[ecodes.EV_KEY])
            rel_codes = set(mouse_caps[ecodes.EV_REL])
            mouse_ui = UInput(events=mouse_caps, name="MS-10 Rules Virtual Mouse")
            keyboard_ui = UInput(events=keyboard_caps, name="MS-10 Rules Virtual Keyboard")
            self.log.info("uinput devices created: mouse=%s; keyboard=%s; EV_REL=%s",
                          len(mouse_key_codes), len(keyboard_key_codes), sorted(rel_codes))
            dev.grab()
            grabbed = True
            middle_code = getattr(ecodes, "BTN_MIDDLE", -1)
            middle_rule = rules.get(middle_code)
            self.log.info("EVIOCGRAB confirmed: physical mouse events are now captured")
            self.log.info("Mouse captured: %s (%s); profile=%s; rules=%s; BTN_MIDDLE=%s",
                          selected["name"], selected["path"], config.get("active_profile", "?"),
                          len(rules), "configured" if middle_rule else "no rule")
            for event in dev.read_loop():
                if self.stop_event.is_set():
                    break
                try:
                    current_mtime = CONFIG_FILE.stat().st_mtime_ns if CONFIG_FILE.exists() else 0
                    if current_mtime != config_mtime:
                        fresh_config = load_config()
                        rules = compile_rule_map(fresh_config, ecodes, self.log)
                        config_mtime = current_mtime
                        middle_rule = rules.get(middle_code)
                        self.log.info("Configuration reloaded: profile=%s; rules=%s; BTN_MIDDLE=%s",
                                      fresh_config["active_profile"], len(rules),
                                      "configured" if middle_rule else "no rule")
                except Exception:
                    self.log.exception("Could not reload configuration; keeping previous rules")
                is_new_press = False
                if event.type == ecodes.EV_KEY:
                    if int(event.value) == 0:
                        self._release_toggle_press(int(event.code))
                        self._stop_while_holding(int(event.code))
                    is_new_press = self._is_new_button_press(int(event.code), int(event.value))
                rule = rules.get(int(event.code)) if event.type == ecodes.EV_KEY else None
                if rule:
                    if rule["trigger"] == "toggle":
                        if is_new_press:
                            self._toggle_button(int(event.code), rule, mouse_ui, keyboard_ui, ecodes)
                    elif rule["trigger"] == "while_holding":
                        if event.value == 1 and is_new_press:
                            self._start_while_holding(int(event.code), rule, mouse_ui, keyboard_ui, ecodes)
                        elif event.value == 0:
                            self._stop_while_holding(int(event.code))
                    elif event.value in (1, 0):
                        expected = "press" if event.value == 1 else "release"
                        if rule["trigger"] == expected:
                            for item in get_rule_actions(rule):
                                try:
                                    self._execute_action(item, mouse_ui, keyboard_ui, ecodes, rule)
                                except Exception:
                                    self.log.exception("Failed to execute action %s in rule %s",
                                                       item.get("action"), rule.get("id", "?"))
                    continue
                if event.type == ecodes.EV_SYN:
                    if event.code == ecodes.SYN_REPORT:
                        mouse_ui.syn(); keyboard_ui.syn()
                elif event.type == ecodes.EV_REL and event.code in rel_codes:
                    if event.code in (ecodes.REL_X, ecodes.REL_Y) and self._sniper_active:
                        output = self._scale_relative_motion(event.code, event.value, ecodes)
                        if output:
                            mouse_ui.write(ecodes.EV_REL, event.code, output)
                    else:
                        mouse_ui.write_event(event)
                elif event.type == ecodes.EV_KEY:
                    if event.code in mouse_key_codes:
                        mouse_ui.write_event(event)
                    elif event.code in keyboard_key_codes:
                        keyboard_ui.write_event(event)
        finally:
            self._release_all_toggles()
            with self._toggle_lock:
                self._pressed_buttons.clear()
            if grabbed:
                try:
                    dev.ungrab()
                except OSError:
                    pass
            if keyboard_ui:
                keyboard_ui.close()
            if mouse_ui:
                mouse_ui.close()
            dev.close()


# -------------------------------- GTK interface -------------------------------


def run_gui() -> int:
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk, Gio, GLib, Gdk
    except Exception as exc:
        print("Could not start the GTK4 interface. Install gtk4 and python-gobject.", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2

    def show_message(parent: Any, message: str) -> None:
        window = Gtk.Window(title="MS-10 Rules", transient_for=parent, modal=True)
        window.set_default_size(380, 150)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(20); box.set_margin_bottom(18); box.set_margin_start(20); box.set_margin_end(20)
        window.set_child(box)
        label = Gtk.Label(label=message, xalign=0, wrap=True)
        box.append(label)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.END)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: window.close())
        actions.append(close); box.append(actions)
        window.present()

    class RuleEditor(Gtk.Window):
        def __init__(self, parent: Any, rule: dict[str, Any] | None, on_save: Any):
            super().__init__(title="Edit rule" if rule else "New rule", transient_for=parent, modal=True)
            self.set_default_size(700, 650)
            self.set_resizable(True)
            self.on_save = on_save
            self.button_keys = supported_mouse_buttons()
            self.action_rows: list[tuple[Any, Any, Any]] = []

            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
            outer.set_margin_top(26); outer.set_margin_bottom(22)
            outer.set_margin_start(28); outer.set_margin_end(28)
            self.set_child(outer)

            heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            title = Gtk.Label(label="Edit assignment" if rule else "New assignment", xalign=0)
            title.add_css_class("dialog-title")
            heading.append(title)
            outer.append(heading)

            input_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            input_card.add_css_class("dialog-card")
            fields = Gtk.Grid()
            fields.set_column_spacing(16); fields.set_row_spacing(10)
            fields.set_column_homogeneous(False)
            self.button = Gtk.DropDown.new_from_strings([f"{BUTTONS[key]} ({key})" for key in self.button_keys])
            self.trigger = Gtk.DropDown.new_from_strings(list(TRIGGERS.values()))
            self.button.set_hexpand(True); self.trigger.set_hexpand(True)
            self._field(fields, "Mouse button", self.button, 0, 0)
            self._field(fields, "Trigger", self.trigger, 1, 0)
            input_card.append(fields)
            self.toggle_delay = Gtk.Entry()
            self.toggle_delay.set_placeholder_text("Leave blank for immediate activation")
            self.toggle_delay_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            self._field(self.toggle_delay_row, "Toggle activation time (ms)", self.toggle_delay)
            input_card.append(self.toggle_delay_row)
            self.trigger.connect("notify::selected", self._trigger_changed)
            outer.append(input_card)

            action_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            action_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            action_titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            action_title = Gtk.Label(label="Actions", xalign=0)
            action_title.add_css_class("section-title")
            action_titles.append(action_title)
            action_titles.set_hexpand(True)
            add_action = Gtk.Button(label="Add action")
            add_action.add_css_class("flat")
            add_action.add_css_class("compact-button")
            add_action.connect("clicked", lambda *_: self._add_action_row())
            action_header.append(action_titles); action_header.append(add_action)
            action_section.append(action_header)
            self.actions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            action_scroll = Gtk.ScrolledWindow()
            action_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            action_scroll.set_min_content_height(150); action_scroll.set_max_content_height(230)
            action_scroll.set_child(self.actions_box); action_scroll.set_vexpand(True)
            action_section.append(action_scroll)
            outer.append(action_section)

            options = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            options.add_css_class("dialog-card")
            self.enabled = Gtk.CheckButton(label="Enable this assignment")
            self.enabled.set_active(rule.get("enabled", True) if rule else True)
            options.append(self.enabled)
            outer.append(options)

            self.help = Gtk.Label(xalign=0, wrap=True)
            self.help.add_css_class("help-text")
            outer.append(self.help)

            footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            footer.set_halign(Gtk.Align.END)
            cancel = Gtk.Button(label="Cancel")
            cancel.add_css_class("compact-button")
            cancel.connect("clicked", lambda *_: self.close())
            save = Gtk.Button(label="Save assignment")
            save.add_css_class("compact-button")
            save.connect("clicked", self._save_clicked)
            footer.append(cancel); footer.append(save)
            outer.append(footer)

            if rule:
                if rule.get("button") in self.button_keys:
                    self.button.set_selected(self.button_keys.index(rule["button"]))
                self.trigger.set_selected(list(TRIGGERS).index(rule["trigger"]))
                self.enabled.set_active(rule.get("enabled", True))
                self.toggle_delay.set_text(str(rule.get("hold_ms", "")))
                for item in get_rule_actions(rule):
                    self._add_action_row(item)
            else:
                self._add_action_row({"action": "keyboard", "value": ""})
            self._trigger_changed()

        @staticmethod
        def _field(container: Any, label: str, widget: Any, column: int | None = None,
                   row: int | None = None) -> None:
            wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            title = Gtk.Label(label=label, xalign=0)
            title.add_css_class("field-label")
            wrapper.append(title); wrapper.append(widget)
            if column is None:
                container.append(wrapper)
            else:
                container.attach(wrapper, column, row or 0, 1, 1)

        def _trigger_changed(self, *_: Any) -> None:
            self.toggle_delay_row.set_visible(
                self.trigger.get_selected() < len(TRIGGERS)
                and list(TRIGGERS)[self.trigger.get_selected()] == "toggle"
            )
            self._update_help()

        def _add_action_row(self, item: dict[str, Any] | None = None) -> None:
            item = item or {"action": "keyboard", "value": ""}
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("action-row")
            action_keys = list(ACTIONS)
            action = Gtk.DropDown.new_from_strings(list(ACTIONS.values()))
            action.set_size_request(185, -1)
            value = Gtk.Entry()
            value.set_hexpand(True)
            clear = Gtk.Button(label="×")
            clear.add_css_class("flat")
            clear.add_css_class("icon-button")
            clear.set_tooltip_text("Clear value")
            clear.connect("clicked", lambda *_: value.set_text(""))
            remove = Gtk.Button(label="−")
            remove.add_css_class("flat")
            remove.add_css_class("icon-button")
            remove.set_tooltip_text("Remove this action")
            action.set_selected(action_keys.index(item.get("action", "keyboard")) if item.get("action") in action_keys else 0)
            value.set_text(item.get("value", ""))
            remove.connect("clicked", lambda *_: self._remove_action_row(row, action, value))
            action.connect("notify::selected", lambda *_: self._action_changed(action, value, clear_value=True))
            row.append(action)
            row.append(value)
            row.append(clear)
            row.append(remove)
            self.actions_box.append(row)
            self.action_rows.append((row, action, value))
            self._action_changed(action, value)
            self._sync_remove_buttons()

        def _action_changed(self, action: Any, value: Any, clear_value: bool = False) -> None:
            action_keys = list(ACTIONS)
            index = action.get_selected()
            key = action_keys[index] if index < len(action_keys) else ""
            if clear_value:
                value.set_text("")
            placeholders = {
                "keyboard": "CTRL+ALT+T",
                "mouse_button": "BTN_MIDDLE",
                "scroll": "up / down / left / right",
                "command": "notify-send 'Hello'",
                "open": "https://example.com or firefox",
                "disable": "No value required",
                "sniper": "0.25",
            }
            value.set_placeholder_text(placeholders.get(key, "Action parameter"))
            value.set_sensitive(key != "disable")
            if key == "sniper" and not value.get_text().strip():
                value.set_text("0.25")
            self._update_help()

        def _sync_remove_buttons(self) -> None:
            # The clear (×) control clears the entry; the separate minus button
            # removes a whole action. Either control remains available on one row.
            for row, _action, _value in self.action_rows:
                last = row.get_last_child()
                if last is not None:
                    last.set_sensitive(True)

        def _remove_action_row(self, row: Any, action: Any, value: Any) -> None:
            self.actions_box.remove(row)
            self.action_rows = [entry for entry in self.action_rows if entry[0] is not row]
            self._sync_remove_buttons()
            self._update_help()

        def _update_help(self, *_: Any) -> None:
            hints = {
                "keyboard": "Enter a shortcut such as CTRL+ALT+T or SUPER+E. Toggle turns the keys on with one click and releases them with the next.",
                "mouse_button": "Choose a destination button such as BTN_LEFT or BTN_MIDDLE. Toggle presses it on the first click and releases it on the next.",
                "scroll": "Direction: up, down, left or right.",
                "command": "Runs an executable and its arguments as your user. Shell operators are not interpreted.",
                "open": "Enter an application, file path or URL.",
                "disable": "Swallows the source button without sending an action.",
                "sniper": "Reduces pointer movement while active. Works with Toggle or While holding. Enter a multiplier from 0.01 to 1.00.",
            }
            selected = [list(ACTIONS)[entry[1].get_selected()] for entry in self.action_rows if entry[1].get_selected() < len(ACTIONS)]
            messages = [hints[key] for key in selected if key in hints]
            trigger_key = list(TRIGGERS)[self.trigger.get_selected()] if self.trigger.get_selected() < len(TRIGGERS) else ""
            if "sniper" in selected and trigger_key not in ("toggle", "while_holding"):
                messages.append("Sniper mode requires Toggle or While holding.")
            if trigger_key == "toggle":
                messages.append("Hold for the configured duration to toggle on or off. A short press cancels the timer.")
            elif trigger_key == "while_holding":
                messages.append("Actions stay active only while the physical button is down. Keyboard and mouse buttons release on key-up; scroll repeats until release.")
            self.help.set_text("\n".join(messages))

        def _save_clicked(self, *_: Any) -> None:
            button_index = self.button.get_selected()
            trigger_index = self.trigger.get_selected()
            if button_index >= len(self.button_keys) or trigger_index >= len(TRIGGERS):
                return
            actions = []
            action_keys = list(ACTIONS)
            for _row, action, value in self.action_rows:
                index = action.get_selected()
                if index < len(action_keys):
                    actions.append({"action": action_keys[index], "value": value.get_text().strip()})
            rule = {
                "id": str(uuid.uuid4()),
                "button": self.button_keys[button_index],
                "trigger": list(TRIGGERS)[trigger_index],
                "hold_ms": (str(normalize_hold_delay(self.toggle_delay.get_text()))
                            if self.toggle_delay.get_text().strip() else ""),
                "actions": actions,
                "enabled": self.enabled.get_active(),
            }
            try:
                validate_rule(rule)
                self.on_save(rule)
                self.close()
            except Exception as exc:
                self._show_error(str(exc))

        def _show_error(self, message: str) -> None:
            show_message(self, message)


    class ProfileEditor(Gtk.Window):
        def __init__(self, parent: Any, on_create: Any):
            super().__init__(title="New profile", transient_for=parent, modal=True)
            self.set_default_size(430, 220)
            self.on_create = on_create
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            box.set_margin_top(24); box.set_margin_bottom(22); box.set_margin_start(26); box.set_margin_end(26)
            self.set_child(box)
            title = Gtk.Label(label="Create profile", xalign=0)
            title.add_css_class("dialog-title")
            box.append(title)
            field_label = Gtk.Label(label="Profile name", xalign=0)
            field_label.add_css_class("field-label")
            box.append(field_label)
            self.entry = Gtk.Entry(); self.entry.set_placeholder_text("e.g. Gaming")
            box.append(self.entry)
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            actions.set_halign(Gtk.Align.END)
            cancel = Gtk.Button(label="Cancel"); cancel.add_css_class("compact-button")
            cancel.connect("clicked", lambda *_: self.close())
            create = Gtk.Button(label="Create profile"); create.add_css_class("compact-button")
            create.connect("clicked", self._create_clicked)
            actions.append(cancel); actions.append(create); box.append(actions)

        def _create_clicked(self, *_: Any) -> None:
            name = self.entry.get_text().strip()
            if not name:
                self.entry.grab_focus(); return
            try:
                self.on_create(name)
                self.close()
            except Exception as exc:
                show_message(self, str(exc))

    class MainWindow(Gtk.ApplicationWindow):
        def __init__(self, app: Any):
            super().__init__(application=app, title=APP_NAME)
            self.set_default_size(1040, 700)
            self.data = load_config()
            self.current_rules: list[dict[str, Any]] = []
            self._loading_profile = False
            self._loading_receiver = True
            self.receiver_options: list[dict[str, str]] = []
            self._build()
            self._load_profile()
            self.refresh_status()

        def _build(self) -> None:
            css = b"""
            window { font-family: Cantarell, sans-serif; }
            .muted { opacity: 0.72; font-size: 0.9em; }
            .sidebar { padding: 20px 16px; border-right: 1px solid rgba(128, 128, 128, 0.25); }
            .sidebar-heading { font-size: 1.05em; font-weight: 700; }
            .section-heading { font-size: 1.35em; font-weight: 700; }
            .field-label { font-size: 0.9em; font-weight: 600; }
            .table-heading { opacity: 0.65; font-size: 0.78em; font-weight: 700; }
            .assignment-row { padding: 9px 6px; }
            .rule-title { font-weight: 650; }
            .rule-subtitle { opacity: 0.72; font-size: 0.86em; }
            .rule-action { font-size: 0.92em; }
            .icon-button { min-width: 30px; min-height: 30px; padding: 3px; }
            .compact-button { min-height: 30px; padding: 3px 9px; }
            .dialog-card { padding: 14px; }
            .section-title { font-size: 1.1em; font-weight: 700; }
            .dialog-title { font-size: 1.35em; font-weight: 700; }
            .help-text { padding: 10px; opacity: 0.8; }
            .action-row { padding: 4px; }
            scrolledwindow { background: transparent; }
            """
            provider = Gtk.CssProvider()
            provider.load_from_string(css.decode("utf-8"))
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

            root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            self.set_child(root)
            shell = root
            sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=17)
            sidebar.add_css_class("sidebar")
            sidebar.set_size_request(224, -1)
            sidebar.set_margin_top(12); sidebar.set_margin_bottom(12)
            sidebar.set_margin_start(8); sidebar.set_margin_end(0)
            shell.append(sidebar)

            device_heading = Gtk.Label(label="MS-10 Wireless", xalign=0)
            device_heading.add_css_class("sidebar-heading")
            sidebar.append(device_heading)
            device_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            device_card.set_margin_top(4)
            receiver_label = Gtk.Label(label="Receiver", xalign=0); receiver_label.add_css_class("field-label")
            device_card.append(receiver_label)
            receiver_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            self.receiver = Gtk.DropDown.new_from_strings(["Scanning receivers…"])
            self.receiver.set_hexpand(True)
            self.receiver.connect("notify::selected", self._receiver_changed)
            receiver_row.append(self.receiver)
            refresh = Gtk.Button(label="↻")
            refresh.add_css_class("secondary-action")
            refresh.set_tooltip_text("Refresh connected receivers")
            refresh.connect("clicked", self._refresh_receivers)
            receiver_row.append(refresh)
            device_card.append(receiver_row)
            sidebar.append(device_card)

            profile_heading = Gtk.Label(label="Profile", xalign=0)
            profile_heading.add_css_class("field-label")
            sidebar.append(profile_heading)
            profile_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            profile_card.set_margin_top(4)
            self.profile = Gtk.DropDown.new_from_strings(list(self.data["profiles"].keys()))
            self.profile.connect("notify::selected", self._profile_changed)
            profile_card.append(self.profile)
            new_profile = Gtk.Button(label="Create profile")
            new_profile.add_css_class("flat")
            new_profile.add_css_class("compact-button")
            new_profile.connect("clicked", self._new_profile)
            profile_card.append(new_profile)
            sidebar.append(profile_card)

            service_heading = Gtk.Label(label="Service", xalign=0)
            service_heading.add_css_class("field-label")
            sidebar.append(service_heading)
            service_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=11)
            service_card.set_margin_top(4)
            status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
            
            self.status_dot = Gtk.Label(label="●")
            self.status_dot.add_css_class("status-dot")
            self.status_text = Gtk.Label(label="Checking service…", xalign=0, wrap=True)
            self.status_text.add_css_class("service-text")
            self.status_text.set_hexpand(True)
            status_row.append(self.status_dot); status_row.append(self.status_text)
            service_card.append(status_row)
            service_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.start_btn = Gtk.Button(label="Start")
            self.start_btn.add_css_class("compact-button")
            self.start_btn.set_hexpand(True)
            self.start_btn.connect("clicked", lambda *_: self.service("start"))
            self.stop_btn = Gtk.Button(label="Stop")
            self.stop_btn.add_css_class("compact-button")
            self.stop_btn.set_hexpand(True)
            self.stop_btn.connect("clicked", lambda *_: self.service("stop"))
            service_actions.append(self.start_btn); service_actions.append(self.stop_btn)
            service_card.append(service_actions)
            sidebar.append(service_card)

            sidebar_spacer = Gtk.Box(); sidebar_spacer.set_vexpand(True)
            sidebar.append(sidebar_spacer)
            sidebar_footer = Gtk.Label(label=f"Version {APP_VERSION}", xalign=0)
            sidebar_footer.add_css_class("muted")
            sidebar.append(sidebar_footer)

            main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            main.set_margin_top(26); main.set_margin_bottom(20)
            main.set_margin_start(28); main.set_margin_end(28)
            main.set_hexpand(True); main.set_vexpand(True)
            shell.append(main)

            page_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            page_titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            page_title = Gtk.Label(label="Button assignments", xalign=0)
            page_title.add_css_class("section-heading")
            page_titles.append(page_title)
            page_titles.set_hexpand(True)
            page_head.append(page_titles)
            self.rules_count = Gtk.Label(label="0")
            self.rules_count.add_css_class("muted")
            page_head.append(self.rules_count)
            add = Gtk.Button()
            add.set_child(Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6))
            add_content = add.get_child()
            add_content.append(Gtk.Image.new_from_icon_name("list-add-symbolic"))
            add_content.append(Gtk.Label(label="Add assignment"))
            add.add_css_class("flat")
            add.add_css_class("compact-button")
            add.set_tooltip_text("Create an assignment")
            add.connect("clicked", self._add_rule)
            page_head.append(add)
            main.append(page_head)

            self.rules_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_vexpand(True)
            scroller.set_child(self.rules_box)
            main.append(scroller)

            self._refresh_receivers()

        def _refresh_receivers(self, *_: Any) -> None:
            try:
                devices = discover_mouse_devices()
            except Exception:
                devices = []
            selected_phys = str(self.data.get("device_phys", ""))
            self.receiver_options = receiver_options(devices, selected_phys)
            self._loading_receiver = True
            self.receiver.set_model(Gtk.StringList.new([item["label"] for item in self.receiver_options]))
            selected_index = next(
                (index for index, item in enumerate(self.receiver_options) if item["phys"] == selected_phys), 0
            )
            self.receiver.set_selected(selected_index)
            self._loading_receiver = False
            self.receiver.set_sensitive(bool(devices) or bool(selected_phys))

        def _receiver_changed(self, *_: Any) -> None:
            if getattr(self, "_loading_receiver", True):
                return
            index = self.receiver.get_selected()
            if index >= len(self.receiver_options):
                return
            selected_phys = self.receiver_options[index]["phys"]
            if selected_phys == self.data.get("device_phys", ""):
                return
            self.data["device_phys"] = selected_phys
            save_config(self.data)
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "ms10-rules.service"],
                capture_output=True, text=True, timeout=3
            )
            if result.stdout.strip() == "active":
                self.service("restart")

        def _load_profile(self) -> None:
            name = self.data["active_profile"]
            names = list(self.data["profiles"].keys())
            self._loading_profile = True
            self.profile.set_selected(names.index(name) if name in names else 0)
            self._loading_profile = False
            self.current_rules = [normalize_rule(rule) for rule in self.data["profiles"][name].get("rules", [])]
            self._render_rules()

        def _save(self) -> None:
            name = self.data["active_profile"]
            self.data["profiles"].setdefault(name, {})["rules"] = self.current_rules
            save_config(self.data)

        def _profile_changed(self, *_: Any) -> None:
            if self._loading_profile: return
            names = list(self.data["profiles"].keys()); idx = self.profile.get_selected()
            if idx >= len(names): return
            self._save(); self.data["active_profile"] = names[idx]
            self._load_profile(); self._save()

        def _new_profile(self, *_: Any) -> None:
            dialog = ProfileEditor(self, self._create_profile)
            dialog.present()

        def _create_profile(self, name: str) -> None:
            if name in self.data["profiles"]:
                raise ValueError("A profile with that name already exists")
            self._save(); self.data["profiles"][name] = {"rules": []}; self.data["active_profile"] = name
            save_config(self.data)
            self._loading_profile = True
            self.profile.set_model(Gtk.StringList.new(list(self.data["profiles"].keys())))
            self._loading_profile = False
            self._load_profile()

        def _add_rule(self, *_: Any) -> None:
            RuleEditor(self, None, self._append_rule).present()

        def _append_rule(self, rule: dict[str, Any]) -> None:
            self.current_rules.append(rule); self._save(); self._render_rules()

        def _edit_rule(self, rule: dict[str, Any]) -> None:
            RuleEditor(self, rule, lambda updated: self._replace_rule(rule, updated)).present()

        def _replace_rule(self, original: dict[str, Any], updated: dict[str, Any]) -> None:
            idx = self.current_rules.index(original); self.current_rules[idx] = updated
            self._save(); self._render_rules()

        def _render_rules(self) -> None:
            child = self.rules_box.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                self.rules_box.remove(child)
                child = nxt
            count = len(self.current_rules)
            self.rules_count.set_text(f"{count} assignment" if count == 1 else f"{count} assignments")
            if not self.current_rules:
                empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                empty.add_css_class("empty-state")
                empty.set_margin_top(28)
                empty.set_margin_bottom(28)
                title = Gtk.Label(label="No assignments", xalign=0)
                title.add_css_class("empty-title")
                hint = Gtk.Label(
                    label="Add an assignment to change what a mouse button does.",
                    xalign=0, wrap=True
                )
                hint.add_css_class("muted")
                empty.append(title)
                empty.append(hint)
                self.rules_box.append(empty)
                return

            headings = Gtk.Grid()
            headings.set_column_spacing(16)
            headings.set_margin_start(12)
            headings.set_margin_end(12)
            headings.set_margin_top(4)
            headings.set_margin_bottom(7)
            for column, label_text in enumerate(("BUTTON", "TRIGGER", "ACTION", "ACTIVE", "", "")):
                label = Gtk.Label(label=label_text, xalign=0)
                label.add_css_class("table-heading")
                headings.attach(label, column, 0, 1, 1)
            headings.get_child_at(0, 0).set_size_request(110, -1)
            headings.get_child_at(1, 0).set_size_request(120, -1)
            headings.get_child_at(2, 0).set_hexpand(True)
            headings.get_child_at(3, 0).set_size_request(55, -1)
            self.rules_box.append(headings)

            for rule in self.current_rules:
                row = Gtk.Grid()
                row.add_css_class("assignment-row")
                row.set_column_spacing(16)
                row.set_margin_start(2)
                row.set_margin_end(2)
                if not rule.get("enabled", True):
                    row.add_css_class("dim-label")

                button_cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                button_name = Gtk.Label(
                    label=BUTTONS.get(rule.get("button"), rule.get("button", "Unknown button")),
                    xalign=0
                )
                button_name.add_css_class("rule-title")
                button_code = Gtk.Label(label=rule.get("button", ""), xalign=0)
                button_code.add_css_class("rule-subtitle")
                button_cell.append(button_name)
                button_cell.append(button_code)
                button_cell.set_size_request(110, -1)
                row.attach(button_cell, 0, 0, 1, 1)

                trigger_name = TRIGGERS.get(rule.get("trigger"), rule.get("trigger", "Unknown trigger"))
                if rule.get("trigger") == "toggle":
                    raw_delay = str(rule.get("hold_ms", "")).strip()
                    trigger_name += f" ({normalize_hold_delay(raw_delay)} ms)" if raw_delay else " (immediate)"
                trigger = Gtk.Label(label=trigger_name, xalign=0)
                trigger.add_css_class("rule-subtitle")
                trigger.set_size_request(120, -1)
                trigger.set_ellipsize(3)
                row.attach(trigger, 1, 0, 1, 1)

                action_items = get_rule_actions(rule)
                action_names = [ACTIONS.get(item.get("action"), item.get("action", "Unknown")) for item in action_items]
                action_label = Gtk.Label(label=", ".join(action_names) if action_names else "No action", xalign=0)
                action_label.add_css_class("rule-action")
                action_label.set_ellipsize(3)
                action_label.set_hexpand(True)
                row.attach(action_label, 2, 0, 1, 1)

                enabled = Gtk.Switch()
                enabled.set_active(rule.get("enabled", True))
                enabled.set_valign(Gtk.Align.CENTER)
                enabled.set_halign(Gtk.Align.CENTER)
                enabled.set_tooltip_text("Enable or disable this assignment")
                enabled.connect("notify::active", lambda sw, _p, r=rule: self._toggle_rule(r, sw.get_active()))
                row.attach(enabled, 3, 0, 1, 1)

                edit = Gtk.Button(label="Edit")
                edit.add_css_class("secondary-action")
                edit.connect("clicked", lambda *_args, r=rule: self._edit_rule(r))
                row.attach(edit, 4, 0, 1, 1)
                delete = Gtk.Button()
                delete.set_child(Gtk.Image.new_from_icon_name("user-trash-symbolic"))
                delete.add_css_class("flat")
                delete.add_css_class("icon-button")
                delete.set_tooltip_text("Remove assignment")
                delete.connect("clicked", lambda *_args, r=rule: self._delete_rule(r))
                row.attach(delete, 5, 0, 1, 1)
                self.rules_box.append(row)
                if rule is not self.current_rules[-1]:
                    divider = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
                    self.rules_box.append(divider)

        def _toggle_rule(self, rule: dict[str, Any], enabled: bool) -> None:
            rule["enabled"] = enabled; self._save()

        def _delete_rule(self, rule: dict[str, Any]) -> None:
            if rule in self.current_rules:
                self.current_rules.remove(rule); self._save(); self._render_rules()

        def _message(self, message: str) -> None:
            show_message(self, message)

        def service(self, action: str) -> None:
            try:
                if action == "start":
                    session_vars = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                                    "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE", "GDK_BACKEND", "XAUTHORITY")
                    present = [name for name in session_vars if os.environ.get(name)]
                    if present:
                        subprocess.run(["systemctl", "--user", "import-environment", *present],
                                       check=False, capture_output=True, text=True, timeout=5)
                    if present and shutil.which("dbus-update-activation-environment"):
                        subprocess.run(["dbus-update-activation-environment", "--systemd", *present],
                                       check=False, capture_output=True, text=True, timeout=5)
                    systemd_action = "restart"
                else:
                    systemd_action = action
                result = subprocess.run(["systemctl", "--user", systemd_action, "ms10-rules.service"],
                                        capture_output=True, text=True, timeout=12)
                if result.returncode:
                    self._message((result.stderr or result.stdout or "Could not control the service").strip())
                GLib.timeout_add(700, self.refresh_status)
            except Exception as exc:
                self._message(str(exc))

        def refresh_status(self) -> bool:
            try:
                result = subprocess.run(["systemctl", "--user", "is-active", "ms10-rules.service"],
                                        capture_output=True, text=True, timeout=2)
                active = result.stdout.strip() == "active"
                self.status_text.set_text("Service active" if active else "Service stopped")
                self.status_dot.set_markup('<span foreground="#62c58b">●</span>' if active else '<span foreground="#e09a45">●</span>')
                self.start_btn.set_sensitive(not active); self.stop_btn.set_sensitive(active)
            except Exception:
                self.status_text.set_text("Could not query the user systemd service")
            return False

    class SplashWindow(Gtk.ApplicationWindow):
        def __init__(self, app: Any):
            super().__init__(application=app)
            self.set_title(APP_NAME)
            self.set_default_size(560, 286)
            self.set_resizable(False)
            self.set_decorated(False)
            self.add_css_class("splash-window")

            splash_css = Gtk.CssProvider()
            splash_css.load_from_string("""
                window.splash-window { background: #17191d; color: #eceff2; border: 1px solid #343940; border-radius: 16px; }
                .splash-title { color: #f1f2f3; font-size: 27px; font-weight: 700; }
                .splash-caption { color: #a5adb6; font-size: 13px; }
                .splash-status { color: #858f99; font-size: 11px; }
                .splash-progress trough { min-height: 3px; background: #30363d; border-radius: 3px; }
                .splash-progress progress { min-height: 3px; background: #8fa99d; border-radius: 3px; }
            """)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), splash_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
            outer.set_margin_top(30)
            outer.set_margin_bottom(24)
            outer.set_margin_start(32)
            outer.set_margin_end(32)
            self.set_child(outer)

            center = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
            center.set_valign(Gtk.Align.CENTER)
            center.set_vexpand(True)
            copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            copy.set_valign(Gtk.Align.CENTER)
            title = Gtk.Label(label="MS-10 Rules", xalign=0)
            title.add_css_class("splash-title")
            caption = Gtk.Label(label="Button mapping and profiles", xalign=0)
            caption.add_css_class("splash-caption")
            copy.append(title)
            copy.append(caption)
            copy.set_hexpand(True)
            center.append(copy)

            art = Gtk.DrawingArea()
            art.set_content_width(176)
            art.set_content_height(142)
            art.set_draw_func(self._draw_signal_art)
            center.append(art)
            outer.append(center)

            separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            outer.append(separator)
            bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
            status = Gtk.Label(label="Opening application", xalign=0)
            status.add_css_class("splash-status")
            bottom.append(status)
            self.progress = Gtk.ProgressBar()
            self.progress.add_css_class("splash-progress")
            self.progress.set_pulse_step(0.14)
            bottom.append(self.progress)
            outer.append(bottom)
            self._pulse_source = GLib.timeout_add(85, self._pulse)

        @staticmethod
        def _draw_signal_art(_area: Any, cr: Any, width: int, height: int, _data: Any = None) -> None:
            cx, cy = width / 2.0, height / 2.0
            cr.set_line_width(1.15)
            for radius, start, end in ((25, 0.35, 5.6), (43, 0.1, 5.3), (61, 0.65, 5.95)):
                cr.set_source_rgba(0.68, 0.75, 0.79, 0.19)
                cr.arc(cx, cy, radius, start, end)
                cr.stroke()
            cr.set_source_rgba(0.68, 0.75, 0.79, 0.22)
            cr.move_to(cx - 76, cy + 1)
            cr.curve_to(cx - 47, cy - 19, cx - 26, cy + 21, cx, cy)
            cr.curve_to(cx + 27, cy - 21, cx + 48, cy + 19, cx + 76, cy - 1)
            cr.stroke()
            cr.set_source_rgba(0.68, 0.79, 0.72, 0.95)
            cr.arc(cx + 43, cy - 43, 3.2, 0, 6.283185307)
            cr.fill()
            cr.set_source_rgba(0.83, 0.86, 0.88, 0.82)
            cr.arc(cx - 49, cy + 33, 2.0, 0, 6.283185307)
            cr.fill()

        def _pulse(self) -> bool:
            if not self.get_visible():
                return GLib.SOURCE_REMOVE
            self.progress.pulse()
            return GLib.SOURCE_CONTINUE

        def stop_pulse(self) -> None:
            if self._pulse_source:
                GLib.source_remove(self._pulse_source)
                self._pulse_source = 0

    class App(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="dev.rainnxd.MS10Rules", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
            self._splash: SplashWindow | None = None
            self._main_window: MainWindow | None = None

        def do_activate(self):
            if self._main_window is not None:
                self._main_window.present()
                return
            if self._splash is not None:
                self._splash.present()
                return
            self._splash = SplashWindow(self)
            self._splash.present()
            GLib.timeout_add(620, self._finish_startup)

        def _finish_startup(self) -> bool:
            self._main_window = MainWindow(self)
            if self._splash is not None:
                self._splash.stop_pulse()
                self._splash.close()
                self._splash = None
            self._main_window.present()
            return GLib.SOURCE_REMOVE

    return App().run(None)


def detach_gui_from_terminal() -> None:
    """Keep the GTK window independent from an interactive terminal session."""
    if os.environ.get("GIO_LAUNCHED_DESKTOP_FILE"):
        return
    if not (sys.stdin.isatty() or sys.stdout.isatty() or sys.stderr.isatty()):
        return
    try:
        pid = os.fork()
    except OSError:
        return
    if pid > 0:
        os._exit(0)
    os.setsid()
    null_fd = os.open(os.devnull, os.O_RDWR)
    for stream_fd in (0, 1, 2):
        os.dup2(null_fd, stream_fd)
    if null_fd > 2:
        os.close(null_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    parser.add_argument("command", nargs="?", choices=("gui", "daemon", "detect", "check"), default="gui")
    args = parser.parse_args()
    if args.command == "daemon":
        return MouseDaemon().run()
    if args.command == "detect":
        try:
            details: list[str] = []
            devices = discover_mouse_devices(details)
            if not devices:
                print("No YICHIP 3151:3020 pointer interface was found.")
                for detail in details:
                    print(" - " + detail)
                print("If access is denied, run ./install.sh again to apply device ACLs. Do not run the daemon as root.")
                return 1
            for device in devices:
                print(f"{device['name']} | {device['path']} | {device['phys']}")
            return 0
        except Exception as exc:
            print(exc, file=sys.stderr); return 2
    if args.command == "check":
        errors = []
        try:
            import evdev  # noqa: F401
        except Exception: errors.append("python-evdev")
        try:
            import gi
            gi.require_version("Gtk", "4.0")
        except Exception: errors.append("GTK4 + python-gobject")
        if errors:
            print("Missing dependencies: " + ", ".join(errors)); return 1
        print("Python and GTK dependencies are available.")
        return 0
    detach_gui_from_terminal()
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
