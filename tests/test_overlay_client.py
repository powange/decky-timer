"""Decky Timer as a client of the Decky Overlay socket (fake server here)."""
import asyncio
import json
import time

import main


class FakeOverlay:
    def __init__(self):
        self.messages = []
        self.server = None
        self.writers = []

    async def start(self):
        self.server = await asyncio.start_unix_server(self._handle, path=main.OVERLAY_SOCKET)

    async def _handle(self, reader, writer):
        self.writers.append(writer)
        while line := await reader.readline():
            self.messages.append(json.loads(line))

    async def stop(self):
        for w in self.writers:
            w.close()
        self.server.close()
        await self.server.wait_closed()

    def last(self, op=None):
        return [m for m in self.messages if op is None or m["op"] == op][-1]


async def _connect(plugin):
    plugin.overlay.last_attempt = 0
    await plugin.overlay.ensure()
    await asyncio.sleep(0.1)


def test_sends_timers_as_countdowns(plugin):
    async def scenario():
        fake = FakeOverlay()
        await fake.start()
        await plugin.start_timer("Pâtes", 600)  # before connecting: resent on connect
        await _connect(plugin)
        assert fake.messages[0] == {"op": "hello", "client": "decky-timer", "name": "Decky Timer", "protocol": 1}
        block = fake.last("set")
        assert block["id"] == "timers" and block["zone"] == "top-center"
        assert block["items"][0]["label"] == "Pâtes"
        assert 590 < block["items"][0]["countdown"]["end_at"] - time.time() <= 600
        tid = plugin.timers[0]["id"]
        await plugin.pause_timer(tid)
        await asyncio.sleep(0.05)
        assert fake.last("set")["items"][0]["countdown"]["paused"] is True
        await plugin.save_settings({"position": "bottom-left", "overlay_last_minutes": 5})
        await asyncio.sleep(0.05)
        assert fake.last("set")["zone"] == "bottom-left" and fake.last("set")["items"][0]["show_under"] == 300
        await plugin.stop_timer(tid)
        await asyncio.sleep(0.05)
        assert fake.messages[-1] == {"op": "clear", "id": "timers"}
        assert plugin._state()["overlay_status"] == "connected"
        await fake.stop()
    asyncio.run(scenario())


def test_reconnects_after_overlay_restart(plugin):
    async def scenario():
        fake = FakeOverlay()
        await fake.start()
        await _connect(plugin)
        await fake.stop()
        await asyncio.sleep(0.1)
        assert not plugin.overlay.connected
        import os
        if os.path.exists(main.OVERLAY_SOCKET):
            os.unlink(main.OVERLAY_SOCKET)
        fake2 = FakeOverlay()
        await fake2.start()
        await plugin.start_timer("Boss", 60)
        await _connect(plugin)
        assert fake2.messages[0]["op"] == "hello" and fake2.last("set")["items"][0]["label"] == "Boss"
        await fake2.stop()
    asyncio.run(scenario())


def test_overlay_disabled_or_preview(plugin):
    async def scenario():
        fake = FakeOverlay()
        await fake.start()
        await _connect(plugin)
        await plugin.save_settings({"overlay_enabled": False})
        await plugin.start_timer("A", 60)
        await asyncio.sleep(0.05)
        assert fake.messages[-1]["op"] == "clear"
        await plugin.preview_overlay()
        await asyncio.sleep(0.05)
        assert fake.last("set")["items"][0]["label"] == "Aperçu Decky Timer"
        await fake.stop()
    asyncio.run(scenario())


def test_status_when_overlay_missing(plugin, tmp_path, monkeypatch):
    import decky
    monkeypatch.setattr(decky, "DECKY_HOME", str(tmp_path / "homebrew"), raising=False)
    assert plugin._state()["overlay_status"] == "not_installed"
    d = tmp_path / "homebrew" / "plugins" / "decky-overlay"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({"name": "Decky Overlay"}))
    plugin.overlay_checked = 0
    assert plugin._state()["overlay_status"] == "not_running"


def test_install_prompt_is_remembered(plugin):
    assert plugin.settings["overlay_install_prompted"] is False
    asyncio.run(plugin.overlay_install_prompted())
    plugin2 = type(plugin)()
    plugin2._load()
    assert plugin2.settings["overlay_install_prompted"] is True
