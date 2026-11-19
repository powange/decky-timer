import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid

import decky

DEFAULT_SETTINGS = {
    "overlay_enabled": True,
    "position": "top-right",   # top-left | top-center | top-right | bottom-left | bottom-right
    "size": "medium",          # small | medium | large | xlarge
    "opacity": 0.75,
    "notify": True,            # toast when a timer reaches zero
    "display": ":0",           # gamescope's Xwayland used by Steam
}

# Environment variables set by Decky's bundled (PyInstaller) python that would
# break the system python3 used for the overlay.
_STRIP_ENV = ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH")


def _now() -> float:
    return time.time()


class Plugin:
    def __init__(self):
        self.state_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "state.json")
        self.overlay_script = os.path.join(decky.DECKY_PLUGIN_DIR, "overlay", "overlay.py")
        self.overlay_log = os.path.join(decky.DECKY_PLUGIN_LOG_DIR, "overlay.log")
        self.presets = []
        self.timers = []
        self.settings = dict(DEFAULT_SETTINGS)
        self.proc = None
        self.overlay_error = ""
        self.overlay_failures = []
        self.watch_task = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def _main(self):
        os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
        os.makedirs(decky.DECKY_PLUGIN_LOG_DIR, exist_ok=True)
        self._load()
        self._sync_overlay()
        self.watch_task = asyncio.get_event_loop().create_task(self._watch())
        decky.logger.info("Decky Timer loaded")

    async def _unload(self):
        if self.watch_task:
            self.watch_task.cancel()
        self._stop_overlay()
        decky.logger.info("Decky Timer unloaded")

    async def _uninstall(self):
        self._stop_overlay()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _load(self):
        try:
            with open(self.state_path, "r") as f:
                data = json.load(f)
            self.presets = data.get("presets", [])
            self.timers = data.get("timers", [])
            self.settings = {**DEFAULT_SETTINGS, **data.get("settings", {})}
        except FileNotFoundError:
            pass
        except Exception as e:
            decky.logger.error(f"Could not read state: {e}")

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"presets": self.presets, "timers": self.timers, "settings": self.settings}, f, indent=2)
        os.replace(tmp, self.state_path)

    def _state(self):
        return {
            "now": _now(),
            "presets": self.presets,
            "timers": self.timers,
            "settings": self.settings,
            "overlay_running": self.proc is not None and self.proc.poll() is None,
            "overlay_error": self.overlay_error,
        }

    def _changed(self):
        self._save()
        self._sync_overlay()
        return self._state()

    def _find_timer(self, timer_id):
        return next((t for t in self.timers if t["id"] == timer_id), None)

    # ------------------------------------------------------------------ #
    # Overlay process
    # ------------------------------------------------------------------ #

    def _overlay_wanted(self):
        return bool(self.settings.get("overlay_enabled")) and len(self.timers) > 0

    def _overlay_payload(self):
        timers = [
            {k: t.get(k) for k in ("id", "name", "end_at", "remaining", "paused")}
            for t in self.timers
        ]
        settings = {k: self.settings.get(k) for k in ("position", "size", "opacity")}
        return json.dumps({"timers": timers, "settings": settings}) + "\n"

    def _start_overlay(self):
        python = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else shutil.which("python3")
        if not python:
            self.overlay_error = "python3 introuvable sur le système"
            return
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env["DISPLAY"] = self.settings.get("display") or ":0"
        try:
            log = open(self.overlay_log, "a")
            log.write(f"--- start {time.strftime('%Y-%m-%d %H:%M:%S')} DISPLAY={env['DISPLAY']}\n")
            log.flush()
            self.proc = subprocess.Popen(
                [python, "-u", self.overlay_script],
                stdin=subprocess.PIPE, stdout=log, stderr=log, env=env,
                cwd=os.path.dirname(self.overlay_script),
            )
            self.proc_started = _now()
            log.close()
        except Exception as e:
            self.proc = None
            self.overlay_error = f"Lancement impossible : {e}"
            decky.logger.error(self.overlay_error)

    def _stop_overlay(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=1.5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def _sync_overlay(self):
        """Start/stop the overlay as needed and push it the current timers."""
        if not self._overlay_wanted():
            self._stop_overlay()
            return
        if self.proc is not None and self.proc.poll() is not None:
            self._overlay_died()
        if self.proc is None:
            # Give up after 3 crashes in a minute; settings changes retry.
            recent = [t for t in self.overlay_failures if _now() - t < 60]
            if len(recent) >= 3:
                return
            self._start_overlay()
        if self.proc is not None:
            try:
                self.proc.stdin.write(self._overlay_payload().encode())
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._overlay_died()

    def _overlay_died(self):
        code = self.proc.poll() if self.proc else None
        self.proc = None
        self.overlay_failures.append(_now())
        tail = ""
        try:
            with open(self.overlay_log, "r") as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
            tail = lines[-1] if lines else ""
        except Exception:
            pass
        self.overlay_error = f"L'overlay s'est arrêté (code {code}). {tail}".strip()
        decky.logger.error(self.overlay_error)

    async def _watch(self):
        while True:
            try:
                await asyncio.sleep(0.5)
                now = _now()
                fired = []
                for t in self.timers:
                    if not t.get("paused") and not t.get("notified") and t.get("end_at") and t["end_at"] <= now:
                        t["notified"] = True
                        fired.append(t)
                if fired:
                    self._save()
                    if self.settings.get("notify", True):
                        for t in fired:
                            await decky.emit("timer_finished", {"id": t["id"], "name": t["name"]})
                # Restart a crashed overlay.
                if self._overlay_wanted() and (self.proc is None or self.proc.poll() is not None):
                    self._sync_overlay()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"watch loop: {e}")

    # ------------------------------------------------------------------ #
    # Frontend API
    # ------------------------------------------------------------------ #

    async def get_state(self):
        return self._state()

    async def get_version(self) -> str:
        version = os.environ.get("DECKY_PLUGIN_VERSION")
        if version:
            return version
        try:
            with open(os.path.join(decky.DECKY_PLUGIN_DIR, "package.json"), "r") as f:
                return json.load(f).get("version", "?")
        except Exception:
            return "?"

    # Timers ----------------------------------------------------------- #

    async def start_timer(self, name: str, duration: float, preset_id: str = ""):
        duration = max(1, int(duration))
        now = _now()
        self.timers.append({
            "id": uuid.uuid4().hex[:10],
            "name": (name or "").strip() or "Timer",
            "duration": duration,
            "preset_id": preset_id or None,
            "end_at": now + duration,
            "remaining": None,
            "paused": False,
            "notified": False,
        })
        return self._changed()

    async def pause_timer(self, timer_id: str):
        t = self._find_timer(timer_id)
        if t and not t["paused"]:
            t["remaining"] = t["end_at"] - _now()
            t["end_at"] = None
            t["paused"] = True
        return self._changed()

    async def resume_timer(self, timer_id: str):
        t = self._find_timer(timer_id)
        if t and t["paused"]:
            t["end_at"] = _now() + float(t["remaining"] or 0)
            t["remaining"] = None
            t["paused"] = False
        return self._changed()

    async def restart_timer(self, timer_id: str):
        t = self._find_timer(timer_id)
        if t:
            t["end_at"] = _now() + t["duration"]
            t["remaining"] = None
            t["paused"] = False
            t["notified"] = False
        return self._changed()

    async def stop_timer(self, timer_id: str):
        self.timers = [t for t in self.timers if t["id"] != timer_id]
        return self._changed()

    async def stop_all(self):
        self.timers = []
        return self._changed()

    # Presets ---------------------------------------------------------- #

    async def save_preset(self, preset_id: str, name: str, duration: float):
        name = (name or "").strip() or "Timer"
        duration = max(1, int(duration))
        existing = next((p for p in self.presets if p["id"] == preset_id), None) if preset_id else None
        if existing:
            existing["name"] = name
            existing["duration"] = duration
        else:
            self.presets.append({"id": uuid.uuid4().hex[:10], "name": name, "duration": duration})
        return self._changed()

    async def delete_preset(self, preset_id: str):
        self.presets = [p for p in self.presets if p["id"] != preset_id]
        return self._changed()

    async def move_preset(self, preset_id: str, delta: int):
        idx = next((i for i, p in enumerate(self.presets) if p["id"] == preset_id), None)
        if idx is not None:
            new = max(0, min(len(self.presets) - 1, idx + int(delta)))
            self.presets.insert(new, self.presets.pop(idx))
        return self._changed()

    # Settings --------------------------------------------------------- #

    async def save_settings(self, settings: dict):
        old_display = self.settings.get("display")
        for key in DEFAULT_SETTINGS:
            if key in settings:
                self.settings[key] = settings[key]
        # A settings change is also a manual retry after overlay crashes.
        self.overlay_failures = []
        self.overlay_error = ""
        if self.settings.get("display") != old_display:
            self._stop_overlay()
        return self._changed()
