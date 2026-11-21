import importlib.util
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace

MODULE = Path(__file__).parents[1] / "src" / "ms10_rules.py"
spec = importlib.util.spec_from_file_location("ms10_rules", MODULE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class CoreTests(unittest.TestCase):
    def test_toggle_delay_blank_is_immediate(self):
        self.assertEqual(mod.normalize_hold_delay(""), 0)
        self.assertEqual(mod.normalize_hold_delay("   "), 0)

    def test_toggle_delay_accepts_integer_text(self):
        self.assertEqual(mod.normalize_hold_delay("5"), 5)
        self.assertEqual(mod.normalize_hold_delay("1"), 1)

    def test_toggle_delay_invalid_text_defaults_to_one_ms(self):
        self.assertEqual(mod.normalize_hold_delay("a"), 1)
        self.assertEqual(mod.normalize_hold_delay("5ms"), 1)
        self.assertEqual(mod.normalize_hold_delay("-5"), 1)

    def test_sniper_multiplier_range_and_fallback(self):
        self.assertEqual(mod.sniper_multiplier("0.25"), 0.25)
        self.assertEqual(mod.sniper_multiplier("1"), 1.0)
        self.assertEqual(mod.sniper_multiplier("invalid"), 0.25)
        self.assertEqual(mod.sniper_multiplier("0"), 0.25)
        self.assertEqual(mod.sniper_multiplier("2"), 0.25)

    def test_legacy_hold_rule_migrates_to_toggle(self):
        rule = {"button": "BTN_SIDE", "trigger": "hold", "hold_ms": "", "action": "disable", "value": ""}
        migrated = mod.normalize_rule(rule)
        self.assertEqual(migrated["trigger"], "toggle")
        self.assertEqual(migrated["hold_ms"], "")

    def test_legacy_default_profile_is_renamed_in_english(self):
        import json
        import tempfile
        from pathlib import Path
        old_dir, old_file = mod.CONFIG_DIR, mod.CONFIG_FILE
        try:
            root = Path(tempfile.mkdtemp())
            mod.CONFIG_DIR = root
            mod.CONFIG_FILE = root / "profiles.json"
            mod.CONFIG_FILE.write_text(json.dumps({"version": 1, "active_profile": "Padrão",
                "profiles": {"Padrão": {"rules": []}}}), encoding="utf-8")
            config = mod.load_config()
            self.assertEqual(config["active_profile"], "Default")
            self.assertIn("Default", config["profiles"])
        finally:
            mod.CONFIG_DIR, mod.CONFIG_FILE = old_dir, old_file

    def test_legacy_single_action_is_migrated(self):
        rule = {"button": "BTN_SIDE", "trigger": "press", "action": "command", "value": "notify-send test"}
        self.assertEqual(mod.get_rule_actions(rule), [{"action": "command", "value": "notify-send test"}])
        migrated = mod.normalize_rule(rule)
        self.assertEqual(migrated["actions"], [{"action": "command", "value": "notify-send test"}])
        self.assertNotIn("action", migrated)

    def test_multiple_actions_validate(self):
        rule = {"button": "BTN_SIDE", "trigger": "press", "actions": [
            {"action": "keyboard", "value": "CTRL+ALT+T"},
            {"action": "command", "value": "notify-send hello"},
        ]}
        mod.validate_rule(rule)

    def test_toggled_keyboard_action_is_released_on_next_toggle(self):
        daemon = mod.MouseDaemon()
        class FakeUInput:
            def __init__(self): self.events = []
            def write(self, *event): self.events.append(event)
            def syn(self): self.events.append(("SYN",))
        mouse, keyboard = FakeUInput(), FakeUInput()
        codes = SimpleNamespace(EV_KEY=1, EV_REL=2, BTN_LEFT=272,
                                 KEY_LEFTCTRL=29, KEY_LEFTALT=56, KEY_T=20)
        state = {"code": 275, "rule": {"actions": [{"action": "keyboard", "value": "CTRL+ALT+T"}]},
                 "mouse": mouse, "keyboard": keyboard, "ecodes": codes,
                 "timer": None, "active": False, "keys": [], "mouse_buttons": []}
        daemon._held[275] = state
        daemon._activate_toggle_state(state)
        self.assertEqual([event for event in keyboard.events if event != ("SYN",)],
                         [(1, 29, 1), (1, 56, 1), (1, 20, 1)])
        daemon._end_toggle(275)
        self.assertEqual([event for event in keyboard.events if event != ("SYN",)],
                         [(1, 29, 1), (1, 56, 1), (1, 20, 1),
                          (1, 20, 0), (1, 56, 0), (1, 29, 0)])

    def test_toggle_button_turns_on_then_off_with_two_presses(self):
        daemon = mod.MouseDaemon()
        class FakeUInput:
            def __init__(self): self.events = []
            def write(self, *event): self.events.append(event)
            def syn(self): self.events.append(("SYN",))
        mouse, keyboard = FakeUInput(), FakeUInput()
        codes = SimpleNamespace(EV_KEY=1, EV_REL=2, BTN_LEFT=272,
                                 KEY_LEFTCTRL=29, KEY_LEFTALT=56, KEY_T=20)
        rule = {"button": "BTN_SIDE", "trigger": "toggle", "hold_ms": "",
                "actions": [{"action": "keyboard", "value": "CTRL+ALT+T"}]}
        daemon._toggle_button(275, rule, mouse, keyboard, codes)
        self.assertIn(275, daemon._held)
        self.assertEqual([e for e in keyboard.events if e != ("SYN",)],
                         [(1, 29, 1), (1, 56, 1), (1, 20, 1)])
        daemon._toggle_button(275, rule, mouse, keyboard, codes)
        self.assertNotIn(275, daemon._held)
        self.assertEqual([e for e in keyboard.events if e != ("SYN",)],
                         [(1, 29, 1), (1, 56, 1), (1, 20, 1),
                          (1, 20, 0), (1, 56, 0), (1, 29, 0)])

    def test_duplicate_key_down_does_not_toggle_twice(self):
        daemon = mod.MouseDaemon()
        self.assertTrue(daemon._is_new_button_press(275, 1))
        self.assertFalse(daemon._is_new_button_press(275, 1))
        self.assertFalse(daemon._is_new_button_press(275, 2))
        self.assertFalse(daemon._is_new_button_press(275, 0))
        self.assertTrue(daemon._is_new_button_press(275, 1))

    def test_toggle_remains_active_after_delay_until_next_physical_click(self):
        daemon = mod.MouseDaemon()
        class FakeUInput:
            def __init__(self): self.events = []
            def write(self, *event): self.events.append(event)
            def syn(self): self.events.append(("SYN",))
        mouse, keyboard = FakeUInput(), FakeUInput()
        codes = SimpleNamespace(EV_KEY=1, EV_REL=2, BTN_LEFT=272, KEY_A=30)
        rule = {"button": "BTN_SIDE", "trigger": "toggle", "hold_ms": "5000",
                "actions": [{"action": "keyboard", "value": "A"}]}
        daemon._toggle_button(275, rule, mouse, keyboard, codes)
        state = daemon._held[275]
        daemon._activate_toggle_state(state)
        self.assertIn(275, daemon._held)
        self.assertTrue(state["active"])
        self.assertIn((1, 30, 1), keyboard.events)
        daemon._toggle_button(275, rule, mouse, keyboard, codes)
        self.assertIn(275, daemon._held)
        self.assertTrue(state["pending_off"])
        daemon._finish_toggle_off(state)
        self.assertNotIn(275, daemon._held)
        self.assertIn((1, 30, 0), keyboard.events)

    def test_releasing_before_hold_delay_cancels_activation(self):
        daemon = mod.MouseDaemon()
        class FakeUInput:
            def __init__(self): self.events = []
            def write(self, *event): self.events.append(event)
            def syn(self): self.events.append(("SYN",))
        mouse, keyboard = FakeUInput(), FakeUInput()
        codes = SimpleNamespace(EV_KEY=1, EV_REL=2, BTN_LEFT=272, KEY_A=30)
        rule = {"button": "BTN_SIDE", "trigger": "toggle", "hold_ms": "5000",
                "actions": [{"action": "keyboard", "value": "A"}]}
        self.assertTrue(daemon._is_new_button_press(275, 1))
        daemon._toggle_button(275, rule, mouse, keyboard, codes)
        self.assertIn(275, daemon._held)
        daemon._release_toggle_press(275)
        daemon._is_new_button_press(275, 0)
        self.assertNotIn(275, daemon._held)
        self.assertEqual(keyboard.events, [])

    def test_releasing_after_activation_keeps_toggle_on(self):
        daemon = mod.MouseDaemon()
        class FakeUInput:
            def __init__(self): self.events = []
            def write(self, *event): self.events.append(event)
            def syn(self): self.events.append(("SYN",))
        mouse, keyboard = FakeUInput(), FakeUInput()
        codes = SimpleNamespace(EV_KEY=1, EV_REL=2, BTN_LEFT=272, KEY_A=30)
        rule = {"button": "BTN_SIDE", "trigger": "toggle", "hold_ms": "5000",
                "actions": [{"action": "keyboard", "value": "A"}]}
        daemon._is_new_button_press(275, 1)
        daemon._toggle_button(275, rule, mouse, keyboard, codes)
        state = daemon._held[275]
        daemon._activate_toggle_state(state)
        daemon._release_toggle_press(275)
        daemon._is_new_button_press(275, 0)
        self.assertIn(275, daemon._held)
        self.assertTrue(state["active"])
        self.assertIn((1, 30, 1), keyboard.events)

    def test_releasing_second_hold_cancels_deactivation(self):
        daemon = mod.MouseDaemon()
        class FakeUInput:
            def __init__(self): self.events = []
            def write(self, *event): self.events.append(event)
            def syn(self): self.events.append(("SYN",))
        mouse, keyboard = FakeUInput(), FakeUInput()
        codes = SimpleNamespace(EV_KEY=1, EV_REL=2, BTN_LEFT=272, KEY_A=30)
        rule = {"button": "BTN_SIDE", "trigger": "toggle", "hold_ms": "5000",
                "actions": [{"action": "keyboard", "value": "A"}]}
        daemon._is_new_button_press(275, 1)
        daemon._toggle_button(275, rule, mouse, keyboard, codes)
        state = daemon._held[275]
        daemon._activate_toggle_state(state)
        daemon._is_new_button_press(275, 0)
        daemon._is_new_button_press(275, 1)
        daemon._toggle_button(275, rule, mouse, keyboard, codes)
        self.assertTrue(state["pending_off"])
        daemon._release_toggle_press(275)
        daemon._is_new_button_press(275, 0)
        self.assertIn(275, daemon._held)
        self.assertTrue(state["active"])
        self.assertNotIn((1, 30, 0), keyboard.events)

    def test_sniper_scales_motion_and_preserves_fractional_remainder(self):
        daemon = mod.MouseDaemon()
        daemon._sniper_active[275] = 0.25
        codes = SimpleNamespace(REL_X=0, REL_Y=1)
        outputs = [daemon._scale_relative_motion(codes.REL_X, 1, codes) for _ in range(4)]
        self.assertEqual(outputs, [0, 0, 0, 1])
        self.assertEqual(daemon._scale_relative_motion(codes.REL_Y, -8, codes), -2)

    def test_sniper_does_not_scale_when_inactive(self):
        daemon = mod.MouseDaemon()
        codes = SimpleNamespace(REL_X=0, REL_Y=1)
        self.assertEqual(daemon._scale_relative_motion(codes.REL_X, 7, codes), 7)

    def test_toggle_trigger_validates(self):
        mod.validate_rule({"button": "BTN_SIDE", "trigger": "toggle", "hold_ms": "5",
                           "actions": [{"action": "keyboard", "value": "CTRL"}]})

    def test_sniper_requires_toggle(self):
        rule = {"button": "BTN_SIDE", "trigger": "press",
                "actions": [{"action": "sniper", "value": "0.25"}]}
        with self.assertRaises(ValueError):
            mod.validate_rule(rule)

    def test_sniper_valid_with_toggle_trigger(self):
        mod.validate_rule({"button": "BTN_SIDE", "trigger": "toggle",
                           "actions": [{"action": "sniper", "value": "0.25"}]})

    def test_empty_action_list_is_rejected(self):
        with self.assertRaises(ValueError):
            mod.validate_rule({"button": "BTN_SIDE", "trigger": "press", "actions": []})

    def test_event_node_enumeration_falls_back_to_filesystem(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        (root / "event4").touch(); (root / "event9").touch()
        self.assertEqual(mod.enumerate_event_nodes(str(root), lambda: []),
                         [str(root / "event4"), str(root / "event9")])

    def test_event_node_enumeration_merges_evdev_and_filesystem(self):
        import tempfile
        root = Path(tempfile.mkdtemp()); (root / "event4").touch()
        self.assertEqual(mod.enumerate_event_nodes(str(root), lambda: ["/dev/input/event2"]),
                         ["/dev/input/event2", str(root / "event4")])

    def test_virtual_mouse_caps_excludes_non_mouse_hid_events(self):
        fake = SimpleNamespace(EV_KEY=1, EV_REL=2, EV_MSC=4, BTN_LEFT=272, BTN_RIGHT=273,
            BTN_MIDDLE=274, BTN_SIDE=275, BTN_EXTRA=276, BTN_FORWARD=277, BTN_BACK=278,
            BTN_TASK=0x117, REL_X=0, REL_Y=1, REL_WHEEL=8, REL_HWHEEL=6,
            REL_WHEEL_HI_RES=11, REL_HWHEEL_HI_RES=12)
        fake.BTN_0 = 256; fake.BTN_1 = 257
        result = mod.build_virtual_mouse_caps({1: [0, 272, 273, 274, 400], 2: [0, 1, 8], 4: [4]}, fake)
        self.assertEqual(set(result), {1, 2})
        self.assertNotIn(0, result[1]); self.assertNotIn(400, result[1])
        self.assertEqual(result[2], [0, 1, 6, 8, 11, 12])

    def test_virtual_keyboard_caps_contains_only_named_key_codes(self):
        fake = SimpleNamespace(EV_KEY=1, KEY={1: "KEY_ESC", 30: "KEY_A", 272: "BTN_LEFT", 0: "KEY_RESERVED"})
        self.assertEqual(mod.build_virtual_keyboard_caps(fake), {1: [1, 30]})

    def test_virtual_keyboard_caps_supports_name_to_code_maps(self):
        fake = SimpleNamespace(EV_KEY=1, KEY={"KEY_ESC": 1, "KEY_A": 30, "BTN_LEFT": 272, "KEY_RESERVED": 0})
        self.assertEqual(mod.build_virtual_keyboard_caps(fake), {1: [1, 30]})

    def test_virtual_keyboard_caps_uses_canonical_table(self):
        fake = SimpleNamespace(EV_KEY=1, KEY={}, ecodes={"KEY_ESC": 1, "KEY_A": 30, "BTN_LEFT": 272})
        self.assertEqual(mod.build_virtual_keyboard_caps(fake), {1: [1, 30]})

    def test_virtual_keyboard_caps_excludes_sentinels(self):
        fake = SimpleNamespace(EV_KEY=1, KEY_MAX=0x2FF,
            KEY={1: "KEY_ESC", 30: "KEY_A", 767: "KEY_MAX", 768: "KEY_CNT"},
            ecodes={"KEY_ESC": 1, "KEY_A": 30, "KEY_MAX": 767, "KEY_CNT": 768})
        result = mod.build_virtual_keyboard_caps(fake)
        self.assertEqual(result, {1: [1, 30]})
        self.assertTrue(all(0 < code < fake.KEY_MAX for code in result[1]))

    def test_middle_button_rule_is_compiled(self):
        rule = {"button": "BTN_MIDDLE", "trigger": "press", "action": "open",
                "value": "https://github.com", "enabled": True}
        config = {"active_profile": "Default", "profiles": {"Default": {"rules": [rule]}}}
        compiled = mod.compile_rule_map(config, SimpleNamespace(BTN_MIDDLE=274), logging.getLogger("test"))
        self.assertEqual(compiled[274]["actions"], [{"action": "open", "value": "https://github.com"}])

    def test_disabled_rule_is_not_compiled(self):
        rule = {"button": "BTN_SIDE", "trigger": "press", "action": "disable", "value": "", "enabled": False}
        config = {"active_profile": "Default", "profiles": {"Default": {"rules": [rule]}}}
        self.assertEqual(mod.compile_rule_map(config, SimpleNamespace(BTN_SIDE=275), logging.getLogger("test")), {})

    def test_device_usb_ids_use_ioctl_info(self):
        device = SimpleNamespace(info=SimpleNamespace(vendor=0x3151, product=0x3020), phys="usb-1")
        self.assertEqual(mod.input_device_usb_ids(device, "/dev/input/event9"), ("3151", "3020", "usb-1"))

    def test_device_usb_ids_fall_back_to_sysfs(self):
        device = SimpleNamespace(info=SimpleNamespace(vendor=0, product=0), phys="")
        original = mod.sysfs_usb_ids
        try:
            mod.sysfs_usb_ids = lambda _path: ("3151", "3020", "/sys/device")
            self.assertEqual(mod.input_device_usb_ids(device, "/dev/input/event9"), ("3151", "3020", "/sys/device"))
        finally:
            mod.sysfs_usb_ids = original

    def test_reject_unknown_button(self):
        with self.assertRaises(ValueError):
            mod.validate_rule({"button": "BTN_FAKE", "trigger": "press", "action": "disable", "value": ""})

    def test_scroll_directions(self):
        mod.validate_rule({"button": "BTN_SIDE", "trigger": "press", "action": "scroll", "value": "up"})
        with self.assertRaises(ValueError):
            mod.validate_rule({"button": "BTN_SIDE", "trigger": "press", "action": "scroll", "value": "diagonal"})

    def test_scroll_emits_high_resolution_and_legacy_events(self):
        codes = SimpleNamespace(EV_REL=2, REL_WHEEL=8, REL_HWHEEL=6,
                                REL_WHEEL_HI_RES=11, REL_HWHEEL_HI_RES=12)
        self.assertEqual(mod.scroll_event_values("up", codes), [(2, 11, 120), (2, 8, 1)])
        self.assertEqual(mod.scroll_event_values("down", codes), [(2, 11, -120), (2, 8, -1)])
        self.assertEqual(mod.scroll_event_values("left", codes), [(2, 12, -120), (2, 6, -1)])
        self.assertEqual(mod.scroll_event_values("right", codes), [(2, 12, 120), (2, 6, 1)])

    def test_execute_scroll_writes_events_and_syncs(self):
        daemon = mod.MouseDaemon()
        class FakeUInput:
            def __init__(self): self.events = []
            def write(self, *event): self.events.append(event)
            def syn(self): self.events.append(("SYN",))
        mouse = FakeUInput()
        codes = SimpleNamespace(EV_KEY=1, EV_REL=2, REL_WHEEL=8, REL_HWHEEL=6,
                                REL_WHEEL_HI_RES=11, REL_HWHEEL_HI_RES=12)
        daemon._execute_action({"action": "scroll", "value": "up"}, mouse, None, codes)
        self.assertEqual(mouse.events, [(2, 11, 120), (2, 8, 1), ("SYN",)])

    def test_receiver_options_hide_device_paths(self):
        devices = [{"name": "MS-10 Receiver", "path": "/dev/input/event4", "phys": "usb-1"}]
        labels = [option["label"] for option in mod.receiver_options(devices)]
        self.assertIn("MS-10 Receiver", labels)
        self.assertFalse(any("/dev/input" in label for label in labels))

    def test_no_receiver_has_a_single_neutral_option(self):
        self.assertEqual(mod.receiver_options([]), [{"phys": "", "label": "No receiver found"}])

    def test_receiver_selection_filters_by_stable_phys(self):
        devices = [
            {"name": "MS-10 A", "path": "/dev/input/event4", "phys": "usb-1"},
            {"name": "MS-10 B", "path": "/dev/input/event8", "phys": "usb-2"},
        ]
        self.assertEqual(mod.filter_selected_receiver(devices, "usb-2"), [devices[1]])
        self.assertEqual(mod.filter_selected_receiver(devices, ""), devices)
        self.assertEqual(mod.filter_selected_receiver(devices, "missing"), [])

    def test_receiver_options_include_auto_and_disconnected_saved_device(self):
        devices = [{"name": "MS-10", "path": "/dev/input/event4", "phys": "usb-1"}]
        options = mod.receiver_options(devices, "usb-old")
        self.assertEqual(options[0]["phys"], "")
        self.assertEqual(options[1]["phys"], "usb-1")
        self.assertEqual(options[2]["phys"], "usb-old")
        self.assertIn("offline", options[2]["label"])


    def test_while_holding_keyboard_output_releases_on_physical_release(self):
        daemon = mod.MouseDaemon()
        class FakeUInput:
            def __init__(self): self.events = []
            def write(self, *event): self.events.append(event)
            def syn(self): self.events.append(("SYN",))
        mouse, keyboard = FakeUInput(), FakeUInput()
        codes = SimpleNamespace(EV_KEY=1, EV_REL=2, BTN_LEFT=272, KEY_LEFTCTRL=29, KEY_A=30)
        rule = {"button": "BTN_SIDE", "trigger": "while_holding", "actions": [
            {"action": "keyboard", "value": "CTRL+A"}
        ]}
        daemon._start_while_holding(275, rule, mouse, keyboard, codes)
        self.assertIn(275, daemon._while_held)
        self.assertEqual([event for event in keyboard.events if event != ("SYN",)],
                         [(1, 29, 1), (1, 30, 1)])
        daemon._stop_while_holding(275)
        self.assertNotIn(275, daemon._while_held)
        self.assertEqual([event for event in keyboard.events if event != ("SYN",)],
                         [(1, 29, 1), (1, 30, 1), (1, 30, 0), (1, 29, 0)])
    def test_while_holding_does_not_allow_one_shot_actions(self):
        for action, value in (("command", "notify-send hi"), ("open", "firefox"), ("disable", "")):
            with self.subTest(action=action), self.assertRaises(ValueError):
                mod.validate_rule({"button": "BTN_SIDE", "trigger": "while_holding",
                                   "actions": [{"action": action, "value": value}]})
    def test_while_holding_scroll_repeats_until_release(self):
        from unittest.mock import patch
        daemon = mod.MouseDaemon()
        class FakeUInput:
            def __init__(self): self.events = []
            def write(self, *event): self.events.append(event)
            def syn(self): self.events.append(("SYN",))
        class FakeTimer:
            def __init__(self, _delay, callback, args=()):
                self.callback, self.args, self.started, self.cancelled = callback, args, False, False
            def start(self): self.started = True
            def cancel(self): self.cancelled = True
            def fire(self): self.callback(*self.args)
        mouse, keyboard = FakeUInput(), FakeUInput()
        codes = SimpleNamespace(EV_KEY=1, EV_REL=2, BTN_LEFT=272,
                                 REL_WHEEL=8, REL_HWHEEL=6)
        rule = {"button": "BTN_SIDE", "trigger": "while_holding", "actions": [
            {"action": "scroll", "value": "up"}
        ]}
        with patch.object(mod.threading, "Timer", FakeTimer):
            daemon._start_while_holding(275, rule, mouse, keyboard, codes)
            state = daemon._while_held[275]
            self.assertEqual(mouse.events.count((2, 8, 1)), 1)
            first_timer = state["scroll_timers"][0]
            first_timer.fire()
            self.assertEqual(mouse.events.count((2, 8, 1)), 2)
            next_timer = state["scroll_timers"][0]
            daemon._stop_while_holding(275)
            self.assertTrue(next_timer.cancelled)
            self.assertNotIn(275, daemon._while_held)
    def test_virtual_mouse_always_exposes_scroll_axes(self):
        fake = SimpleNamespace(EV_KEY=1, EV_REL=2, BTN_LEFT=272, BTN_RIGHT=273,
            BTN_MIDDLE=274, BTN_SIDE=275, BTN_EXTRA=276, BTN_FORWARD=277, BTN_BACK=278,
            BTN_TASK=0x117, REL_X=0, REL_Y=1, REL_WHEEL=8, REL_HWHEEL=6,
            REL_WHEEL_HI_RES=11, REL_HWHEEL_HI_RES=12)
        fake.BTN_0 = 256; fake.BTN_1 = 257
        result = mod.build_virtual_mouse_caps({1: [272], 2: [0, 1]}, fake)
        self.assertIn(8, result[2])
        self.assertIn(6, result[2])
        self.assertIn(11, result[2])
        self.assertIn(12, result[2])


if __name__ == "__main__":
    unittest.main()
