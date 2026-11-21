"""Test harness: a fake `decky` module so main.py runs outside Decky Loader."""
import logging
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "overlay"))

decky = types.ModuleType("decky")
decky.logger = logging.getLogger("decky-test")
decky.DECKY_USER = "nobody-test"
decky.events = []


async def _emit(event, *args):
    decky.events.append((event, args))


decky.emit = _emit
sys.modules["decky"] = decky


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    import main

    for name in ("settings", "logs", "plugin"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(decky, "DECKY_PLUGIN_SETTINGS_DIR", str(tmp_path / "settings"), raising=False)
    monkeypatch.setattr(decky, "DECKY_PLUGIN_LOG_DIR", str(tmp_path / "logs"), raising=False)
    monkeypatch.setattr(decky, "DECKY_PLUGIN_DIR", ROOT, raising=False)
    decky.events.clear()
    p = main.Plugin()
    p.settings["overlay_enabled"] = False  # no X server needed unless a test asks
    yield p
    p._stop_overlay()
    p._reap(force=True)
