import asyncio
import json
import os
import time

import pytest

import main


def run(coro):
    return asyncio.run(coro)


def test_timer_lifecycle(plugin):
    s = run(plugin.start_timer("Pâtes", 600))
    tid = s["timers"][0]["id"]
    run(plugin.pause_timer(tid))
    assert plugin._find_timer(tid)["paused"]
    run(plugin.resume_timer(tid))
    assert 590 < plugin._find_timer(tid)["end_at"] - time.time() <= 600
    run(plugin.stop_timer(tid))
    assert plugin.timers == []


def test_alarm_cap_and_mute(plugin, monkeypatch):
    calls = []
    monkeypatch.setattr(main.Plugin, "_play_alarm", lambda self: calls.append(1))
    now = time.time()
    plugin.timers = [
        {"id": "old", "name": "o", "duration": 60, "end_at": now - 600, "paused": False},
        {"id": "new", "name": "n", "duration": 60, "end_at": now - 5, "paused": False},
    ]
    plugin._alarm_tick(now)
    assert calls == [1] and "old" not in plugin.alarm_last
    run(plugin.mute_alarm("new"))
    calls.clear()
    plugin.alarm_last = {}
    plugin._alarm_tick(time.time())
    assert calls == []
    run(plugin.restart_timer("new"))
    assert plugin._find_timer("new")["alarm_muted"] is False


def test_add_time_and_snooze(plugin):
    tid = run(plugin.start_timer("A", 10))["timers"][0]["id"]
    t = plugin._find_timer(tid)
    t.update(end_at=time.time() - 30, notified=True, alarm_muted=True)
    run(plugin.add_time(tid, 60))
    assert 25 < t["end_at"] - time.time() < 35 and not t["notified"] and not t["alarm_muted"]
    run(plugin.snooze_timer(tid, 300))
    assert 295 < t["end_at"] - time.time() <= 300


def test_save_preset_returns_id_and_cleans_name(plugin):
    s = run(plugin.save_preset("", "  x" * 40, 90))
    assert s["saved_id"] == s["presets"][-1]["id"]
    assert len(s["presets"][-1]["name"]) == main.NAME_MAX


def test_damaged_state_is_repaired(plugin):
    with open(plugin.state_path, "w") as f:
        json.dump({
            "timers": [{"id": "a", "duration": 60, "paused": False}, {"id": "b", "duration": 30, "paused": True},
                       {"name": "no id"}, "junk"],
            "presets": [{"id": "p", "name": "P", "duration": 90}, {"bad": 1}],
            "settings": {"opacity": "very", "position": "top-left"},
        }, f)
    plugin._load()
    assert [t["id"] for t in plugin.timers] == ["a", "b"]
    assert plugin.timers[0]["end_at"] and plugin.timers[1]["remaining"] == 30.0
    assert len(plugin.presets) == 1
    assert plugin.settings["position"] == "top-left"
    assert plugin.settings["opacity"] == main.DEFAULT_SETTINGS["opacity"]


def test_settings_whitelist():
    v = main._valid_settings({"opacity": 0.5, "display": ":0; rm -rf /", "size": "huge",
                              "notify": "yes", "unknown": 1, "shortcut": "l4_r4"})
    assert v == {"opacity": 0.5, "shortcut": "l4_r4"}


def test_api_bounds(plugin):
    t = run(plugin.start_timer("x", 10**9, "missing"))["timers"][-1]
    assert t["duration"] == main.MAX_DURATION and t["preset_id"] is None
    with pytest.raises(ValueError):
        run(plugin.start_timer("x", "60"))


def test_overlay_gives_up_and_rotates_log(plugin, monkeypatch):
    monkeypatch.setattr(main, "_gamescope_running", lambda: True)
    plugin.settings.update(overlay_enabled=True, display=":99")  # no X server there
    with open(plugin.overlay_log, "w") as f:
        f.write("x" * (main.LOG_MAX_BYTES + 1))
    plugin.timers = [{"id": "t", "name": "T", "duration": 60, "end_at": time.time() + 60, "paused": False}]
    for _ in range(40):
        plugin._sync_overlay()
        plugin._reap()
        time.sleep(0.25)
        if plugin.overlay_failures >= main.OVERLAY_MAX_FAILURES:
            break
    assert plugin.overlay_failures == main.OVERLAY_MAX_FAILURES and plugin.proc is None
    assert os.path.exists(plugin.overlay_log + ".1")


def test_stuck_overlay_never_blocks(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_gamescope_running", lambda: True)
    stuck = tmp_path / "stuck.py"
    stuck.write_text("import time\ntime.sleep(60)\n")
    plugin.overlay_script = str(stuck)
    plugin.settings["overlay_enabled"] = True
    plugin.timers = [{"id": str(i), "name": "N" * 24, "duration": 60, "end_at": time.time() + 60,
                      "paused": False} for i in range(40)]
    t0 = time.time()
    for _ in range(200):
        plugin._sync_overlay()
    assert time.time() - t0 < 2 and plugin.overlay_failures >= 1


def test_no_overlay_outside_game_mode(plugin, monkeypatch):
    monkeypatch.setattr(main, "_gamescope_running", lambda: False)
    plugin.settings["overlay_enabled"] = True
    plugin.timers = [{"id": "t", "name": "T", "duration": 60, "end_at": time.time() + 60, "paused": False}]
    plugin.game_mode_checked = 0
    assert plugin._overlay_wanted() is False
