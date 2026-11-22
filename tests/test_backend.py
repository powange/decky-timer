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
            "settings": {"alarm_volume": "very", "position": "top-left"},
        }, f)
    plugin._load()
    assert [t["id"] for t in plugin.timers] == ["a", "b"]
    assert plugin.timers[0]["end_at"] and plugin.timers[1]["remaining"] == 30.0
    assert len(plugin.presets) == 1
    assert plugin.settings["position"] == "top-left"
    assert plugin.settings["alarm_volume"] == main.DEFAULT_SETTINGS["alarm_volume"]


def test_settings_whitelist():
    v = main._valid_settings({"alarm_volume": 0.5, "display": ":0; rm -rf /", "position": "huge",
                              "notify": "yes", "unknown": 1, "shortcut": "l4_r4"})
    assert v == {"alarm_volume": 0.5, "shortcut": "l4_r4"}


def test_api_bounds(plugin):
    t = run(plugin.start_timer("x", 10**9, "missing"))["timers"][-1]
    assert t["duration"] == main.MAX_DURATION and t["preset_id"] is None
    with pytest.raises(ValueError):
        run(plugin.start_timer("x", "60"))
