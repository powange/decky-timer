import os
import threading
import time

import main

CTRL = bytes([1, 0, 9] + [0] * 61)


def _report(*buttons):
    r = bytearray(CTRL)
    for b in buttons:
        byte, bit = main.DECK_BUTTONS[b]
        r[byte] |= 1 << bit
    return bytes(r)


def _feed(plugin, node, reports, delay=0.002):
    t = threading.Thread(target=plugin._read_controller, args=(node,), daemon=True)
    t.start()
    w = os.open(node, os.O_WRONLY)
    try:
        for r in reports:
            os.write(w, r)
            time.sleep(delay)
    except BrokenPipeError:
        pass
    finally:
        os.close(w)
    t.join(1)


def test_combo_needs_a_hold(plugin, tmp_path, monkeypatch):
    fired = []
    monkeypatch.setattr(main.Plugin, "_shortcut_triggered", lambda self: fired.append(1))
    plugin.settings["shortcut"] = "l5_r5"
    node = str(tmp_path / "hid")
    os.mkfifo(node)
    held = [_report("L5", "R5")] * 30          # 30 x 20 ms = 0.6 s
    brief = [_report("L5", "R5")] * 5          # 0.1 s
    _feed(plugin, node, [_report("L5")] * 30 + held + [CTRL] + brief + [CTRL], delay=0.02)
    assert fired == [1]


def test_keyboard_interface_is_ignored(plugin, tmp_path):
    plugin.settings["shortcut"] = "l5_r5"
    node = str(tmp_path / "kbd")
    os.mkfifo(node)
    _feed(plugin, node, [bytes(64)] * 30)
    assert node in plugin.ignored_nodes


def test_reader_stops_when_shortcut_off(plugin, tmp_path):
    plugin.settings["shortcut"] = "off"
    node = str(tmp_path / "hid")
    os.mkfifo(node)
    t = threading.Thread(target=plugin._read_controller, args=(node,), daemon=True)
    t.start()
    w = os.open(node, os.O_WRONLY)
    try:
        os.write(w, CTRL)
    except BrokenPipeError:
        pass
    t.join(1)
    os.close(w)
    assert not t.is_alive()
